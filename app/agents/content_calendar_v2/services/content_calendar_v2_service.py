# app/agents/content_calendar_v2/services/content_calendar_v2_service.py
"""
Content Calendar V2 — Creative Intelligence Engine.

Staging-only, fully isolated from the v1 7-day Content Calendar
(app/agents/social_media_manager/services/content_calendar_service.py):
own collection (content_calendar_v2_plans), own package, own router prefix.

Rewritten against "URI Social — Living Content Calendar & Creative
Intelligence Engine" — see
/Users/macintoshhd/.claude/plans/enchanted-wiggling-treehouse.md for the
full plan this was built against.

CRITICAL, non-negotiable (PRD §2): performance data and Google Trends must
NEVER influence which content ideas get selected. This file has ZERO
dependency on PerformanceAnalyticsService or TrendDataService anywhere in
its generation path — sync_item_performance() is the one function allowed
to touch a draft's performance metrics, and only AFTER publication, purely
for user-facing display, never feeding back into generation. If you're
about to add a performance/trend-derived signal to anything upstream of
_generate_candidate_concepts, don't — that's exactly the line this rewrite
exists to hold.

Selection now runs a staged pipeline (PRD §32-38): load creative framework
-> generate ~80 candidate CONCEPTS (structured only, no copy) -> score on
9 non-performance dimensions -> diversity-optimized select 30 -> assign
dates -> assign format (dynamic 2-3 slide carousels, not fixed) -> generate
final copy + creative direction (concept-conditioned, one combined call per
chunk — see _generate_final_copy's docstring for why two stages share one
network round-trip) -> ad evaluation -> validate (deterministic + semantic
+ anti-boring) -> auto-regenerate flagged items.

Reused from v1 (now in calendar_rules.py; v1 itself was deleted) — only the genuinely
content-type-agnostic pure helpers: _STAGE_GUIDANCE,
_business_pulse_freshness_str, _validate_day. Deliberately NOT reused:
_pick_mix_from_performance, DEFAULT_MIX_VARIANTS, INDUSTRY_MIX, POST_FORMATS,
HOOK_STYLES, POST_FORMAT_TO_KEY, CONTENT_TYPES, CONTENT_TYPE_LABELS — all
superseded by creative_framework.py's territory/subject/angle/device system,
or (for _pick_mix_from_performance) removed outright as the one function
that directly violated PRD §2.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.AIService import AIService

from app.agents.social_media_manager.services.holiday_calendar_service import HolidayCalendarService
from app.agents.social_media_manager.services.cultural_moment_service import CulturalMomentService

# Pure, stateless helpers shared with the caption writer (calendar_rules.py).
from app.agents.social_media_manager.services.calendar_rules import (
    _STAGE_GUIDANCE,
    _business_pulse_freshness_str,
    _validate_day,
)

from ..models import AdCopyV2, AdOpportunityV2
from ..creative_framework import (
    get_creative_framework,
    STRUCTURAL_DEVICE_SLIDE_HINT,
    ANTI_BORING_PHRASES,
    ANTI_BORING_OPENER_STEMS,
    GENERIC_AI_WORDS,
)

# Matches a risky verb, any conjugation ("Unlock", "Unlocking", "Discover",
# "Discovering"...), OPENING the title or a later clause within it — a title
# can be two sentences ("Why Pay More? Discover Our Pricing Edge"), and the
# addendum's concern is the verb opening a headline-style clause, not merely
# appearing somewhere in the sentence structure (that's still the fixed
# bigrams above, and why "the electrician masters in year one" — the verb
# mid-clause — must NOT match here). Built once at import time.
_ANTI_BORING_OPENER_RE = re.compile(
    r"(?:^|[.!?:—-]\s+)\s*((?:" + "|".join(ANTI_BORING_OPENER_STEMS) + r")\w*)\b",
    re.IGNORECASE,
)

COLLECTION = "content_calendar_v2_plans"
PLAN_DAYS = 30
CANDIDATE_POOL_SIZE = 80     # PRD §10's own stated floor (recommended range 80-150) —
                              # confirmed live: this endpoint is hitting a 504 Gateway
                              # Timeout, and the backend keeps working minutes after the
                              # client gives up. Every stage's latency scales with pool
                              # size (candidate generation AND the scoring call, which
                              # also mis-fired at 100 candidates — 124 scores returned
                              # for 102 candidates). Staying at the PRD's floor rather
                              # than going below it trades some candidate-pool richness
                              # for real margin against the timeout.
CANDIDATE_CHUNK_SIZE = 20    # concurrent chunks, mirrors the proven content-chunking pattern
CONTENT_CHUNK_SIZE = 6       # ~5 chunks of 6 for final copy — v1's own 7-item cap is
                              # evidence larger single structured-JSON calls degrade

MAX_VIDEO_FORMATS_PER_WEEK = 1   # user requirement: at most this many video-like items
MAX_VIDEO_FORMATS_PER_WEEK_WHEN_CHOSEN = 3   # the brand picked Reels/video in its Playbook
MIN_CAROUSEL_FORMATS_PER_WEEK = 1   # business requirement (Oct 2026): 1-2 carousels per week
MAX_CAROUSEL_FORMATS_PER_WEEK = 2
MAX_CAPTION_WORDS = 60   # business requirement (Oct 2026): captions were coming back too wordy;
                         # TEXT DENSITY was only ever a prompt suggestion, never a hard cap
                                  # (video/product_video/ai_video) per 7-day calendar week
_VIDEO_FORMATS = {"video", "product_video", "ai_video"}

# PRD §26 — Ad Angle Library, derived from an item's own 27-value creative
# angle (creative_framework.ANGLES), not from content_type (the old
# _ANGLE_BY_CONTENT_TYPE only ever reached 4/7 of its declared values).
_AD_ANGLE_BY_CREATIVE_ANGLE: Dict[str, str] = {
    "the_mistake": "problem", "the_hidden_cost": "problem", "the_misconception": "problem",
    "the_warning": "problem",
    "the_transformation": "outcome", "the_aspiration": "outcome",
    "the_customer_story": "proof", "the_story": "proof", "the_confession": "proof",
    "the_experiment": "proof",
    "the_opportunity": "offer",
    "before_you_buy": "objection", "the_customers_question": "objection", "nobody_tells_you": "objection",
    "the_comparison": "comparison", "the_decision_guide": "comparison", "what_would_you_choose": "comparison",
    "what_happens_if": "urgency", "the_challenge": "urgency",
    "the_beginner_perspective": "convenience", "the_explanation": "convenience",
    "the_unexpected_truth": "transformation", "the_myth": "transformation", "the_reaction": "transformation",
    "the_expert_perspective": "product_demonstration", "the_founder_perspective": "product_demonstration",
    "the_unpopular_opinion": "product_demonstration",
}

# Maps the new 12-territory classification back onto v1's 5-value
# content_type enum, purely so the existing frontend TypeBadge (already
# built, keyed off content_type) keeps rendering without changes — territory
# is the real, richer selection dimension now; content_type is a
# compatibility shim onto it, not the other way around.
_CONTENT_TYPE_BY_TERRITORY: Dict[str, str] = {
    "A_PROBLEM": "educational",
    "B_DESIRE": "relatable",
    "C_CURIOSITY": "educational",
    "D_CONTRARIAN": "educational",
    "E_PROOF": "promotional",
    "F_PEOPLE": "behind_the_scenes",
    "G_PROCESS": "behind_the_scenes",
    "H_COMPARISON": "educational",
    "I_EDUCATION": "educational",
    "J_CULTURE_CONTEXT": "relatable",
    "K_ENTERTAINMENT": "engagement",
    "L_COMMERCIAL": "promotional",
}


def _derive_content_type(territory: str) -> str:
    return _CONTENT_TYPE_BY_TERRITORY.get(territory, "educational")


def _derive_ad_angle(creative_angle: str) -> str:
    return _AD_ANGLE_BY_CREATIVE_ANGLE.get(creative_angle, "outcome")


def _cal_v2_scope(user_id: str, brand_id: Optional[str]) -> Dict[str, Any]:
    """Brand-aware Mongo filter — deliberately a standalone copy of v1's
    _cal_scope (same pattern used everywhere in this codebase: _brand_scope,
    _cal_scope, _auto_scope), not an import, so an edit to one calendar
    system's isolation logic can never silently change the other's."""
    from app.models.brand_account import BrandAccount
    personal_bid = BrandAccount.personal_brand_id(user_id)
    if brand_id and brand_id != personal_bid:
        return {"brand_id": brand_id}
    return {
        "user_id": user_id,
        "$or": [
            {"brand_id": {"$exists": False}},
            {"brand_id": None},
            {"brand_id": personal_bid},
        ],
    }


def _get_period_start(ref: datetime) -> datetime:
    """First day of the 30-day window — 'today', midnight UTC (unlike v1's
    Monday-anchored week, a 30-day plan has no natural week anchor)."""
    return ref.replace(hour=0, minute=0, second=0, microsecond=0)


def _loads_lenient(raw: str) -> Any:
    """json.loads with repair passes for the two ways LLM JSON reliably
    breaks (both confirmed live in this pipeline): invalid backslash escapes
    (Invalid \\escape) and trailing commas. Also strips a leading ```json
    fence. Raises the original JSONDecodeError if none of the repairs land."""
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) >= 2 else text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Double any backslash that isn't the start of a valid JSON escape.
    repaired = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    # Strip trailing commas before } or ].
    repaired = re.sub(r',(\s*[}\]])', r'\1', repaired)
    return json.loads(repaired)


def _as_creative_device(v: Any) -> Dict[str, str]:
    """Normalizes creative_device to a guaranteed {"category","device"} dict
    at the one point it enters the pipeline (candidate generation), so every
    downstream .get() call on it is safe. Confirmed live: despite the prompt
    asking for an object, the model can flatten this to a plain string
    (e.g. "visual - split screen"), which crashed every .get("category")
    call site downstream with 'str' object has no attribute 'get'."""
    if isinstance(v, dict):
        return {"category": str(v.get("category", "")), "device": str(v.get("device", ""))}
    if isinstance(v, str) and v.strip():
        return {"category": "", "device": v.strip()}
    return {"category": "", "device": ""}


# ── PRD §20 — Creative memory (never performance) ───────────────────────────

async def _fetch_creative_memory(scope: Dict[str, Any], db: AsyncIOMotorDatabase) -> Dict[str, Any]:
    """Remembers WHAT was used before, never how it performed — the exact
    line PRD §20 draws ('we already used X' is fine; 'X performed well, make
    another' is not). Tracks territory/subject/angle/device/format/concept-
    name/hook/cta/visual-concept — 8 of PRD §20's 9 listed dimensions (the
    9th, content series, has its own history once a series is actually
    assigned — see series_names below), still via the same 2-most-recent-
    plans query, no new collection needed.

    hooks/ctas/visual_concepts were the 3 PRD §20 dimensions missing before
    this pass — a repeated hook or CTA is what a real reader actually
    notices, more than an internal territory/angle key repeating."""
    memory: Dict[str, Any] = {
        "titles": [], "key_points": [], "territories": [], "subjects": [],
        "angles": [], "devices": [], "formats": [], "concept_names": [],
        "hooks": [], "ctas": [], "visual_concepts": [], "series_names": [],
    }
    async for past in db[COLLECTION].find(
        {**scope},
        {"_id": 0, "items.title": 1, "items.key_points": 1, "items.territory": 1,
         "items.subject": 1, "items.creative_angle": 1, "items.creative_device": 1,
         "items.format": 1, "items.creative_concept_name": 1, "items.hook": 1,
         "items.cta": 1, "items.creative_direction": 1, "items.series_name": 1},
    ).sort("created_at", -1).limit(2):
        for it in past.get("items", []):
            if it.get("title"):
                memory["titles"].append(it["title"])
            memory["key_points"] += [str(p) for p in (it.get("key_points") or []) if p]
            if it.get("territory"):
                memory["territories"].append(it["territory"])
            if it.get("subject"):
                memory["subjects"].append(it["subject"])
            if it.get("creative_angle"):
                memory["angles"].append(it["creative_angle"])
            device = it.get("creative_device") or {}
            if device.get("device"):
                memory["devices"].append(device["device"])
            if it.get("format"):
                memory["formats"].append(it["format"])
            if it.get("creative_concept_name"):
                memory["concept_names"].append(it["creative_concept_name"])
            if it.get("hook"):
                memory["hooks"].append(it["hook"])
            if it.get("cta"):
                memory["ctas"].append(it["cta"])
            visual_idea = (it.get("creative_direction") or {}).get("central_visual_idea")
            if visual_idea:
                memory["visual_concepts"].append(visual_idea)
            if it.get("series_name"):
                memory["series_names"].append(it["series_name"])
    return memory


# ── PRD §22 — Asset-first intelligence, scoped to what's actually queryable ─

async def _get_existing_assets_summary(
    user_id: str, brand_id: Optional[str], db: AsyncIOMotorDatabase,
) -> str:
    """No asset-library/repurposing infrastructure exists anywhere in this
    codebase (confirmed via exhaustive grep during planning) — the only
    queryable 'existing content' a brand has on record is content_drafts
    (past generated/uploaded media) and brand_profiles' own logo/sample
    templates. Returns a short human-readable summary threaded into
    generation prompts as a nudge toward reuse, not a full asset-management
    subsystem (see plan's explicit deferral of full §23 repurposing)."""
    query: Dict[str, Any] = {"brand_id": brand_id} if brand_id else {"user_id": user_id}
    parts: List[str] = []
    try:
        image_count = await db["content_drafts"].count_documents({**query, "image_url": {"$exists": True, "$ne": None}})
        video_count = await db["content_drafts"].count_documents({**query, "video_url": {"$exists": True, "$ne": None}})
        if image_count:
            parts.append(f"{image_count} existing product/brand image(s) from past drafts")
        if video_count:
            parts.append(f"{video_count} existing video(s) from past drafts")

        profile = await db["brand_profiles"].find_one(query, {"logo_url": 1, "sample_template_urls": 1})
        if profile:
            if profile.get("logo_url"):
                parts.append("a brand logo")
            if profile.get("sample_template_urls"):
                parts.append(f"{len(profile['sample_template_urls'])} sample design template(s)")
    except Exception as exc:
        print(f"[CalendarV2] existing-assets lookup failed (non-fatal): {exc}", flush=True)
    return "; ".join(parts)


# ── URI Content Calendar Generation addendum §1-5 — Brand Playbook as a
# primary input, not background. Both blocks are shared between candidate
# generation (so ideas are grounded in real customer psychology from the
# start, not decorated with it afterward — the addendum's own "do not
# generate the calendar first and then add the brand" rule) and final copy
# (so headlines/visuals execute against the same grounding). Every field is
# read defensively — a brand that hasn't filled these in yet still gets a
# normal prompt, just without this block; nothing here can crash or degrade
# generation for a thin profile. ──────────────────────────────────────────

def _customer_psychology_block(brand: Dict[str, Any]) -> str:
    """Addendum §1-4: pain points/needs/desires/objections/etc — real
    customer psychology from the Brand Playbook, not the brand's own product
    description. Confirmed before this existed: the calendar's prompts only
    ever read brand_name/industry/voice/audience-summary/USP/business_pulse
    — customer_pain_points/needs/objections/why_customers_choose_us were
    already stored on every brand profile (to_brand_context already returns
    them) but never once reached either generation prompt."""
    parts: List[str] = []
    if brand.get("ideal_customer_profile"):
        parts.append(f"Ideal customer profile: {brand['ideal_customer_profile']}")
    for label, key in [
        ("Pain points", "customer_pain_points"),
        ("Needs", "customer_needs"),
        ("Desires", "customer_desires"),
        ("Fears", "customer_fears"),
        ("Frustrations", "customer_frustrations"),
        ("Aspirations", "customer_aspirations"),
        ("Objections", "customer_objections"),
        ("Reasons they hesitate", "customer_hesitations"),
        ("Common questions", "common_questions"),
        ("Buying triggers", "buying_triggers"),
    ]:
        vals = [str(v) for v in (brand.get(key) or []) if v]
        if vals:
            parts.append(f"{label}: " + "; ".join(vals))
    if brand.get("why_customers_choose_us"):
        parts.append(f"Why customers choose this business: {brand['why_customers_choose_us']}")
    words_to_avoid = [str(w) for w in (brand.get("words_to_avoid") or []) if w]
    if words_to_avoid:
        parts.append("Words/phrases to avoid: " + ", ".join(words_to_avoid))
    if not parts:
        return ""
    return (
        "\n\nCUSTOMER PSYCHOLOGY (from this brand's Playbook — real customer reality, not a generic assumption):\n"
        + "\n".join(f"- {p}" for p in parts)
        + "\n\nEvery idea must clearly draw on ONE specific item above — a real pain point, need, "
          "desire, objection, fear, or reason this audience chooses this business. Write for THIS "
          "audience specifically — never for \"business owners\", \"customers\", \"people\", or "
          "\"everyone\". Where a customer phrase or question is listed above, consider using it "
          "close to verbatim rather than paraphrasing it into generic marketing language — see the "
          "addendum's own example: \"I don't have time to cook\" becomes \"You don't hate cooking. "
          "You hate deciding what to cook at 7:30pm,\" not \"5 Benefits of Meal Delivery.\""
    )


def _brand_rules_block(brand: Dict[str, Any]) -> str:
    """The Playbook's hard rules, content pillars and important dates for the
    PLAN itself. Drafts made from an idea always applied these, but the ideas
    were generated without them — a plan could be off-pillar, miss the brand's
    own key dates, or pitch a topic the brand said never to touch."""
    from app.agents.social_media_manager.services import brand_brief

    parts: List[str] = []
    rules = brand_brief.hard_rules(brand)
    if rules:
        parts.append("HARD RULES (never break these — an idea that needs to break one is not usable):\n"
                     + "\n".join(f"- {r}" for r in rules))
    pillars = [str(x) for x in (brand.get("content_pillars") or []) if x]
    if pillars:
        parts.append(f"CONTENT PILLARS: {', '.join(pillars)}. Every idea must sit under one of these, "
                     f"and the plan should be spread across them rather than leaning on one.")
    if brand.get("key_dates"):
        parts.append(f"IMPORTANT DATES for this brand: {brand['key_dates']}. Plan a post for any that "
                     f"fall inside this plan's period, on or just before the date.")
    allowed = _allowed_formats(brand)
    if allowed:
        parts.append("FORMATS this brand posts in: " + ", ".join(sorted(allowed))
                     + ". Plan only ideas that work in these formats.")
    parts.append('NEVER use these generic AI words, in any form ("transforms", "discovering" count too): '
                 + ", ".join(GENERIC_AI_WORDS) + ". Say the specific thing instead.")
    return "\n\n" + "\n\n".join(parts)


def _visual_guide_block(brand: Dict[str, Any]) -> str:
    """Addendum §5: the client's selected Visual Guide is the design system
    — the model must stop inventing a new visual style per post. Confirmed
    before this existed: design_style/layout_direction were fully
    open-ended per item ("a few words describing the visual design style"),
    even though style_selections (the actual Visual Guide the client picked)
    was already sitting on the brand profile, unused by this pipeline."""
    styles = [str(s) for s in (brand.get("style_selections") or []) if s]
    if not styles:
        return ""
    return (
        f"\n\nVISUAL GUIDE (the client's selected design system — mandatory, not optional): "
        f"{', '.join(styles)}. This is the ONE visual language for every item in this plan. "
        f"The creative idea can and should change from post to post; design_style and "
        f"layout_direction must stay recognisably within this visual language every time — "
        f"do not invent a different design style per item."
    )


def _recurring_series_names(resolved_names: List[Optional[str]], min_occurrences: int = 2) -> set:
    """Which series names actually recurred across the plan.

    PRD §21 / the candidate-generation prompt both say a series name should
    only be used when it genuinely applies to several concepts — "a series
    used exactly once isn't recurring, don't invent one just to fill this
    field." That was prompt-only until now, and confirmed live: a real plan
    carried 6 distinct series names, every one used exactly once. Pure and
    deterministic on purpose, matching this file's other hard-rule checks
    (_find_banned_words, the carousel slide-count clamp) — this rule doesn't
    get to depend on the model having followed the instruction."""
    counts: Dict[str, int] = {}
    for name in resolved_names:
        if name:
            counts[name] = counts.get(name, 0) + 1
    return {name for name, count in counts.items() if count >= min_occurrences}


def _resolve_series_name(name: Optional[str], brand_name: str, industry: str) -> Optional[str]:
    """SERIES_TEMPLATES keeps a couple of names as literal placeholders
    ("Ask [Brand]", "The [Industry] Myth") — confirmed live: a generated plan
    shipped "The [Industry] Myth" verbatim, because nothing ever substituted
    the bracket before the name reached the model or the final item. Applied
    both when building the prompt's series list (so the model is never shown
    an unresolved bracket to copy) and again defensively on the final item."""
    if not name:
        return name
    return name.replace("[Brand]", brand_name).replace("[Industry]", industry)


# ── Step 3b — Listening-sourced candidates (Listening PRD §18) ─────────────
# NOT the TrendDataService/PerformanceAnalyticsService this file's module
# docstring bans upstream of candidate generation — those are fabricated/
# simulated placeholders and real post-performance numbers respectively,
# exactly the kind of signal that could corrupt autonomous ideation with
# something false. A Listening card is the opposite: a real, evidence-gated
# customer signal from this exact business's own market, already passed
# through its own confidence/relevance gates and fabrication-rejecting
# validator (market_intelligence/listening/personalize.py) before it ever
# reaches here. This function does no ideation and calls no model — it only
# reshapes already-real, already-validated fields into the candidate-concept
# shape, so it carries zero fabrication risk into the pool it's added to.
# ad_test cards are skipped: there's no content format for them here, and
# turning one into a post would misrepresent what it actually is.
_LISTENING_TERRITORY_BY_TYPE = {
    "recurring_need": "A_PROBLEM",
}
_LISTENING_FORMAT_BY_ACTION_TYPE = {"video": "video", "carousel": "carousel", "faq": "image", "post": "image"}


async def _listening_candidate_concepts(
    db: AsyncIOMotorDatabase, user_id: str, brand_id: Optional[str], limit: int = 6,
) -> List[Dict[str, Any]]:
    from app.models.brand_account import BrandAccount
    resolved_brand_id = brand_id or BrandAccount.personal_brand_id(user_id)
    now = datetime.utcnow()
    out: List[Dict[str, Any]] = []
    cursor = db["mi_listen_cards"].find(
        {"brand_id": resolved_brand_id, "status": {"$in": ["new", "shown", "saved"]}, "expires_at": {"$gt": now}}
    ).sort([("scores.total", -1)]).limit(limit * 2)  # headroom for ad_test skips
    async for card in cursor:
        action = card.get("action") or {}
        if action.get("type") == "ad_test" or not action.get("platforms"):
            continue
        out.append({
            "territory": _LISTENING_TERRITORY_BY_TYPE.get(card.get("type"), "C_CURIOSITY"),
            "subject": card.get("topic_name") or card.get("topic", ""),
            "angle": "The Customer's Question",
            "creative_device": {"category": "conversational", "device": "Customer question"},
            "format_hint": _LISTENING_FORMAT_BY_ACTION_TYPE.get(action.get("type"), "image"),
            "objective": "engagement",
            "audience_segment": (card.get("why") or "")[:80] or "customers already asking about this",
            "concept_name": action.get("title") or card.get("topic_name") or card.get("topic", ""),
            "series_name": None,
            "source": "listening",
            "source_card_id": card.get("id"),
            "listening_brief": {
                "observation": card.get("observation"),
                "hook": action.get("hook"),
                "outline": action.get("outline"),
                "cta": action.get("cta"),
            },
        })
        if len(out) >= limit:
            break
    return out


# ── Step 3c — Repurposing candidates (PRD §23) ──────────────────────────────
# No asset-library/repurposing infrastructure exists anywhere in this
# codebase (confirmed — see _get_existing_assets_summary's docstring above),
# so there's no literal "founder interview" or "customer testimonial" raw
# asset to chain the PRD's worked examples from. What IS real and already
# queryable: this brand's own past APPROVED drafts — genuine content the
# business already put out, often with real media already generated. PRD
# §23's actual goal ("recognize when one source can generate multiple
# pieces... reduces production burden") maps onto that directly: seed a new
# angle/format this cycle from a real proven post instead of starting from
# nothing. Same zero-fabrication-risk shape as _listening_candidate_concepts
# — real text in, nothing invented — which is why it reuses that function's
# "listening_brief" grounding field rather than inventing a parallel one.
_REPURPOSE_FORMAT_BY_POST_TYPE = {
    # The new format this cycle, given what the source post already was —
    # never repeats the same format back (that's not repurposing, that's
    # a duplicate), and always something genuinely producible from the
    # existing media (PRD §17's "production feasibility" factor).
    "image": "carousel",      # break the same proven idea into steps
    "carousel": "image",      # distill it to one punchy quote/graphic
    "video": "carousel",      # turn a video's key moments into a carousel
}
_MIN_REPURPOSE_SOURCE_WORDS = 8   # shorter than this isn't real "source material"


async def _repurposing_candidate_concepts(
    db: AsyncIOMotorDatabase, user_id: str, brand_id: Optional[str], limit: int = 4,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"brand_id": brand_id} if brand_id else {"user_id": user_id}
    query.update({
        "approval_status": "approved",
        "content": {"$exists": True, "$ne": ""},
        "$or": [{"has_image": True}, {"video_url": {"$exists": True, "$ne": None}}],
    })
    out: List[Dict[str, Any]] = []
    cursor = db["content_drafts"].find(query).sort([("created_at", -1)]).limit(limit * 4)
    async for draft in cursor:
        content = str(draft.get("content") or "").strip()
        if len(content.split()) < _MIN_REPURPOSE_SOURCE_WORDS:
            continue
        new_format = _REPURPOSE_FORMAT_BY_POST_TYPE.get(draft.get("post_type") or "image", "image")
        out.append({
            "territory": "E_PROOF",
            "subject": "A proven idea, retold",
            "angle": "The Comparison" if new_format == "carousel" else "The Unexpected Truth",
            # Deliberately NOT "story"/"conversational"+"talking_head" —
            # _assign_format treats those as unconditional video overrides,
            # which would silently discard new_format below.
            "creative_device": {"category": "visual", "device": "Repurposed asset"},
            "format_hint": new_format,
            "objective": "engagement",
            "audience_segment": "an audience that already responded to this once",
            "concept_name": f"Repurposed: {content[:60]}",
            "series_name": None,
            "source": "repurposed",
            "source_draft_id": draft.get("id"),
            "listening_brief": {
                "observation": f"A real post this business already published: \"{content[:300]}\"",
                "hook": None, "outline": None, "cta": None,
            },
        })
        if len(out) >= limit:
            break
    return out


# ── Step 3 — Candidate concept pool (PRD §10, §36) ──────────────────────────

async def _generate_candidate_concepts(
    brand: Dict[str, Any],
    framework: Dict[str, Any],
    existing_assets_summary: str,
    creative_memory: Dict[str, Any],
    platforms: List[str],
    cultural_moments: Optional[List[Any]] = None,  # real dated events only — CulturalMomentService.get_cultural_moments()
    target_count: int = CANDIDATE_POOL_SIZE,
) -> List[Dict[str, Any]]:
    """Generates 80-150 structured CONCEPTS — territory/subject/angle/
    creative_device/format_hint/objective/audience_segment/concept_name
    ONLY, explicitly no final copy yet (PRD §36: 'the system is deciding
    WHAT to say, not yet writing exactly how to say it'). NEVER receives
    performance or trend_keywords — that's the concrete enforcement of
    PRD §2, not just a prompt instruction: those objects simply don't exist
    in this function's argument list.

    industry_best_practices was deliberately removed from this signature —
    IndustryTrendService.get_industry_best_practices() returns fabricated
    engagement statistics ("Educational content drives 2.3x more engagement
    in tech") presented as fact. That's PRD §2's very first banned input
    (historical engagement performance) and a §31 fabrication violation at
    the same time, and it was being injected straight into this prompt."""
    brand_name = brand.get("brand_name") or "the brand"
    industry = brand.get("industry") or "business"
    audience = brand.get("target_audience") or "general audience"
    voice = brand.get("brand_voice") or "professional and engaging"
    description = brand.get("business_description", "")
    usp = brand.get("unique_selling_proposition", "")
    business_stage = brand.get("business_stage", "")
    stage_note = (
        f"Business stage: {business_stage} — {_STAGE_GUIDANCE.get(business_stage, '')}"
        if business_stage else ""
    )
    psychology_block = _customer_psychology_block(brand) + _brand_rules_block(brand)

    territories_block = "\n".join(
        f"- {key} ({t['label']}): {t['description']} Example subjects: {', '.join(t['subjects'][:8])}"
        for key, t in framework["territories"].items()
    )
    angles_block = ", ".join(a["label"] for a in framework["angles"])
    devices_block = "\n".join(
        f"- {cat}: " + ", ".join(d["label"] for d in devices)
        for cat, devices in framework["creative_devices"].items()
    )
    series_templates = framework.get("series_templates") or []
    series_block = "\n".join(
        f'- "{_resolve_series_name(s["name"], brand_name, industry)}"' for s in series_templates
    )
    prior_series = creative_memory.get("series_names") or []
    prior_series_block = (
        "\nSeries already used for this business — prefer reusing one of these over starting a new one, "
        "so it actually recurs recognizably: " + ", ".join(dict.fromkeys(prior_series))
    ) if prior_series else ""

    context_lines = []
    if cultural_moments:
        # Real dated events only (CulturalMomentService.get_cultural_moments)
        # — List[Dict] with a "name" key. String entries handled too, kept
        # defensive since this list is built from an external service.
        names = [
            (m.get("name") or m.get("topic") or str(m)) if isinstance(m, dict) else str(m)
            for m in cultural_moments[:5]
        ]
        context_lines.append(f"Relevant cultural moments this period: {', '.join(names)}")
    context_block = ("\n" + "\n".join(context_lines)) if context_lines else ""

    avoid_parts = []
    if creative_memory.get("concept_names"):
        avoid_parts.append(
            "Already explored recently — do not repeat these concept names or their "
            "underlying territory+subject+angle combination:\n"
            + "\n".join(f"- {c}" for c in creative_memory["concept_names"][:30])
        )
    # PRD §20's other 2 remembered dimensions besides concept/territory/subject/
    # angle — a repeated hook or CTA is what a real reader actually notices.
    if creative_memory.get("hooks"):
        avoid_parts.append(
            "Already-used hooks — do not reuse or closely rephrase these:\n"
            + "\n".join(f"- {h}" for h in creative_memory["hooks"][:30])
        )
    if creative_memory.get("ctas"):
        distinct_ctas = list(dict.fromkeys(creative_memory["ctas"]))[:10]
        if distinct_ctas:
            avoid_parts.append(
                "Recently-used CTAs — vary the call to action, don't default back to these every time:\n"
                + "\n".join(f"- {c}" for c in distinct_ctas)
            )
    if creative_memory.get("visual_concepts"):
        avoid_parts.append(
            "Already-used central visual ideas — do not repeat these:\n"
            + "\n".join(f"- {v}" for v in creative_memory["visual_concepts"][:30])
        )
    avoid_block = ("\n" + "\n".join(avoid_parts)) if avoid_parts else ""

    assets_block = f"\nExisting assets available: {existing_assets_summary}" if existing_assets_summary else ""

    remaining = target_count
    chunk_sizes: List[int] = []
    while remaining > 0:
        size = min(CANDIDATE_CHUNK_SIZE, remaining)
        chunk_sizes.append(size)
        remaining -= size

    async def _one_chunk(chunk_idx: int, n: int) -> List[Dict[str, Any]]:
        prompt = f"""You are a senior creative strategist generating CANDIDATE content
CONCEPTS for {brand_name}, a {industry} business. Target audience: {audience}.
Brand voice: {voice}. {f'What they do: {description}.' if description else ''}
{f'USP: {usp}.' if usp else ''}
{stage_note}{context_block}{assets_block}{avoid_block}{psychology_block}
Platforms: {', '.join(platforms) if platforms else 'social media'}.

Available Content Territories (draw from these freely — you don't need every one):
{territories_block}

Available Angles: {angles_block}

Available Creative Devices:
{devices_block}

Recurring content series available (PRD §21) — recognizable branded formats
that build audience familiarity over time:
{series_block}{prior_series_block}

Generate exactly {n} DISTINCT candidate concepts. This is IDEATION only —
DO NOT write titles, hooks, captions, or any final copy. Just decide WHAT
each idea is, not HOW to say it yet.

For each concept, return:
- territory: one of the territory keys above (e.g. "A_PROBLEM")
- subject: one specific subject from that territory's list (or a close industry-specific variant)
- angle: one angle label from the list above, exactly as written
- creative_device: {{"category": one of story|visual|conversational|psychological|structural, "device": one device label from that category, exactly as written}}
- format_hint: one of image|carousel|video|product_video|ai_video|text — pick based on
  what the IDEA needs, not habit: carousel for a sequence/steps/comparison/story/several
  related insights; image for one single powerful idea, a statement, or a visual metaphor;
  video only where demonstration, personality, movement, or spoken delivery genuinely
  matters — not by default
- objective: one of reach|engagement|leads|sales|awareness
- audience_segment: which part of the audience this speaks to, 3-6 words — specific
  (e.g. "first-time buyers hesitant on price"), never "everyone" or "customers"
- concept_name: a short 3-6 word internal name for this idea (e.g. "The Upfront Cost Trap")
- series_name: OPTIONAL, null for most concepts. Only set this when the idea genuinely
  fits one of the series above (use its exact name). Across this whole batch, use at
  most 2-3 distinct series total, and only if you can genuinely assign the SAME series
  name to several different concepts — a series used exactly once isn't recurring,
  don't invent one just to fill this field.

No two concepts in this batch may share the same territory+subject+angle combination.
Never rely on historical engagement, trending topics, or search data — these
concepts must come purely from business/audience/brand/creative-framework
reasoning (nothing else exists in this task).

Return ONLY a valid JSON array of exactly {n} objects with exactly these 8 keys, nothing else."""
        # One retry on parse failure — was a bare try/except with no retry at
        # all, so a single malformed response silently dropped the whole
        # chunk (confirmed live: a framework-config bug made this fail
        # systematically; keeping one retry now as a general resilience
        # backstop, matching _generate_final_copy's pattern, not because
        # transient parse failures are expected to be common).
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                ai_request = AIService.build_ai_model(
                    messages=[{"role": "user", "content": prompt}], model="gpt-4o", temperature=1.0,
                )
                response = await AIService.chat_completion(ai_request)
                # Confirmed live (Oct 2026): AIService.chat_completion returns a
                # plain {"error": "..."} dict on failure (auth/rate-limit/quota),
                # not an exception or a normal response object — without this
                # check, the real error gets masked behind a useless
                # "'dict' object has no attribute 'choices'", which was the
                # actual cause of "no candidate concepts produced" shipping
                # with zero diagnostic value for a real, live failure.
                if isinstance(response, dict) and response.get("error"):
                    raise ValueError(response["error"])
                raw = response.choices[0].message.content.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                parsed = _loads_lenient(raw)
                if not isinstance(parsed, list):
                    raise ValueError(f"expected a JSON array, got {type(parsed)}")
                forbidden = {"title", "hook", "caption", "key_points", "description", "exact_copy"}
                for c in parsed:
                    if isinstance(c, dict):
                        for f in forbidden:
                            c.pop(f, None)
                        c["creative_device"] = _as_creative_device(c.get("creative_device"))
                return [c for c in parsed if isinstance(c, dict)]
            except Exception as exc:
                last_exc = exc
        print(f"[CalendarV2] candidate chunk {chunk_idx} failed after retry: {last_exc}", flush=True)
        return []

    results = await asyncio.gather(*[_one_chunk(i, size) for i, size in enumerate(chunk_sizes)])
    return [c for chunk in results for c in chunk]


# ── Step 4 — Score candidates (PRD §11-12, §37) ─────────────────────────────

_REQUIRED_SCORE_KEYS = (
    "strategic_relevance", "audience_relevance", "creative_strength", "distinctiveness",
    "brand_fit", "commercial_relevance", "asset_feasibility", "context_relevance", "repetition_risk",
)
_FORBIDDEN_SCORE_KEYS = {"performance_score", "google_trends_score", "engagement_score", "search_volume_score"}


async def _score_candidates(
    concepts: List[Dict[str, Any]],
    brand: Dict[str, Any],
    framework: Dict[str, Any],
    creative_memory: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Explicitly must NOT be influenced by performance or trend data — no
    such object is ever passed into this function. The returned score dict
    is built key-by-key from _REQUIRED_SCORE_KEYS only, so any
    performance_score/google_trends_score/etc a model might hallucinate in
    is structurally dropped, not merely instructed against — belt-and-
    suspenders on top of the prompt (PRD §12's explicit 'must NOT include').

    Chunked and run concurrently (was one giant unchunked call across every
    candidate) — confirmed live: at 100+ candidates in one call the model
    lost count and returned 124 scores for 102 concepts, and being fully
    sequential (not gathered like every other stage) made it a real,
    avoidable contributor to the 504 Gateway Timeout this pipeline was
    hitting overall."""
    if not concepts:
        return []
    brand_name = brand.get("brand_name") or "the brand"
    industry = brand.get("industry") or "business"
    prior_block = ""
    if creative_memory.get("concept_names"):
        prior_block = "\nRecently used concepts (penalize repetition_risk if similar):\n" + "\n".join(
            f"- {c}" for c in creative_memory["concept_names"][:30]
        )

    async def _score_chunk(chunk: List[Dict[str, Any]], offset: int) -> List[Any]:
        listing = "\n".join(
            f"{i}: territory={c.get('territory')}, subject={c.get('subject')}, angle={c.get('angle')}, "
            f"device={(c.get('creative_device') or {}).get('device')}, format_hint={c.get('format_hint')}, "
            f"concept_name={c.get('concept_name')}"
            for i, c in enumerate(chunk)
        )
        prompt = f"""Score these {len(chunk)} candidate content concepts for {brand_name}
({industry}).{prior_block}

{listing}

For EACH concept (by index), score 0-10 on exactly these 9 dimensions:
- strategic_relevance: does it support the business's real objectives?
- audience_relevance: does it matter to the actual target audience?
- creative_strength: is the idea interesting enough to stop the scroll?
- distinctiveness: does it feel different from the OTHER concepts in this list?
- brand_fit: does it make sense for this specific brand?
- commercial_relevance: can it meaningfully contribute to the business?
- asset_feasibility: can this business realistically produce it?
- context_relevance: any genuine date/seasonal tie-in relevance (0 if none)?
- repetition_risk: HIGH (near 10) if this closely resembles a recently-used concept above or another concept in this same list, LOW (near 0) if genuinely fresh.

Do NOT score based on historical engagement, trending topics, follower growth,
or search volume — none of that exists in this task and must never factor in.

Return ONLY a valid JSON array of exactly {len(chunk)} objects, index-aligned
(object 0 = concept 0, etc.), each with exactly these 9 numeric keys:
["strategic_relevance", "audience_relevance", "creative_strength", "distinctiveness",
"brand_fit", "commercial_relevance", "asset_feasibility", "context_relevance", "repetition_risk"]"""
        try:
            ai_request = AIService.build_ai_model(
                messages=[{"role": "user", "content": prompt}], model="gpt-4o", temperature=0.4,
            )
            response = await AIService.chat_completion(ai_request)
            if isinstance(response, dict) and response.get("error"):
                raise ValueError(response["error"])
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = _loads_lenient(raw)
            if isinstance(parsed, list) and len(parsed) == len(chunk):
                return parsed
            raise ValueError(f"expected {len(chunk)} scores, got {len(parsed) if isinstance(parsed, list) else type(parsed)}")
        except Exception as exc:
            print(f"[CalendarV2] candidate scoring chunk at offset {offset} failed ({exc}) — neutral scores for this chunk", flush=True)
            return [{} for _ in chunk]

    chunks = [concepts[i:i + CANDIDATE_CHUNK_SIZE] for i in range(0, len(concepts), CANDIDATE_CHUNK_SIZE)]
    chunk_results = await asyncio.gather(*[_score_chunk(c, i * CANDIDATE_CHUNK_SIZE) for i, c in enumerate(chunks)])
    scores: List[Any] = [s for chunk_scores in chunk_results for s in chunk_scores]

    scored: List[Dict[str, Any]] = []
    for concept, raw_score in zip(concepts, scores):
        clean_score: Dict[str, float] = {}
        for key in _REQUIRED_SCORE_KEYS:
            val = raw_score.get(key) if isinstance(raw_score, dict) else None
            clean_score[key] = float(val) if isinstance(val, (int, float)) else 5.0
        assert not (_FORBIDDEN_SCORE_KEYS & set(clean_score.keys())), "forbidden score key leaked in"
        clean_score["diversity_gain"] = 0.0  # computed at selection time, see _select_diverse_thirty
        scored.append({**concept, "selection_score": clean_score})
    return scored


# ── Step 5 — Select 30, diversity-optimized (PRD §13-15) ───────────────────

def _buckets_for_concept(c: Dict[str, Any]) -> set:
    """PRD §15's 9 minimum-creative-bucket requirements, mapped from
    territory/device combinations — deterministic, no LLM call needed."""
    buckets = set()
    territory = c.get("territory", "")
    device_category = (c.get("creative_device") or {}).get("category", "")
    if device_category == "story":
        buckets.add("story_driven")
    if territory == "E_PROOF":
        buckets.add("proof_social_proof")
    if territory == "G_PROCESS":
        buckets.add("behind_the_scenes_process")
    if territory == "A_PROBLEM":
        buckets.add("customer_problem")
    if territory == "K_ENTERTAINMENT" or device_category == "conversational":
        buckets.add("audience_interaction")
    if territory == "L_COMMERCIAL":
        buckets.add("commercial")
    if territory == "D_CONTRARIAN":
        buckets.add("opinion_contrarian")
    if device_category == "visual":
        buckets.add("visually_distinctive")
    if territory in ("K_ENTERTAINMENT", "C_CURIOSITY"):
        buckets.add("unexpected_experimental")
    return buckets


def _concept_base_score(c: Dict[str, Any]) -> float:
    s = c.get("selection_score") or {}
    return (
        s.get("strategic_relevance", 0) + s.get("audience_relevance", 0) + s.get("creative_strength", 0)
        + s.get("brand_fit", 0) + s.get("commercial_relevance", 0) + s.get("asset_feasibility", 0)
        - s.get("repetition_risk", 0) * 0.5
    )


def _similarity_overlap(candidate: Dict[str, Any], selected: List[Dict[str, Any]]) -> int:
    """Worst-case overlap against any already-selected item — the
    diversity_gain input from PRD §13's illustrative algorithm. Deliberately
    cheap/deterministic (territory/subject/angle/device/format overlap
    count), not embedding-based — see plan's documented deferral of
    embedding similarity as a fast-follow."""
    worst = 0
    c_device = (candidate.get("creative_device") or {}).get("device")
    for s in selected:
        overlap = sum([
            candidate.get("territory") == s.get("territory"),
            candidate.get("subject") == s.get("subject"),
            candidate.get("angle") == s.get("angle"),
            c_device == (s.get("creative_device") or {}).get("device"),
            candidate.get("format_hint") == s.get("format_hint"),
        ])
        worst = max(worst, overlap)
    return worst


def _select_diverse_thirty(scored_concepts: List[Dict[str, Any]], framework: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pick 30 optimizing strategic coverage + creative variety + business
    relevance + production feasibility — NOT simply top-scoring (PRD §13:
    'otherwise the system may select ten excellent but almost identical
    ideas'). Implements the PRD's illustrative iterative-maximize algorithm."""
    rules = framework["validation_rules"]
    target = rules["item_count"]
    max_commercial = max(1, round(target * rules["commercial_territory_max_share"]))

    pool = list(scored_concepts)
    selected: List[Dict[str, Any]] = []
    commercial_count = 0

    while len(selected) < target and pool:
        best_idx, best_val = None, float("-inf")
        for idx, c in enumerate(pool):
            if c.get("territory") == "L_COMMERCIAL" and commercial_count >= max_commercial:
                continue
            overlap = _similarity_overlap(c, selected)
            diversity_gain = max(0.0, 10.0 - overlap * 3.0)
            val = _concept_base_score(c) + diversity_gain
            if val > best_val:
                best_val, best_idx = val, idx
        if best_idx is None:
            # Commercial cap is blocking every remaining candidate — relax
            # rather than under-fill the plan.
            best_idx = max(range(len(pool)), key=lambda i: _concept_base_score(pool[i]))
        chosen = pool.pop(best_idx)
        chosen["selection_score"]["diversity_gain"] = round(best_val - _concept_base_score(chosen), 2)
        selected.append(chosen)
        if chosen.get("territory") == "L_COMMERCIAL":
            commercial_count += 1

    # Backfill minimum creative buckets (PRD §15) — swap the best unselected
    # candidate covering a missing bucket in for the currently-weakest
    # selected item.
    covered: set = set()
    for c in selected:
        covered |= _buckets_for_concept(c)
    for bucket, need in rules["min_creative_buckets"].items():
        if need <= 0 or bucket in covered or not selected:
            continue
        fix = max(
            (c for c in pool if bucket in _buckets_for_concept(c)),
            key=_concept_base_score, default=None,
        )
        if fix is None:
            continue
        weakest = min(selected, key=_concept_base_score)
        selected.remove(weakest)
        pool.append(weakest)
        pool.remove(fix)
        selected.append(fix)
        covered |= _buckets_for_concept(fix)

    return selected[:target]


# ── Step 6 — Assign dates (PRD §18) ─────────────────────────────────────────

def _assign_dates(
    selected: List[Dict[str, Any]], all_dates: List[str], holidays_by_date: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """A holiday/moment only stays attached to an item if the scoring stage
    already gave it genuine context_relevance — never a blind 'Happy
    [Holiday]' just because a date happens to be near one (PRD §18)."""
    out = []
    for day_index, concept in enumerate(selected):
        date_str = all_dates[day_index]
        item = {**concept, "day_index": day_index, "date": date_str}
        score = concept.get("selection_score") or {}
        if score.get("context_relevance", 0) >= 5 and date_str in holidays_by_date:
            item["holiday_tie_in"] = holidays_by_date[date_str]
        out.append(item)
    return out


# ── Step 6b — The brand's own key dates are guaranteed a post ─────────────────
# The plan prompt already asks for a post on each important date in the
# period, but that was only a request: a real plan on prod (Oct 2026) had the
# brand's "launch anniversary" on 5 November inside its period and gave that
# day an unrelated idea. So after dates are assigned, any key date in the
# period whose day isn't already about it has that day's concept turned into
# the key-date post, before the final copy is written.

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _parse_key_date(raw: str, years: List[int]) -> List[str]:
    """ISO dates for a key date: an exact date ("2026-11-05") as given, or a
    yearly one ("11-05", "5 November", "Nov 5") in each of `years`."""
    raw = (raw or "").strip().lower()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        return [f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"]
    month = day = None
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})", raw)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})|([a-z]{3})[a-z]*\.?\s+(\d{1,2})", raw)
        if m:
            if m.group(1):
                day, month = int(m.group(1)), _MONTHS.get(m.group(2))
            else:
                month, day = _MONTHS.get(m.group(3)), int(m.group(4))
    if not month or not day:
        return []
    out = []
    for y in years:
        try:
            out.append(datetime(y, month, day).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return out


def _brand_key_dates(brand: Dict[str, Any], all_dates: List[str]) -> List[Dict[str, str]]:
    """[{label, date}] for the brand's key dates that fall inside the plan.
    Reads the raw list ({label, date}) or to_brand_context's
    "Label (date), Label (date)" string."""
    raw = brand.get("key_dates")
    pairs: List[tuple] = []
    if isinstance(raw, list):
        for d in raw:
            if isinstance(d, dict):
                pairs.append((str(d.get("label") or d.get("name") or "").strip(), str(d.get("date") or "")))
    elif isinstance(raw, str) and raw.strip():
        for m in re.finditer(r"([^,(]+?)\s*\(([^)]+)\)", raw):
            pairs.append((m.group(1).strip(), m.group(2).strip()))
    if not all_dates:
        return []
    years = sorted({int(all_dates[0][:4]), int(all_dates[-1][:4])})
    window = set(all_dates)
    out, seen = [], set()
    for label, date_raw in pairs:
        if not label:
            continue
        for iso in _parse_key_date(date_raw, years):
            if iso in window and (label, iso) not in seen:
                seen.add((label, iso))
                out.append({"label": label, "date": iso})
    return out


def _is_about(item: Dict[str, Any], label: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", label.lower()) if len(w) > 3]
    text = " ".join(str(item.get(k) or "") for k in
                    ("subject", "concept_name", "creative_concept_name", "title", "angle")).lower()
    return bool(words) and sum(w in text for w in words) >= max(1, len(words) // 2)


def _enforce_key_dates(dated: List[Dict[str, Any]], brand: Dict[str, Any], all_dates: List[str]) -> List[Dict[str, str]]:
    """Make sure each key date in the plan's period has its post. Changes the
    concepts in place; returns the key dates that had to be placed."""
    placed = []
    by_date = {it["date"]: it for it in dated}
    for kd in _brand_key_dates(brand, all_dates):
        item = by_date.get(kd["date"])
        if not item or _is_about(item, kd["label"]):
            continue
        label = kd["label"]
        item.update({
            "subject": label,
            "angle": f"Mark {label} — what it means for the brand and the people it serves",
            "concept_name": label,
            "holiday_tie_in": {"name": label, "date": kd["date"], "source": "brand_key_date"},
            "key_date": kd,
        })
        placed.append(kd)
    return placed


# ── Step 7 — Assign format, dynamic 2-3 slide carousels (business rule) ───

def _pick_carousel_slide_count(concept: Dict[str, Any]) -> int:
    device_key = (concept.get("creative_device") or {}).get("device", "")
    return STRUCTURAL_DEVICE_SLIDE_HINT.get(device_key, 3)


def _assign_format(concept: Dict[str, Any], brand: Dict[str, Any], existing_assets_summary: str) -> Dict[str, Any]:
    """Per-idea format decision (PRD §17's worked examples as an explicit
    rule table), replacing the old fixed-3-carousels-only pre-assignment.
    Carousel eligibility is now an OUTCOME of this function, not a pre-
    picked slot list."""
    device_category = (concept.get("creative_device") or {}).get("category", "")
    device_key = (concept.get("creative_device") or {}).get("device", "")
    format_hint = concept.get("format_hint", "")
    has_product_photo = "product" in (existing_assets_summary or "").lower()

    if device_category == "story":
        fmt = "video"
    elif device_category == "structural" or format_hint == "carousel":
        fmt = "carousel"
    elif format_hint == "product_video" or (device_key == "product_hero" and has_product_photo):
        fmt = "product_video"
    elif format_hint == "ai_video":
        fmt = "ai_video"
    elif device_category == "conversational" and device_key == "talking_head":
        fmt = "video"
    elif format_hint in ("image", "video", "product_video", "ai_video", "text"):
        fmt = format_hint
    else:
        fmt = "image"

    result = {**concept, "format": fmt}
    if fmt == "carousel":
        result["carousel_slide_count"] = _pick_carousel_slide_count(concept)
    return result


# The Playbook's "Content formats" labels -> the calendar formats they allow.
# Labels with no calendar format (Poll, Live session) allow nothing extra.
_PLAYBOOK_FORMATS: Dict[str, Tuple[str, ...]] = {
    "reels / short video": ("video", "product_video", "ai_video"),
    "story": ("video", "product_video", "ai_video"),
    "carousel posts": ("carousel",),
    "static image": ("image",),
    "infographic": ("image",),
    "long-form article": ("text",),
    "thread / x post": ("text",),
}
# When a format has to change, the replacement is the least-used allowed one,
# ties broken in this order.
_FORMAT_FALLBACK_ORDER = ("image", "carousel", "video", "text", "product_video", "ai_video")


def _allowed_formats(brand: Dict[str, Any]) -> Optional[set]:
    """The calendar formats the brand chose in its Playbook, or None when it
    chose none we can plan for (the plan then picks freely, as before).
    Before this, V2 never read preferred_formats at all (v1 did), so a brand
    that picked "Carousel posts" + "Static image" still got a video every week."""
    allowed: set = set()
    for label in brand.get("preferred_formats") or []:
        allowed.update(_PLAYBOOK_FORMATS.get(str(label).strip().lower(), ()))
    return allowed or None


def _assign_formats(
    dated: List[Dict[str, Any]], brand: Dict[str, Any], existing_assets_summary: str,
) -> List[Dict[str, Any]]:
    """Runs _assign_format across the whole plan, enforcing
    MAX_VIDEO_FORMATS_PER_WEEK and MAX_CAROUSEL_FORMATS_PER_WEEK — business
    requirements. _assign_format decides each item's format independently
    with no awareness of any other item, so the weekly caps have to be
    enforced here: running per-week counters, downgrading to "image" once a
    week's video or carousel quota is already spent. A second, best-effort
    pass then tops up any week that ended with zero carousels (the business
    wants 1-2 a week, not a bare maximum) by upgrading one of that week's
    plain "image" items — never a video, never one already pinned to some
    other format by the brand's own allowed-formats list.
    Assumes `dated` is already in day_index order (it is — _assign_dates
    walks the plan sequentially)."""
    allowed = _allowed_formats(brand)
    video_cap = MAX_VIDEO_FORMATS_PER_WEEK
    if allowed and allowed & _VIDEO_FORMATS:
        video_cap = MAX_VIDEO_FORMATS_PER_WEEK_WHEN_CHOSEN
    carousel_cap = MAX_CAROUSEL_FORMATS_PER_WEEK
    video_count_by_week: Dict[int, int] = {}
    carousel_count_by_week: Dict[int, int] = {}
    used: Dict[str, int] = {}
    result = []
    for c in dated:
        assigned = _assign_format(c, brand, existing_assets_summary)
        week = c.get("day_index", 0) // 7
        fmt = assigned.get("format")
        video_full = video_count_by_week.get(week, 0) >= video_cap
        carousel_full = carousel_count_by_week.get(week, 0) >= carousel_cap
        over_quota = (fmt in _VIDEO_FORMATS and video_full) or (fmt == "carousel" and carousel_full)
        if (allowed and fmt not in allowed) or over_quota:
            if not allowed:
                options = ["image"]
            else:
                options = [f for f in _FORMAT_FALLBACK_ORDER
                           if f in allowed and not (f in _VIDEO_FORMATS and video_full)
                           and not (f == "carousel" and carousel_full)]
                if not options:   # only the quota-spent format is allowed this week
                    options = [f for f in _FORMAT_FALLBACK_ORDER if f in allowed]
            new_fmt = min(options, key=lambda f: (used.get(f, 0), _FORMAT_FALLBACK_ORDER.index(f)))
            assigned = {k: v for k, v in assigned.items() if k not in ("format", "carousel_slide_count")}
            assigned["format"] = new_fmt
            if new_fmt == "carousel":
                assigned["carousel_slide_count"] = _pick_carousel_slide_count(c)
            fmt = new_fmt
        if fmt in _VIDEO_FORMATS:
            video_count_by_week[week] = video_count_by_week.get(week, 0) + 1
        elif fmt == "carousel":
            carousel_count_by_week[week] = carousel_count_by_week.get(week, 0) + 1
        used[fmt] = used.get(fmt, 0) + 1
        result.append(assigned)

    if allowed is None or "carousel" in allowed:
        weeks_present = {c.get("day_index", 0) // 7 for c in dated}
        for week in weeks_present:
            if carousel_count_by_week.get(week, 0) >= MIN_CAROUSEL_FORMATS_PER_WEEK:
                continue
            for item in reversed(result):
                if (item.get("day_index", 0) // 7) != week:
                    continue
                if item.get("format") != "image":
                    continue
                item.pop("carousel_slide_count", None)
                item["format"] = "carousel"
                item["carousel_slide_count"] = _pick_carousel_slide_count(item)
                carousel_count_by_week[week] = carousel_count_by_week.get(week, 0) + 1
                break
    return result


# ── Ad-opportunity scoring (rule-based, PRD §19 — performance-free) ─────────

def _score_ad_opportunity(item: Dict[str, Any], near_holiday: bool, has_active_promo: bool) -> float:
    """The old version's avg_engagement_by_topic substring-match branch —
    the one place this function directly touched performance data for a
    selection-adjacent decision — is removed outright. Signals now: is this
    idea Commercial-territory, how commercially relevant did scoring already
    rate it, is there an active promo it could carry, is a real date nearby,
    does it already have a CTA."""
    score = 0.0
    if item.get("territory") == "L_COMMERCIAL":
        score += 40.0
    selection_score = item.get("selection_score") or {}
    score += min(selection_score.get("commercial_relevance", 0) * 3.0, 30.0)
    if has_active_promo and item.get("territory") == "L_COMMERCIAL":
        score += 10.0
    if near_holiday and item.get("territory") in ("L_COMMERCIAL", "A_PROBLEM", "H_COMPARISON"):
        score += 10.0
    if item.get("cta"):
        score += 10.0
    return min(round(score, 1), 100.0)


async def _write_calendar_ad_copy(
    item: Dict[str, Any],
    brand: Dict[str, Any],
    angle: str,
) -> AdCopyV2:
    """LLM-generated ad copy adapting an organic calendar item for paid use.
    Mirrors jane_ads/creative.py's write_ad_copy() two-zone-prompt shape
    (context-free MESSAGE zone + a post-generation self-check), simplified
    for this context (adapting an existing organic idea, not building a
    fresh WhatsApp-click campaign)."""
    brand_name = brand.get("brand_name") or "the brand"
    usp = brand.get("unique_selling_proposition") or ""
    cta_preference = (brand.get("cta_styles") or [""])[0]
    business_pulse = brand.get("business_pulse") or {}
    promo_lines = [v for v in [
        ("Active promotion: " + "; ".join(business_pulse.get("current_promotions") or [])) if business_pulse.get("current_promotions") else "",
        ("Active campaign: " + "; ".join(business_pulse.get("current_campaigns") or [])) if business_pulse.get("current_campaigns") else "",
    ] if v]
    promo_block = ("\n" + "\n".join(promo_lines)) if promo_lines else ""

    prompt = f"""Adapt this organic social post idea into paid-ad copy for {brand_name}.

Organic idea: "{item.get('title', '')}"
Hook: "{item.get('hook', '')}"
Key points: {', '.join(str(p) for p in (item.get('key_points') or [])[:4])}
{f'USP: {usp}' if usp else ''}{promo_block}

If an active promotion/campaign above is genuinely relevant to this specific
idea, name its real terms — do not paraphrase it into something vague like
"exclusive promotions today". If none is relevant here, don't force one in.

Required angle: {angle.replace('_', ' ')} — the ad copy MUST lead with this angle,
not just restate the organic hook.

Write:
- headline: max 8 words, punchy, ad-native (not the same as the organic hook)
- primary_text: 2-3 sentences, ad-native — front-load the value/hook in the
  first line since paid placements get less attention than organic
- short_copy: a compressed 1-sentence version for square/story placements
- cta: a single short action phrase{f' (prefer something like "{cta_preference}" if it fits)' if cta_preference else ''}
- image_prompt: 1 sentence describing the ideal ad visual, informed by the
  organic idea's subject matter

Never invent a specific statistic, testimonial, discount amount, or customer
count that wasn't given above — if you'd need one to make the copy work,
write around it instead (ad-safety rule: no fabricated claims).

Return ONLY valid JSON: {{"headline": "...", "primary_text": "...", "short_copy": "...", "cta": "...", "image_prompt": "..."}}
"""
    try:
        ai_request = AIService.build_ai_model(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o",
            temperature=0.8,
        )
        response = await AIService.chat_completion(ai_request)
        if isinstance(response, dict) and response.get("error"):
            raise ValueError(response["error"])
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = _loads_lenient(raw)
        return AdCopyV2(**{k: data.get(k, "") for k in ("headline", "primary_text", "short_copy", "cta", "image_prompt")})
    except Exception as exc:
        print(f"[CalendarV2] ad copy generation failed: {exc}", flush=True)
        return AdCopyV2()


# ── Generic AI words: last pass ────────────────────────────────────────────
# Banning them in the prompt and rewriting failed items wasn't enough: on
# staging (Oct 2026) the model still wrote "discover" 21 times and "journey"
# 12 across a plan. So after everything else, every sentence that still uses
# one is sent back on its own to be reworded, and anything that survives
# that gets a plain-word swap in code. Covers what readers see and what the
# draft writer is fed (caption_direction, key_points).

_SCRUB_FIELDS = ("title", "hook", "description", "cta", "caption_direction")

# Fallback swaps: (pattern, replacement). Case of the first letter is kept.
_PLAIN_WORDS: List[Tuple[str, str]] = [
    (r"\bdeep dive\b", "close look"), (r"\bdive into\b", "look at"),
    (r"\bnext level\b", "further"), (r"\bin today's\b", "in the current"),
    (r"\bnavigate the\b", "handle the"),
    (r"\bgame-?changing\b", "big"), (r"\bgame-?changers?\b", "big change"),
    (r"\btransformations?\b", "change"), (r"\btransformative\b", "big"),
    (r"\btransformed\b", "changed"), (r"\btransforming\b", "changing"),
    (r"\btransforms\b", "changes"), (r"\btransform\b", "change"),
    (r"\bdiscovery\b", "finding"), (r"\bdiscoveries\b", "findings"),
    (r"\bdiscovered\b", "found"), (r"\bdiscovering\b", "finding"),
    (r"\bdiscovers\b", "finds"), (r"\bdiscover\b", "find"),
    (r"\bjourneys\b", "paths"), (r"\bjourney\b", "path"),
    (r"\bunlocked\b", "opened"), (r"\bunlocking\b", "opening"),
    (r"\bunlocks\b", "opens"), (r"\bunlock\b", "open"),
    (r"\bseamlessly\b", "smoothly"), (r"\bseamless\b", "smooth"),
    (r"\beffortlessly\b", "easily"), (r"\beffortless\b", "easy"),
    (r"\belevated\b", "raised"), (r"\belevating\b", "raising"),
    (r"\belevates\b", "raises"), (r"\belevate\b", "raise"),
    (r"\bempowered\b", "helped"), (r"\bempowering\b", "helping"),
    (r"\bempowerment\b", "support"), (r"\bempowers\b", "helps"), (r"\bempower\b", "help"),
    (r"\bunleashed\b", "released"), (r"\bunleashing\b", "releasing"),
    (r"\bunleashes\b", "releases"), (r"\bunleash\b", "release"),
    (r"\bsupercharged\b", "sped up"), (r"\bsupercharging\b", "speeding up"),
    (r"\bsupercharges\b", "speeds up"), (r"\bsupercharge\b", "speed up"),
    (r"\bharnessed\b", "used"), (r"\bharnessing\b", "using"),
    (r"\bharnesses\b", "uses"), (r"\bharness\b", "use"),
    (r"\bdelved\b", "dug"), (r"\bdelving\b", "digging"), (r"\bdelves\b", "digs"), (r"\bdelve\b", "dig"),
    (r"\bskyrocketed\b", "grew fast"), (r"\bskyrocketing\b", "growing fast"),
    (r"\bskyrockets\b", "grows fast"), (r"\bskyrocket\b", "grow fast"),
    (r"\brevolutioni[sz]ed\b", "changed"), (r"\brevolutioni[sz]ing\b", "changing"),
    (r"\brevolutioni[sz]es\b", "changes"), (r"\brevolutioni[sz]e\b", "change"),
    (r"\brevolutionary\b", "new"), (r"\brevolutions?\b", "shift"),
]


def _plain_words(text: str) -> str:
    def keep_case(m, word):
        return word[:1].upper() + word[1:] if m.group(0)[:1].isupper() else word
    for pat, word in _PLAIN_WORDS:
        text = re.sub(pat, lambda m, w=word: keep_case(m, w), text, flags=re.I)
    return text


def _scrub_slots(item: Dict[str, Any]):
    """(getter, setter) for each copy field of an item that the scrub covers."""
    slots = []
    for k in _SCRUB_FIELDS:
        if isinstance(item.get(k), str):
            slots.append((lambda d=item, k=k: d[k], lambda v, d=item, k=k: d.__setitem__(k, v)))
    for container, keys in ((item.get("exact_copy"), ("headline", "caption")),
                            (item.get("video_idea"), ("hook", "cta"))):
        if isinstance(container, dict):
            for k in keys:
                if isinstance(container.get(k), str):
                    slots.append((lambda d=container, k=k: d[k], lambda v, d=container, k=k: d.__setitem__(k, v)))
    for lst in (item.get("key_points"), (item.get("video_idea") or {}).get("talking_points")
                if isinstance(item.get("video_idea"), dict) else None):
        if isinstance(lst, list):
            for i, v in enumerate(lst):
                if isinstance(v, str):
                    slots.append((lambda l=lst, i=i: l[i], lambda v, l=lst, i=i: l.__setitem__(i, v)))
    for slide in ((item.get("carousel") or {}).get("slides") or []) if isinstance(item.get("carousel"), dict) else []:
        if isinstance(slide, dict):
            for k in ("headline", "body"):
                if isinstance(slide.get(k), str):
                    slots.append((lambda d=slide, k=k: d[k], lambda v, d=slide, k=k: d.__setitem__(k, v)))
    return slots


async def _scrub_generic_words(items: List[Dict[str, Any]]) -> int:
    """Rewords every text in the plan that still uses a generic AI word.
    Returns how many texts were changed. Never raises.

    Confirmed live (Oct 2026): a real plan's own stored caption shipped
    "...Uri Social's innovative solutions" — "innovative" is banned
    everywhere else (CaptionValidatorService.BANNED_WORDS, the list the
    draft pipeline enforces with a hard, no-model-involved guarantee via
    brand_brief.enforce()) but this plan-level scrub only ever checked
    GENERIC_AI_WORDS, a narrower 20-word list. The user's own priority on
    this (explicit): these words "should not be seen at all" — not just at
    draft time, at the plan-preview stage too, since that's what's shown
    before a draft is ever created. Checks the full union now, and
    strip_banned_words() is the same zero-model guarantee the draft
    pipeline already has, applied here as the final backstop."""
    from app.agents.social_media_manager.services.brand_brief import find_banned, strip_banned_words
    from app.agents.social_media_manager.services.caption_validator_service import CaptionValidatorService

    all_banned_words = list(dict.fromkeys(GENERIC_AI_WORDS + CaptionValidatorService.BANNED_WORDS))

    todo = []
    for item in items:
        for get, put in _scrub_slots(item):
            if find_banned(get(), all_banned_words):
                todo.append((get, put))
    if not todo:
        return 0

    async def reword(batch):
        texts = {str(i): get() for i, (get, _) in enumerate(batch)}
        prompt = (
            "Reword each text below so it uses none of these words in any form "
            f"(\"transforms\", \"discovering\" count too): {', '.join(all_banned_words)}.\n"
            "Say the specific, plain thing instead. Keep the meaning, length, tone, language, "
            "line breaks, emojis and hashtags. Change nothing else.\n\n"
            "Return ONLY valid JSON mapping each id to its reworded text.\n\n"
            + json.dumps(texts, ensure_ascii=False)
        )
        try:
            ai_request = AIService.build_ai_model(
                messages=[{"role": "user", "content": prompt}], model="gpt-4o", temperature=0.4,
            )
            response = await AIService.chat_completion(ai_request)
            if isinstance(response, dict) and response.get("error"):
                raise ValueError(response["error"])
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            out = _loads_lenient(raw)
            return out if isinstance(out, dict) else {}
        except Exception as exc:
            print(f"[CalendarV2] generic-word reword failed: {exc}", flush=True)
            return {}

    batches = [todo[i:i + 30] for i in range(0, len(todo), 30)]
    results = await asyncio.gather(*[reword(b) for b in batches])
    changed = 0
    for batch, out in zip(batches, results):
        for i, (get, put) in enumerate(batch):
            new = out.get(str(i))
            text = new if isinstance(new, str) and new.strip() else get()
            if find_banned(text, all_banned_words):
                text = _plain_words(text)
            # Absolute guarantee, same as the draft pipeline: whatever the
            # reword did or didn't catch, no banned word survives this.
            # GENERIC_AI_WORDS has two entries strip_banned_words()'s own
            # default list doesn't ("game-changing", "in today's") — pass
            # them explicitly so the guarantee covers the same union this
            # function detects on, not a subset of it.
            text = strip_banned_words(text, GENERIC_AI_WORDS)
            if text != get():
                put(text)
                changed += 1
    return changed


# ── Creative diversity + anti-boring validation (PRD §14, §29-30) ──────────

def _rule_based_diversity_issues(items: List[Dict[str, Any]]) -> Dict[int, str]:
    """Deterministic half: duplicate/near-duplicate TITLE openings (titles
    are the field the model is explicitly instructed to keep unique — hooks
    aren't, confirmed live: comparing hooks flagged 29/30 items on a real
    run because devices/angles legitimately recur across 30 days), and
    genuinely repeated territory+subject+angle on adjacent days (a much
    stronger, more specific signal than the old content_type+format pair
    the earlier version compared).

    Also covers 2 of PRD §14's checks that had no implementation before this
    pass: repeated CTAs and repeated visual concepts/metaphors. Both are
    deliberately looser than the title/territory checks above, for the same
    reason hooks were dropped from strict adjacent-pair comparison — a CTA
    like "Shop Now" or "Send Message" legitimately recurring isn't a defect
    on its own, PRD §14 calls out CTAs being *overused* specifically. So CTA
    repetition only flags when one CTA dominates the whole batch (>40% of
    items) rather than any two-in-a-row match, while visual concepts (long,
    specific sentences — much lower legitimate-recurrence risk than a short
    CTA) reuse the same adjacent-opening-words check as titles."""
    issues: Dict[int, str] = {}
    seen_openings: Dict[str, int] = {}
    seen_visual_openings: Dict[str, int] = {}
    cta_counts: Dict[str, int] = {}
    for i, item in enumerate(items):
        title = str(item.get("title") or "").strip().lower()
        opening = " ".join(title.split()[:5])
        if opening and opening in seen_openings:
            issues[i] = f"title opens the same way as day {seen_openings[opening]}"
        elif opening:
            seen_openings[opening] = i
        if i > 0:
            prev = items[i - 1]
            if (item.get("territory") == prev.get("territory")
                    and item.get("subject") == prev.get("subject")
                    and item.get("angle") == prev.get("angle")):
                issues[i] = issues.get(i, "") + "; repeats prior day's territory+subject+angle"
        visual_idea = str((item.get("creative_direction") or {}).get("central_visual_idea") or "").strip().lower()
        visual_opening = " ".join(visual_idea.split()[:6])
        if visual_opening and visual_opening in seen_visual_openings:
            issues[i] = issues.get(i, "") + f"; visual concept repeats day {seen_visual_openings[visual_opening]}"
        elif visual_opening:
            seen_visual_openings[visual_opening] = i
        cta = str(item.get("cta") or "").strip().lower()
        if cta:
            cta_counts[cta] = cta_counts.get(cta, 0) + 1

    if items:
        dominant_cta, dominant_count = max(cta_counts.items(), key=lambda kv: kv[1], default=(None, 0))
        if dominant_cta and dominant_count > max(3, int(len(items) * 0.4)):
            for i, item in enumerate(items):
                if str(item.get("cta") or "").strip().lower() == dominant_cta:
                    issues[i] = issues.get(i, "") + f'; CTA "{item.get("cta")}" overused ({dominant_count}/{len(items)} items)'
    return issues


async def _llm_diversity_check(items: List[Dict[str, Any]], brand: Optional[Dict[str, Any]] = None) -> List[int]:
    """One LLM call across all items' titles/hooks doing two jobs at once —
    kept as a single call deliberately, not split into two round-trips,
    for the same latency reasons documented on _generate_final_copy (this
    pipeline is already tight against the gateway timeout):

    1. Duplication — which pairs are substantially the same idea reworded
       (a cheap stand-in for embedding-based semantic similarity, deferred
       as a fast-follow).
    2. URI Content Calendar Generation addendum §19's "No Generic AI
       Content" quality gate — the 3 questions here that genuinely need
       semantic judgment (could this headline appear on 1,000 unrelated
       pages; does the idea use something specific from this brand's
       Playbook; would the actual customer recognise themselves). The
       addendum's other 4 questions (text density, visual-communicates-
       idea, visual-guide-compliance, scroll-stopping) are handled at
       generation time via explicit prompt rules in _generate_final_copy
       instead — better fixed at the source than re-judged after the fact."""
    brand = brand or {}
    brand_name = brand.get("brand_name") or "this business"
    context_line = ""
    psych_keys = ["customer_pain_points", "customer_needs", "customer_desires", "customer_objections"]
    psych_bits = [str(v) for k in psych_keys for v in (brand.get(k) or [])][:6]
    if psych_bits:
        context_line = f"\n{brand_name}'s actual customer reality includes: " + "; ".join(psych_bits)

    listing = "\n".join(
        f"{i}: {it.get('title', '')} — {it.get('hook', '')} — "
        f"{((it.get('exact_copy') or {}).get('caption') or '')[:140]}"
        for i, it in enumerate(items)
    )
    prompt = f"""Below are {len(items)} social media post ideas for {brand_name}.{context_line}

{listing}

Flag an index for regeneration if ANY of these is true:
A) DUPLICATE — substantially the SAME underlying idea reworded as another
   index in the list (not just sharing a territory — genuinely the same
   angle/message).
B) GENERIC — the idea fails ANY of these: (1) this exact headline could
   appear on 1,000 unrelated business pages with zero changes, (2) it does
   not clearly draw on anything specific to this business's actual
   customers (their real pain points, needs, objections, or reasons they
   buy — not just the product category), (3) this business's actual
   customer would not recognise themselves in it.
C) CRUTCH WORD — this item leans on the same go-to benefit word or phrase
   (e.g. "simplify"/"simplifies", "boost", "effortless", "seamless") that
   keeps reappearing across several OTHER items in this list as their
   payoff word, instead of each idea finding its own specific language.
   One or two items sharing a word is normal; a word becoming the list's
   default answer is the problem — read the whole list first to judge this,
   not any single item alone. Do NOT flag ordinary topical words that
   legitimately recur because they're what the business actually is
   (the brand name, "marketing", the industry, the audience) — only a
   lazy, interchangeable benefit word standing in for real specificity.

Return ONLY a JSON array of indexes to regenerate, e.g. [4, 11] or [] if none
qualify. Be conservative — only flag a clear case, not a borderline one.
"""
    try:
        ai_request = AIService.build_ai_model(
            messages=[{"role": "user", "content": prompt}], model="gpt-4o", temperature=0.3,
        )
        response = await AIService.chat_completion(ai_request)
        if isinstance(response, dict) and response.get("error"):
            raise ValueError(response["error"])
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = _loads_lenient(raw)
        return [i for i in parsed if isinstance(i, int) and 0 <= i < len(items)]
    except Exception as exc:
        print(f"[CalendarV2] LLM diversity+quality check failed (non-fatal): {exc}", flush=True)
        return []


def _anti_boring_check(items: List[Dict[str, Any]], brand_name: str = "") -> Dict[int, str]:
    """PRD §30 — flags generic AI phrasings for a creative-quality-review
    NOTE, never an auto-reject (the execution can still redeem a generic
    opener)."""
    brand_lower = (brand_name or "").strip().lower()
    flagged: Dict[int, str] = {}
    for i, item in enumerate(items):
        title = str(item.get("title", ""))
        text = " ".join([
            title, str(item.get("hook", "")),
            str((item.get("exact_copy") or {}).get("caption", "")),
        ]).lower()

        opener_match = _ANTI_BORING_OPENER_RE.search(title)
        if opener_match:
            flagged[i] = f'generic AI-sounding headline opener: "{opener_match.group(1)}..." — verify the execution redeems it'
            continue

        for phrase in ANTI_BORING_PHRASES:
            # Confirmed live: "at {brand}, we believe".split("{brand}")[0].strip()
            # reduces to the bare word "at" — a substring present in nearly any
            # English sentence ("later", "natural", "that", ...), so this fired
            # on almost every item regardless of content. Substitute the real
            # brand name and check for the actual resulting phrase; skip the
            # check entirely (rather than false-positive on "at") when there's
            # no brand name to substitute.
            if "{brand}" in phrase:
                if not brand_lower:
                    continue
                check = phrase.replace("{brand}", brand_lower)
            else:
                check = phrase
            if check and check in text:
                flagged[i] = f'generic phrasing detected: "{phrase}" — verify the execution redeems it'
                break
    return flagged


def _clamp_carousel(idea: Dict[str, Any], target: int) -> None:
    """PRD §17/§28/§53 — 2-3 slides is a HARD rule that code enforces, not a
    hint we hope the model follows (confirmed live: it produced 7-slide
    carousels that then got accepted best-effort after the retry loop gave
    up). Capped at 3, not the PRD document's original 5, per explicit
    business direction (Oct 2026): carousels should stay short. Trims
    anything over 3 to `target`, keeping the first target-1 slides plus the
    last (which carries the CTA in the PRD's slide structures), then
    re-indexes. Under 2 is genuinely broken — left alone for validation to
    flag, it's rare."""
    carousel = idea.get("carousel")
    if not isinstance(carousel, dict):
        return
    slides = carousel.get("slides")
    if not isinstance(slides, list) or len(slides) <= 3:
        return
    keep_n = target if 2 <= target <= 3 else 3
    trimmed = slides[: keep_n - 1] + [slides[-1]]
    for new_idx, s in enumerate(trimmed):
        if isinstance(s, dict):
            s["slide_index"] = new_idx
    carousel["slides"] = trimmed


def _clamp_caption(idea: Dict[str, Any]) -> None:
    """Same spirit as _clamp_carousel: a word-count cap the model is told
    about in the prompt and pushed on via the retry loop, but neither is a
    guarantee, so code enforces it as a last resort. Trims to the last
    complete sentence that still fits within MAX_CAPTION_WORDS words rather
    than cutting mid-sentence; if no sentence boundary exists within the
    limit (one long run-on), cuts cleanly at the word boundary instead."""
    exact_copy = idea.get("exact_copy")
    if not isinstance(exact_copy, dict):
        return
    caption = str(exact_copy.get("caption") or "")
    words = caption.split()
    if len(words) <= MAX_CAPTION_WORDS:
        return
    truncated = " ".join(words[:MAX_CAPTION_WORDS])
    last_sentence_end = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
    exact_copy["caption"] = truncated[: last_sentence_end + 1] if last_sentence_end > 0 else truncated


_TEXT_FIELDS_FOR_SCANNING = (
    "title", "hook", "description", "caption_direction", "cta", "topic",
    "promised_business_outcome", "creative_concept_name", "central_visual_idea",
    "design_style", "layout_direction", "visual_metaphor", "designer_execution_notes",
    "reasoning",
)


def _collect_item_text(idea: Dict[str, Any]) -> str:
    """Flattens every copy-bearing field of a generated item into one string
    — used both for the words-to-avoid guardrail check and could serve any
    future whole-item text scan. Deliberately excludes structural fields
    (territory/format/etc.) that aren't shown to the end reader."""
    parts = [str(idea.get(k) or "") for k in _TEXT_FIELDS_FOR_SCANNING]
    parts.extend(str(p) for p in (idea.get("key_points") or []))
    parts.extend(str(k) for k in (idea.get("keywords") or []))
    video_idea = idea.get("video_idea") or {}
    if isinstance(video_idea, dict):
        parts.append(str(video_idea.get("hook") or ""))
        parts.append(str(video_idea.get("cta") or ""))
        parts.extend(str(p) for p in (video_idea.get("talking_points") or []))
        parts.extend(str(s) for s in (video_idea.get("scenes") or []))
    exact_copy = idea.get("exact_copy") or {}
    if isinstance(exact_copy, dict):
        parts.append(str(exact_copy.get("headline") or ""))
        parts.append(str(exact_copy.get("caption") or ""))
        parts.extend(str(h) for h in (exact_copy.get("hashtags") or []))
    carousel = idea.get("carousel") or {}
    if isinstance(carousel, dict):
        for slide in (carousel.get("slides") or []):
            if isinstance(slide, dict):
                parts.append(str(slide.get("headline") or ""))
                parts.append(str(slide.get("body") or ""))
    return " \n ".join(p for p in parts if p)


def _all_banned_words(brand: Dict[str, Any]) -> List[str]:
    """Words to avoid AND the guardrails' banned words — the Playbook has both,
    and only the first list was being checked here."""
    from app.agents.social_media_manager.services.brand_brief import banned_words
    return banned_words(brand)


def _find_banned_words(idea: Dict[str, Any], words_to_avoid: List[str]) -> List[str]:
    """The brand's words_to_avoid was only ever a prompt instruction (soft
    guidance the model can and does ignore under pressure to hit other
    requirements) — confirmed live: "guaranteed" survived into a caption for
    a brand that explicitly banned it. Per the PRD's own rule (§53 — code
    enforces hard constraints, the LLM handles creative judgment), this
    makes it a deterministic, case-insensitive check instead, wired into the
    same validation-failure → regenerate loop everything else here uses."""
    if not words_to_avoid:
        return []
    # Same matcher every other writer uses: catches other forms of a word
    # ("streamlining" for "streamline") without flagging "book" for "ok".
    from app.agents.social_media_manager.services.brand_brief import find_banned
    return [str(w).strip() for w in find_banned(_collect_item_text(idea), words_to_avoid)]


def _reader_text(idea: Dict[str, Any]) -> str:
    """What the audience actually reads: title, hook, description, CTA, the
    post's headline/caption and slides, the video's script. Internal notes
    (reasoning, designer notes) aren't held to the generic-word ban."""
    parts = [str(idea.get(k) or "") for k in ("title", "hook", "description", "cta")]
    exact_copy = idea.get("exact_copy") or {}
    if isinstance(exact_copy, dict):
        parts += [str(exact_copy.get("headline") or ""), str(exact_copy.get("caption") or "")]
    carousel = idea.get("carousel") or {}
    if isinstance(carousel, dict):
        for slide in (carousel.get("slides") or []):
            if isinstance(slide, dict):
                parts += [str(slide.get("headline") or ""), str(slide.get("body") or "")]
    video_idea = idea.get("video_idea") or {}
    if isinstance(video_idea, dict):
        parts += [str(video_idea.get("hook") or ""), str(video_idea.get("cta") or "")]
        parts += [str(x) for x in (video_idea.get("talking_points") or [])]
    return " \n ".join(p for p in parts if p)


def _generic_words_used(idea: Dict[str, Any]) -> List[str]:
    from app.agents.social_media_manager.services.brand_brief import find_banned
    return [str(w) for w in find_banned(_reader_text(idea), GENERIC_AI_WORDS)]


def _fabricated_claims_used(idea: Dict[str, Any], brand: Dict[str, Any]) -> List[str]:
    """PRD §31 — must never fabricate a statistic, named testimonial/citation,
    price, discount, or guarantee. Until now this was prompt-only
    (_generate_final_copy's "never fabricate... write around missing
    specifics" instruction) — the same risk class the banned-word list was
    before brand_brief.enforce() existed: an instruction a model can still
    miss under pressure to hit everything else asked of it, not a guarantee.
    Reuses the exact deterministic detector single-post generation already
    relies on in production (unverified_numbers/unverified_sources), checked
    against everything the writer actually had available: the brand profile
    itself (so a real price_range or business_pulse figure isn't flagged)
    plus any real Listening evidence attached to this specific item."""
    from app.agents.social_media_manager.services.brand_brief import unverified_numbers, unverified_sources
    text = _reader_text(idea)
    sources = (json.dumps(brand, default=str, ensure_ascii=False) + " "
               + json.dumps(idea.get("listening_brief") or {}, default=str, ensure_ascii=False))
    return [f'number: "{p}"' for p in unverified_numbers(text, sources)] + \
           [f'citation: "{s}"' for s in unverified_sources(text, sources)]


def _validate_item_v2(
    idea: Dict[str, Any],
    is_carousel: bool,
    expected_slides: int = 3,
    words_to_avoid: Optional[List[str]] = None,
) -> List[str]:
    """Extends v1's _validate_day (hard deterministic rules, PRD §28) with
    V2's own required fields. Carousel check enforces the business's HARD
    2-3 rule, not the exact per-concept target — the target is a hint for
    the model, 2-3 is the constraint. _clamp_carousel already trims anything
    over 3 before this runs, so this mostly catches under-2."""
    issues = _validate_day(idea)
    if not str(idea.get("ai_image_prompt") or "").strip():
        issues.append("ai_image_prompt is empty")
    if not str(idea.get("reasoning") or "").strip():
        issues.append("reasoning is empty")
    if not idea.get("primary_kpi"):
        issues.append("primary_kpi is empty")
    if not str(idea.get("creative_concept_name") or "").strip():
        issues.append("creative_concept_name is empty")
    if is_carousel:
        slides = ((idea.get("carousel") or {}).get("slides")) or []
        if not (2 <= len(slides) <= 3):
            issues.append(f"carousel must have 2-3 slides (business hard rule), got {len(slides)}")
    caption = str((idea.get("exact_copy") or {}).get("caption") or "")
    caption_words = len(caption.split())
    if caption_words > MAX_CAPTION_WORDS:
        issues.append(
            f"caption is too long ({caption_words} words, max {MAX_CAPTION_WORDS}) — "
            f"cut it down to one strong line or two, not a paragraph"
        )
    banned_hits = _find_banned_words(idea, words_to_avoid or [])
    if banned_hits:
        issues.append(
            f"uses word(s)/phrase(s) the brand asked to avoid: {', '.join(banned_hits)} — rewrite without them"
        )
    generic_hits = _generic_words_used(idea)
    if generic_hits:
        issues.append(
            f"uses generic AI word(s): {', '.join(generic_hits)} — rewrite those lines in plain, specific words"
        )
    return issues


# ── Step 8+9 — Final copy + creative direction (PRD §37-38) ────────────────

async def _generate_final_copy(
    brand: Dict[str, Any],
    concepts_chunk: List[Dict[str, Any]],   # each already has territory/subject/angle/device/format/date/day_index
    platforms: List[str],
    existing_assets_summary: str,
    force: bool = False,
) -> List[Dict[str, Any]]:
    """PRD Step8 (final copy) and Step9 (creative direction) share ONE
    chunked network call here rather than two — a deliberate simplification
    documented in the plan: this pipeline already makes 3x the LLM calls the
    old single-stage engine did (candidates -> score -> final copy), and
    splitting copy/direction into a 4th sequential stage would meaningfully
    worsen the exact proxy-timeout problem this codebase already fought hard
    to fix (see generate_plan_v2's concurrency comments). The PRD's real
    requirement — copy generation must not reinterpret the approved concept
    — is satisfied by fixing territory/subject/angle/device/format as given
    inputs the model is told not to touch, not by forcing a second round-trip."""
    n = len(concepts_chunk)
    brand_name = brand.get("brand_name") or "the brand"
    industry = brand.get("industry") or "business"
    voice = brand.get("brand_voice") or "professional and engaging"
    audience = brand.get("target_audience") or "general audience"
    platforms_str = ", ".join(platforms) if platforms else "social media"
    tagline = brand.get("tagline", "")
    description = brand.get("business_description", "")
    region = brand.get("region", "")
    usp = brand.get("unique_selling_proposition", "")
    price_range = brand.get("price_range", "")
    business_pulse = brand.get("business_pulse") or {}
    business_pulse_updated_at = brand.get("business_pulse_updated_at")
    psychology_block = _customer_psychology_block(brand) + _brand_rules_block(brand)
    visual_guide_block = _visual_guide_block(brand)

    bp_freshness = _business_pulse_freshness_str(business_pulse_updated_at)
    bp_lines = [v for v in [
        business_pulse.get("current_period_goal"),
        ", ".join(business_pulse.get("current_promotions") or []),
        ", ".join(business_pulse.get("current_campaigns") or []),
        ", ".join(business_pulse.get("new_products_services") or []),
        ", ".join(business_pulse.get("recent_milestones") or []),
    ] if v]
    business_pulse_block = ""
    if bp_lines:
        freshness_note = f" ({bp_freshness})" if bp_freshness else ""
        business_pulse_block = f"Current business pulse{freshness_note}: " + "; ".join(bp_lines)

    assets_block = (
        f"\nExisting assets available (prefer reusing over requesting new production): {existing_assets_summary}"
        if existing_assets_summary else ""
    )
    force_token = f"\n[Regen token: {secrets.token_hex(6)}]\n" if force else ""

    def _fmt_line(c: Dict[str, Any]) -> str:
        if c.get("format") == "carousel":
            return f"CAROUSEL — ~{c.get('carousel_slide_count', 3)} slides (2-3 allowed)"
        return str(c.get("format", "image"))

    def _listening_line(c: Dict[str, Any]) -> str:
        # This concept isn't a brainstormed idea — it's grounded in something
        # real: either a Listening opportunity measured in this business's
        # own market, or a real post this business already published being
        # repurposed into a new angle/format (PRD §23 — see
        # _listening_candidate_concepts / _repurposing_candidate_concepts).
        # Ground the copy in these actual facts rather than reinventing
        # generic supporting detail; still write it fresh — evidence to
        # build from, not copy verbatim.
        brief = c.get("listening_brief") or {}
        source = c.get("source")
        if source not in ("listening", "repurposed") or not brief:
            return ""
        label = ("REAL CUSTOMER EVIDENCE (this is genuinely true, measured in this business's own market"
                 if source == "listening" else
                 "REAL PAST CONTENT (a post this business actually published before, now being repurposed "
                 "into a different format/angle — do not contradict it or invent beyond it")
        parts = [f"{label} — ground the copy in it): {brief.get('observation', '')}"]
        if brief.get("hook"):
            parts.append(f"A hook already considered worth testing: \"{brief['hook']}\"")
        if brief.get("outline"):
            parts.append("Points already identified as worth covering: " + "; ".join(brief["outline"]))
        if brief.get("cta"):
            parts.append(f"CTA direction already matched to this business's goal: \"{brief['cta']}\"")
        return "\n  " + " ".join(parts)

    concepts_block = "\n\n".join(
        f"""Item {i} — {c['date']}:
  Territory: {c.get('territory')} | Subject: {c.get('subject')} | Angle: {c.get('angle')}
  Creative device: {(c.get('creative_device') or {}).get('device')} ({(c.get('creative_device') or {}).get('category')})
  Concept: {c.get('concept_name') or c.get('creative_concept_name', '')}
  Objective: {c.get('objective')} | Audience segment: {c.get('audience_segment', '')}
  Format: {_fmt_line(c)}"""
        + (f"\n  BRAND KEY DATE: this post is about {c['key_date']['label']} ({c['key_date']['date']}) — "
           f"make it the subject of the post, by name." if c.get("key_date")
           else (f"\n  Holiday tie-in: {c['holiday_tie_in'].get('name')}" if c.get("holiday_tie_in") else ""))
        + _listening_line(c)
        for i, c in enumerate(concepts_chunk)
    )

    carousel_spec_lines = [
        f"Item {i}'s carousel: aim for {c.get('carousel_slide_count', 3)} slides. "
        f"HARD LIMIT: never fewer than 2, never more than 5 (PRD rule — do not pad to reach 5)."
        for i, c in enumerate(concepts_chunk) if c.get("format") == "carousel"
    ]
    carousel_spec = ("\n" + "\n".join(carousel_spec_lines)) if carousel_spec_lines else ""

    # Substitute the real brand name into {brand}-templated entries for display
    # — truncating at the placeholder (the old approach) left phrases like
    # "at {brand}, we" showing as the near-meaningless "at..." to the model.
    banned_phrases_str = ", ".join(
        f'"{p.replace("{brand}", brand_name).strip()}..."' for p in ANTI_BORING_PHRASES if p
    )

    prompt = f"""You are a senior social media copywriter turning {n} ALREADY-APPROVED
content concepts into publish-ready posts for {brand_name}{f' ("{tagline}")' if tagline else ''}.
Industry: {industry}. {f'What they do: {description}.' if description else ''}
Target audience: {audience}{f', {region} market' if region else ''}. Brand voice: {voice}.
{f'USP: {usp}.' if usp else ''}
{f'Price positioning: {price_range}.' if price_range else ''}
{business_pulse_block}{assets_block}{psychology_block}{visual_guide_block}
Platforms: {platforms_str}
{force_token}

Each concept below is ALREADY DECIDED — its territory, subject, angle, and
creative device are FIXED. Your job is EXECUTION only: write the actual copy
that brings this specific concept to life. Do NOT invent a different idea,
switch the angle, or change what the post is fundamentally about.

For each item, think in THIS order before writing anything: (1) the human/
business INSIGHT the concept is built on, (2) the CREATIVE IDEA — the
interesting way to express it, (3) the VISUAL CONCEPT — what the audience
should actually see, (4) then the HEADLINE, (5) then supporting copy, (6)
then the CTA. Do not start from "what text goes on the graphic."

{concepts_block}
{carousel_spec}

HEADLINE STANDARD — every title/headline must create curiosity, tension,
recognition, surprise, contrarian thinking, or real specificity. It should
make the reader think "wait, what are they talking about?" — not read like
generic advice. Ground it in the specific customer reality above (a real
pain point, objection, or their own words), never a generic topic statement.
Example of the shift required: instead of "5 Skincare Tips Everyone Needs,"
write "Your skin isn't necessarily dry. Your routine may just be fighting
itself." Avoid opening a headline with generic AI-sounding constructions —
high-risk patterns to avoid: {banned_phrases_str}. These aren't hard-banned,
but treat them as a strong signal to rewrite.

TEXT DENSITY — use the visual to communicate the idea; use text to sharpen
it, not explain it. Prefer one strong headline + one short supporting line
over a headline plus a paragraph plus bullet points plus an explanation. For
a carousel, each slide carries ONE idea — never compress an entire article
across the slides; the story should read visually as the audience swipes.
The caption in exact_copy is capped at {MAX_CAPTION_WORDS} words — this is a
hard limit, not a target to approach. Most captions should land well under
it: a hook line, one supporting line, the CTA. If you find yourself
explaining the idea instead of stating it, cut, don't compress harder.

For EACH item, return ALL of these fields:
- title: max 10 words, punchy, specific to this brand — must clearly reflect its concept's subject+angle and meet the HEADLINE STANDARD above
- hook: exact opening line (1 sentence), executing the item's creative device
- key_points: 2-5 concrete specific points
- description: 2-3 sentences tying the idea together
- caption_direction: 1-2 sentences of specific guidance for the caption — necessary information only, not padding
- keywords: 2-4 real keywords specific to this idea
- cta: one specific call-to-action sentence
- topic: 3-6 word plain-language topic label
- promised_business_outcome: what result this post ultimately supports (1 short sentence)
- content_pillar: which broad content pillar this belongs to (a few words)
- customer_journey_stage: one of awareness|consideration|decision|retention
- video_idea: {{"format": one of talking_head|product_demo|testimonial|tutorial|behind_the_scenes|trend_based, "hook": "...", "talking_points": ["..."], "scenes": ["..."], "cta": "..."}}
- holiday_reference: null unless a real, relevant holiday/observance genuinely
  falls on this item's date for {region or 'the audience region'} — never invent one
- exact_copy: {{"headline": "publish-ready headline/first-line — must meet the HEADLINE STANDARD above", "caption": "the FULL publish-ready caption text, ready to post as-is, max {MAX_CAPTION_WORDS} words — see TEXT DENSITY above", "hashtags": ["2-5 relevant hashtags, no # symbol"]}}
- carousel: null UNLESS this item's format is CAROUSEL (see the exact slide count required above), in which case:
  {{"slides": [{{"slide_index": 0, "headline": "...", "body": "one idea, kept short — see TEXT DENSITY", "visual_note": "..."}}, ...exactly the required number of slides...]}}
- creative_concept_name: a short, final version of the concept name
- central_visual_idea: 1 concrete sentence describing the central visual concept — prioritise a human scene (a customer, founder, employee experiencing the problem or the outcome) or a symbolic visual (an object/environment/before-after/metaphor) over a generic product-interface shot; use actual product imagery only where the product itself is the point, and screenshots/UI only where the content genuinely needs them
- design_style: a few words describing the visual design style{' — MUST stay within the Visual Guide given above, do not invent a different one' if visual_guide_block else ''}
- layout_direction: a few words on layout/composition{' — consistent with the Visual Guide above' if visual_guide_block else ''}
- visual_metaphor: the visual metaphor being used, or "" if none
- ai_image_prompt: a concrete, literal description of a PHOTOGRAPH or IMAGE
  to generate — subject, setting, lighting, composition, mood. This describes
  the raw visual asset only. Do NOT ask for a "social media graphic," do NOT
  include any headline text, caption text, or CTA in this prompt, and do NOT
  mention typography, layout, or brand colours — that's a separate design
  layer applied afterward. End the prompt with "no text, no logos."
- required_assets: a short list of assets needed — prefer reusing anything listed as already available above over requesting new production
- designer_execution_notes: 1-2 sentences of concrete guidance for whoever produces the visual
- reasoning: 1-2 sentences on WHY this idea, for THIS day — reference something
  concrete about the business, audience, business stage, date, or Business
  Pulse. Never reference historical performance or trending searches (neither
  exists in this task). Explaining why the format/device works in general
  ("using a question engages the audience") is NOT a real reason.
- primary_kpi: one of reach|engagement|leads|sales|awareness

Never fabricate a specific statistic, named testimonial, exact customer count,
price, discount, or guarantee that wasn't given to you above — write around
missing specifics instead of inventing them.

Return ONLY a valid JSON array of exactly {n} objects, in the same order as
above, each also including "day_offset" set to its item's absolute plan-day
index.

Rules: no two titles share an opening word; vary emotional tone across items;
be specific — real product/service names, real audience details; every item
must be impossible to copy-paste to a different brand. A useful test: if you
removed the brand name, would this still be clearly written for THIS
business's actual customers, not a generic reader?
"""

    async def _call_and_parse(full_prompt: str) -> List[Dict[str, Any]]:
        ai_request = AIService.build_ai_model(
            messages=[{"role": "user", "content": full_prompt}], model="gpt-4o",
            temperature=0.95 if force else 0.9,
        )
        response = await AIService.chat_completion(ai_request)
        if isinstance(response, dict) and response.get("error"):
            raise ValueError(response["error"])
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = _loads_lenient(raw)
        if not isinstance(parsed, list) or len(parsed) != n:
            raise ValueError(f"expected {n} items, got: {raw[:200]}")
        return parsed

    correction_block = ""
    items: List[Dict[str, Any]] = []
    for attempt in range(2):
        try:
            items = await _call_and_parse(prompt + correction_block)
        except Exception as exc:
            # Confirmed live: this used to `raise` immediately when attempt 0's
            # parse failed, skipping the second attempt entirely — one transient
            # JSON glitch on the FIRST call for a chunk silently wiped out the
            # whole chunk (a 6-item chunk failing here is exactly why 30-item
            # plans were landing at 24). Now it gets the full 2 tries like the
            # validation-failure retry path below already did.
            print(f"[CalendarV2] final-copy chunk parse failed on attempt {attempt + 1}/2 ({exc})", flush=True)
            if attempt == 0:
                continue
            raise

        failures: Dict[int, List[str]] = {}
        for i, idea in enumerate(items):
            is_carousel = concepts_chunk[i].get("format") == "carousel"
            expected_slides = concepts_chunk[i].get("carousel_slide_count", 3)
            if is_carousel:
                _clamp_carousel(idea, expected_slides)  # deterministically enforce the 2-3 hard rule
            issues = _validate_item_v2(
                idea, is_carousel=is_carousel, expected_slides=expected_slides,
                words_to_avoid=_all_banned_words(brand),
            )
            if issues:
                failures[i] = issues
        if not failures:
            break
        print(f"[CalendarV2] validation failed for {len(failures)}/{n} item(s) on attempt {attempt + 1}", flush=True)
        if attempt == 0:
            correction_lines = [f"Item {i}: {'; '.join(iss)}" for i, iss in sorted(failures.items())]
            correction_block = (
                "\n\n=== FIX THESE SPECIFIC PROBLEMS ===\n" + "\n".join(correction_lines)
                + f"\nRegenerate all {n} items, keeping what worked and fixing only what's listed.\n"
            )

    # Merge the fixed concept fields (territory/subject/angle/device/format/
    # date/day_index from Steps 3-7) back onto each generated item. The
    # prompt never asks the model to return these, but confirmed live: it
    # can echo one back anyway (e.g. its own flattened-string creative_device)
    # despite not being asked to — {**concept, **idea} let that silently win
    # and crashed every downstream .get() call on the now-wrong-shaped value.
    # Strip these keys from idea explicitly so concept's values always win,
    # not just "usually win because the model behaves."
    _CONCEPT_FIELDS = {
        "territory", "subject", "angle", "creative_device", "format_hint", "format",
        "objective", "audience_segment", "concept_name", "day_index", "date",
        "carousel_slide_count", "holiday_tie_in", "selection_score", "key_date",
    }
    merged = []
    for i, idea in enumerate(items):
        concept = concepts_chunk[i]
        if isinstance(idea, dict) and concept.get("format") == "carousel":
            _clamp_carousel(idea, concept.get("carousel_slide_count", 3))  # final safety net if both retries broke
        if isinstance(idea, dict):
            _clamp_caption(idea)  # final safety net if both retries still came back too wordy
        idea_safe = {k: v for k, v in idea.items() if k not in _CONCEPT_FIELDS} if isinstance(idea, dict) else {}
        merged.append({**concept, **idea_safe, "day_offset": concept["day_index"]})
    return merged


async def _regenerate_flagged_items(
    flagged_day_indices: List[int],
    items_by_index: Dict[int, Dict[str, Any]],
    brand: Dict[str, Any],
    platforms: List[str],
    existing_assets_summary: str,
) -> Dict[int, Dict[str, Any]]:
    """PRD §29: 'the system should regenerate flagged items rather than
    simply displaying a warning.' One bounded pass; keeps each flagged
    item's already-approved territory/subject/angle/device/format fixed and
    just re-executes Step8+9 with force=True for a genuinely different
    result — items still flagged after this pass fall back to manual-review
    display (today's prior behavior), not an infinite retry loop."""
    if not flagged_day_indices:
        return {}
    concepts_chunk = [items_by_index[i] for i in flagged_day_indices if i in items_by_index]
    if not concepts_chunk:
        return {}
    try:
        regenerated = await _generate_final_copy(
            brand=brand, concepts_chunk=concepts_chunk, platforms=platforms,
            existing_assets_summary=existing_assets_summary, force=True,
        )
    except Exception as exc:
        print(f"[CalendarV2] auto-regeneration of {len(concepts_chunk)} flagged item(s) failed: {exc}", flush=True)
        return {}
    return {item["day_index"]: item for item in regenerated}


# ── Main generation ──────────────────────────────────────────────────────────

# Holds in-flight background generation tasks so they aren't garbage-
# collected mid-run (same pattern complete_social_manager.py's
# _BG_IMAGE_TASKS uses).
_PLAN_GEN_TASKS: set = set()


async def get_active_plan(
    user_id: str,
    db: AsyncIOMotorDatabase,
    brand_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The current LIVE plan — status 'active' only. Internal callers use
    this; the router's GET /plan uses get_latest_plan so an in-progress or
    failed generation is also visible to the frontend."""
    scope = _cal_v2_scope(user_id, brand_id)
    return await db[COLLECTION].find_one({**scope, "status": "active"}, {"_id": 0}, sort=[("created_at", -1)])


# The pipeline normally finishes in ~4-6 min (see start_plan_generation's own
# docstring). Generous buffer above that before treating a 'generating' plan
# as dead rather than just slow.
_STALE_GENERATING_AFTER = timedelta(minutes=12)


async def get_latest_plan(
    user_id: str,
    db: AsyncIOMotorDatabase,
    brand_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Most recent non-archived plan — active, generating, or failed — so the
    frontend can render a 'generating…' state and poll, or show a failure.

    Also self-heals a stuck 'generating' plan: the background task is an
    in-process asyncio.Task (see start_plan_generation), so it dies silently
    — without ever reaching its own except-block — whenever the ECS task
    running it gets replaced mid-run (a routine backend deploy). Confirmed
    live: a plan sat at 'generating' for 76+ minutes with nothing to flip it
    and no way for the frontend's poll loop to escape. Every GET /plan call
    routes through here, so the very next poll after this ships auto-repairs
    any plan already stuck from before it existed."""
    scope = _cal_v2_scope(user_id, brand_id)
    plan = await db[COLLECTION].find_one(
        {**scope, "status": {"$in": ["active", "generating", "failed"]}},
        {"_id": 0}, sort=[("created_at", -1)],
    )
    if plan and plan.get("status") == "generating":
        try:
            created_at = datetime.fromisoformat(plan["created_at"])
        except (TypeError, ValueError, KeyError):
            created_at = None
        if created_at and datetime.utcnow() - created_at > _STALE_GENERATING_AFTER:
            error_msg = (
                "Generation was interrupted (most likely a backend deploy while it was running) "
                "and never finished. Please try again."
            )
            now_iso = datetime.utcnow().isoformat()
            await db[COLLECTION].update_one(
                {"plan_id": plan["plan_id"]},
                {"$set": {"status": "failed", "error": error_msg, "updated_at": now_iso}},
            )
            plan = {**plan, "status": "failed", "error": error_msg, "updated_at": now_iso}
    return plan


async def start_plan_generation(
    user_id: str,
    platforms: List[str],
    brand: Dict[str, Any],
    db: AsyncIOMotorDatabase,
    force: bool = False,
    brand_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Async entry point for the router. Returns near-instantly with either
    the existing active plan (not force) or a status='generating'
    placeholder, kicking the real multi-minute pipeline off as a background
    task. This is the only way generation survives the API gateway's
    timeout — the same job-then-poll shape the video pipeline already uses.
    Confirmed live: the synchronous path reliably 504s while the backend is
    still working minutes later."""
    scope = _cal_v2_scope(user_id, brand_id)
    if not force:
        existing = await get_active_plan(user_id, db, brand_id=brand_id)
        if existing:
            return existing

    # Clear any stale generating/failed placeholders in this scope so
    # get_latest_plan always returns the one we're about to create.
    await db[COLLECTION].update_many(
        {**scope, "status": {"$in": ["generating", "failed"]}},
        {"$set": {"status": "archived"}},
    )

    now = datetime.utcnow()
    period_start = _get_period_start(now)
    plan_id = str(uuid.uuid4())
    placeholder = {
        "plan_id": plan_id, "user_id": user_id, "brand_id": brand_id,
        "status": "generating",
        "period_start": period_start.strftime("%Y-%m-%d"),
        "period_end": (period_start + timedelta(days=PLAN_DAYS - 1)).strftime("%Y-%m-%d"),
        "generation_method": "framework_driven",
        "framework_version": get_creative_framework(brand.get("industry", ""))["framework_version"],
        "pipeline_version": "2026-09-v1",
        "platforms": platforms, "carousel_slots": [],
        "intelligence_snapshot": {}, "territory_mix": {}, "content_mix": {},
        "items": [], "error": None,
        "created_at": now.isoformat(), "updated_at": now.isoformat(),
    }
    await db[COLLECTION].insert_one({**placeholder, "_id": plan_id})

    task = asyncio.create_task(
        _run_plan_generation_bg(plan_id, user_id, platforms, brand, db, force, brand_id)
    )
    _PLAN_GEN_TASKS.add(task)
    task.add_done_callback(_PLAN_GEN_TASKS.discard)
    return {k: v for k, v in placeholder.items() if k != "_id"}


async def _run_plan_generation_bg(
    plan_id: str, user_id: str, platforms: List[str], brand: Dict[str, Any],
    db: AsyncIOMotorDatabase, force: bool, brand_id: Optional[str],
) -> None:
    scope = _cal_v2_scope(user_id, brand_id)
    try:
        doc = await _build_plan_doc(user_id, platforms, brand, db, force, brand_id)
        # Archive prior active plans, then fill the placeholder in place.
        await db[COLLECTION].update_many(
            {**scope, "status": "active", "plan_id": {"$ne": plan_id}},
            {"$set": {"status": "archived"}},
        )
        fill = {k: v for k, v in doc.items()
                if k not in ("plan_id", "user_id", "brand_id", "created_at", "_id")}
        fill["status"] = "active"
        fill["updated_at"] = datetime.utcnow().isoformat()
        await db[COLLECTION].update_one({"plan_id": plan_id}, {"$set": fill})
        print(f"[CalendarV2] plan {plan_id} complete — {len(doc.get('items', []))} items", flush=True)
    except Exception as e:
        import traceback
        print(f"[CalendarV2] plan {plan_id} generation FAILED: {e}\n{traceback.format_exc()}", flush=True)
        await db[COLLECTION].update_one(
            {"plan_id": plan_id},
            {"$set": {"status": "failed", "error": str(e)[:500],
                      "updated_at": datetime.utcnow().isoformat()}},
        )


async def generate_plan_v2(
    user_id: str,
    platforms: List[str],
    brand: Dict[str, Any],
    db: AsyncIOMotorDatabase,
    force: bool = False,
    brand_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Synchronous build-and-persist — kept for any non-router caller / tests.
    The router now uses start_plan_generation instead (async, gateway-safe)."""
    scope = _cal_v2_scope(user_id, brand_id)
    if not force:
        existing = await get_active_plan(user_id, db, brand_id=brand_id)
        if existing:
            return existing
    doc = await _build_plan_doc(user_id, platforms, brand, db, force, brand_id)
    if force:
        await db[COLLECTION].update_many({**scope, "status": "active"}, {"$set": {"status": "archived"}})
    await db[COLLECTION].insert_one({**doc, "_id": doc["plan_id"]})
    return doc


async def _build_plan_doc(
    user_id: str,
    platforms: List[str],
    brand: Dict[str, Any],
    db: AsyncIOMotorDatabase,
    force: bool = False,
    brand_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Runs the full staged pipeline and returns the finished plan doc —
    does NOT persist it (caller decides: insert, or fill a placeholder)."""
    scope = _cal_v2_scope(user_id, brand_id)
    now = datetime.utcnow()
    period_start = _get_period_start(now)
    period_end = period_start + timedelta(days=PLAN_DAYS - 1)

    industry = brand.get("industry", "")
    region = brand.get("region", "")

    # Step 2 — load the versioned creative framework (PRD §35)
    framework = get_creative_framework(industry)

    # Step 1 supplement + Step-new — creative memory and existing assets,
    # both explicitly performance-free (PRD §20, §22)
    creative_memory = await _fetch_creative_memory(scope, db)
    existing_assets_summary = await _get_existing_assets_summary(user_id, brand_id, db)
    has_active_promo = bool((brand.get("business_pulse") or {}).get("current_promotions"))

    # Date/holiday/cultural signals — real dated events only, non-performance
    # inputs (PRD §18). get_cultural_moments() (not get_trending_topics(),
    # which only ever returns a hardcoded SIMULATED_TRENDS placeholder dict
    # of fake industry buzzwords — the exact "trending topics" PRD §2 bans,
    # confirmed live never even used the date it was passed) — this is the
    # real, dated-proximity-filtered event list §18 actually asks for.
    all_dates = [(period_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(PLAN_DAYS)]
    holidays_by_date: Dict[str, Dict[str, Any]] = {}
    cultural_moments_all: List[Any] = []
    for chunk_start_idx in range(0, PLAN_DAYS, 7):
        chunk_week_start = all_dates[chunk_start_idx]
        for h in HolidayCalendarService.get_upcoming_holidays(chunk_week_start, region, industry) or []:
            holidays_by_date[h["date"]] = h
        cultural_moments_all += CulturalMomentService.get_cultural_moments(chunk_week_start, region) or []

    # Step 3 — candidate pool (never receives performance/trend data).
    # industry_best_practices deliberately no longer sourced or passed here —
    # see _generate_candidate_concepts's own docstring for why.
    candidates = await _generate_candidate_concepts(
        brand=brand, framework=framework, existing_assets_summary=existing_assets_summary,
        creative_memory=creative_memory, platforms=platforms,
        cultural_moments=cultural_moments_all,
    )
    if not candidates:
        raise RuntimeError("Content Calendar V2 generation failed — no candidate concepts produced.")

    # Step 3b — real, evidence-backed Listening opportunities join the same
    # pool and compete on identical scoring terms (Listening PRD §18:
    # "insight-backed post suggestions; brief passed directly into
    # generation") — see _listening_candidate_concepts's docstring for why
    # this isn't the performance/trend data banned above.
    try:
        listening_candidates = await _listening_candidate_concepts(db, user_id, brand_id)
    except Exception as exc:
        print(f"[CalendarV2] listening candidates skipped ({exc})", flush=True)
        listening_candidates = []
    if listening_candidates:
        print(f"[CalendarV2] {len(listening_candidates)} listening-sourced candidate(s) added to the pool", flush=True)
        candidates = candidates + listening_candidates

    # Step 3c — real past content repurposed into a new angle/format this
    # cycle (PRD §23) — see _repurposing_candidate_concepts's docstring.
    try:
        repurposing_candidates = await _repurposing_candidate_concepts(db, user_id, brand_id)
    except Exception as exc:
        print(f"[CalendarV2] repurposing candidates skipped ({exc})", flush=True)
        repurposing_candidates = []
    if repurposing_candidates:
        print(f"[CalendarV2] {len(repurposing_candidates)} repurposing candidate(s) added to the pool", flush=True)
        candidates = candidates + repurposing_candidates

    # Step 4 — score (9 named dims, no performance/trend fields — enforced in code)
    scored = await _score_candidates(candidates, brand, framework, creative_memory)

    # Step 5 — select 30, diversity-optimized
    selected = _select_diverse_thirty(scored, framework)
    if len(selected) < PLAN_DAYS:
        raise RuntimeError(
            f"Content Calendar V2 generation failed — only {len(selected)}/{PLAN_DAYS} concepts survived selection."
        )

    # Step 6 — assign dates
    dated = _assign_dates(selected, all_dates, holidays_by_date)
    key_dates_placed = _enforce_key_dates(dated, brand, all_dates)
    if key_dates_placed:
        print(f"[CalendarV2] key dates given their post: {key_dates_placed}")

    # Step 7 — assign format, dynamic 2-3 slide carousels, capped video/week
    formatted = _assign_formats(dated, brand, existing_assets_summary)

    # Step 8+9 — final copy + creative direction, concurrently chunked
    # (same proven concurrency pattern this codebase already fixed a real
    # proxy-timeout bug around — see _generate_final_copy's docstring for
    # why copy+direction share one call instead of adding a 4th stage).
    chunk_starts = list(range(0, PLAN_DAYS, CONTENT_CHUNK_SIZE))

    async def _run_chunk(chunk_start_idx: int) -> List[Dict[str, Any]]:
        chunk = formatted[chunk_start_idx:chunk_start_idx + CONTENT_CHUNK_SIZE]
        try:
            return await _generate_final_copy(
                brand=brand, concepts_chunk=chunk, platforms=platforms,
                existing_assets_summary=existing_assets_summary, force=force,
            )
        except Exception as exc:
            print(f"[CalendarV2] final-copy chunk at offset {chunk_start_idx} failed: {exc}", flush=True)
            return []

    chunk_results = await asyncio.gather(*[_run_chunk(idx) for idx in chunk_starts])
    all_items: List[Dict[str, Any]] = [item for chunk_items in chunk_results for item in chunk_items]

    if not all_items:
        raise RuntimeError("Content Calendar V2 generation failed for every chunk — no items produced.")

    items_by_index = {item["day_index"]: item for item in all_items}

    # Backfill — one more shot at any day whose chunk still failed outright
    # after _generate_final_copy's own 2 attempts (confirmed live: this is
    # how a 30-item plan was landing at 24; the retry-inversion bug above was
    # the main cause, this is the safety net for whatever still slips through
    # — a stubborn chunk, or the retry's OWN second attempt also failing).
    missing_indices = sorted(set(range(PLAN_DAYS)) - set(items_by_index.keys()))
    if missing_indices:
        print(f"[CalendarV2] backfilling {len(missing_indices)} missing item(s) at day_index={missing_indices}", flush=True)
        backfill_concepts = [c for c in formatted if c["day_index"] in missing_indices]
        try:
            backfill_items = await _generate_final_copy(
                brand=brand, concepts_chunk=backfill_concepts, platforms=platforms,
                existing_assets_summary=existing_assets_summary, force=force,
            )
            for item in backfill_items:
                items_by_index[item["day_index"]] = item
            all_items = list(items_by_index.values())
            print(f"[CalendarV2] backfill recovered {len(backfill_items)}/{len(missing_indices)} item(s)", flush=True)
        except Exception as exc:
            print(f"[CalendarV2] backfill pass failed ({exc}) — plan will ship with {len(all_items)}/{PLAN_DAYS} items", flush=True)

    # Step 11 — semantic/creative validation (deterministic hard rules were
    # already enforced per-chunk inside _generate_final_copy's retry loop).
    # llm_flagged now also carries the addendum §19 "No Generic AI Content"
    # gate's semantic-judgment questions — see _llm_diversity_check's docstring.
    rule_issues = _rule_based_diversity_issues(all_items)
    llm_flagged = await _llm_diversity_check(all_items, brand=brand)
    print(f"[CalendarV2] diversity+quality check: rule_issues={len(rule_issues)} llm_flagged={len(llm_flagged)}", flush=True)
    if len(llm_flagged) > max(6, len(all_items) // 4):
        print(f"[CalendarV2] LLM diversity check flagged {len(llm_flagged)}/{len(all_items)} — implausible, discarding", flush=True)
        llm_flagged = []
    # Items that still use a generic AI word after the per-chunk retry get the
    # regeneration pass too, rather than shipping with only a warning.
    generic_flagged = [it.get("day_index") for it in all_items if _generic_words_used(it)]
    # PRD §31 hard rule — a fabricated stat/testimonial/citation is not a
    # style problem to warn about, it's a false claim; it gets the same
    # forced regeneration as everything else here, never a shrug.
    fabrication_flagged = [it.get("day_index") for it in all_items if _fabricated_claims_used(it, brand)]
    flagged_day_indices = sorted(set(rule_issues.keys()) | set(llm_flagged) | set(generic_flagged) | set(fabrication_flagged))
    print(f"[CalendarV2] diversity check: {len(flagged_day_indices)} item(s) flagged for auto-regeneration "
          f"({len(generic_flagged)} for generic words, {len(fabrication_flagged)} for possibly-fabricated claims)", flush=True)

    # Step 12 — regenerate flagged items only, one bounded pass
    regenerated_day_indices: set = set()
    if flagged_day_indices:
        regenerated = await _regenerate_flagged_items(
            flagged_day_indices=flagged_day_indices, items_by_index=items_by_index,
            brand=brand, platforms=platforms, existing_assets_summary=existing_assets_summary,
        )
        for day_index, new_item in regenerated.items():
            items_by_index[day_index] = new_item
            regenerated_day_indices.add(day_index)
        all_items = [items_by_index[i] for i in sorted(items_by_index.keys())]
        # Re-run the cheap deterministic check once more so the stored
        # diversity_check reflects post-regeneration reality.
        rule_issues = _rule_based_diversity_issues(all_items)
        still_flagged = set(flagged_day_indices) - regenerated_day_indices
        flagged_day_indices = sorted(set(rule_issues.keys()) | still_flagged)

    scrubbed = await _scrub_generic_words(all_items)
    if scrubbed:
        print(f"[CalendarV2] reworded {scrubbed} text(s) that still used generic AI words", flush=True)

    flagged_set = set(flagged_day_indices)
    anti_boring_notes = _anti_boring_check(all_items, brand_name=brand.get("brand_name", ""))

    # Step 10 — ad opportunity scoring + copy. Scoring is pure/fast; copy
    # generation is the one real LLM call here — fired concurrently for every
    # candidate (asyncio.gather) instead of awaited one-by-one in the main
    # loop, which was adding N sequential round-trips on top of an already
    # 3-stage pipeline that's tight against the gateway timeout.
    ad_scores: Dict[int, float] = {}
    ad_angles: Dict[int, str] = {}
    for i, idea in enumerate(all_items):
        day_index = idea.get("day_index", i)
        date_str = idea.get("date") or all_dates[day_index]
        near_holiday = date_str in holidays_by_date
        ad_scores[day_index] = _score_ad_opportunity(idea, near_holiday, has_active_promo)
        if ad_scores[day_index] >= 55.0:
            ad_angles[day_index] = _derive_ad_angle(idea.get("angle", ""))

    candidate_indices = list(ad_angles.keys())
    ad_copies = await asyncio.gather(*[
        _write_calendar_ad_copy(items_by_index[di], brand, ad_angles[di]) for di in candidate_indices
    ]) if candidate_indices else []
    ad_copy_by_index = dict(zip(candidate_indices, ad_copies))

    # PRD §21 — every item sharing a series_name within this plan gets the
    # same series_id, so the frontend can group/highlight a recurring series
    # as one thing rather than N unrelated items that happen to share a label.
    #
    # The candidate-generation prompt already asks the model to reuse a series
    # name across at least 2 concepts and never invent one just to fill the
    # field — but nothing enforced that. Confirmed live: a real plan carried 6
    # distinct series names, every one used exactly once, which isn't a
    # recurring series, it's noise dressed up as one. Same pattern this
    # pipeline already uses for its other hard rules (words_to_avoid, the 2-3
    # carousel-slide limit) — count actual occurrences across the selected 30
    # and silently drop any series_name that didn't genuinely recur, rather
    # than trusting the model to have followed the instruction.
    resolved_series_names = [
        _resolve_series_name(idea.get("series_name"), brand.get("brand_name") or "the brand", industry or "business")
        for idea in all_items
    ]
    recurring_series_names = _recurring_series_names(resolved_series_names)

    series_ids_by_name: Dict[str, str] = {}
    items_out: List[Dict[str, Any]] = []
    for i, idea in enumerate(all_items):
        day_index = idea.get("day_index", i)
        date_str = idea.get("date") or all_dates[day_index]
        territory = idea.get("territory", "")
        creative_angle = idea.get("angle", "")
        is_carousel = idea.get("format") == "carousel"
        near_holiday = date_str in holidays_by_date

        ad_score = ad_scores[day_index]
        if day_index in ad_copy_by_index:
            ad_angle = ad_angles[day_index]
            territory_label = framework["territories"].get(territory, {}).get("label", territory)
            ad_opportunity = AdOpportunityV2(
                is_ad_candidate=True, score=ad_score, angle=ad_angle, ad_copy=ad_copy_by_index[day_index],
                reason=f"Scored {ad_score}/100 — {territory_label} territory"
                       + (", near a relevant date" if near_holiday else ""),
            ).model_dump()
        else:
            ad_opportunity = AdOpportunityV2(is_ad_candidate=False, score=ad_score).model_dump()

        day_holidays = [holidays_by_date[date_str]] if date_str in holidays_by_date else []
        ai_holiday = idea.get("holiday_reference")
        if isinstance(ai_holiday, dict) and str(ai_holiday.get("name") or "").strip():
            day_holidays.append({
                "date": date_str, "name": ai_holiday["name"], "type": "ai_suggested",
                "content_angle": ai_holiday.get("why_relevant") or "",
            })

        data_provenance = {
            "price_range": "known" if brand.get("price_range") else "unknown",
            "unique_selling_proposition": "known" if brand.get("unique_selling_proposition") else "unknown",
            "testimonial_or_stat_claims": "unknown",  # never sourced — never fabricate, PRD §31
        }

        reasoning = idea.get("reasoning") or (
            f"Selected for its {territory} territory around \"{idea.get('subject', '')}\", "
            f"using the {creative_angle.replace('_', ' ')} angle."
        )

        device = idea.get("creative_device") or {}
        resolved_series = resolved_series_names[i]
        recurring_series = resolved_series if resolved_series in recurring_series_names else None
        items_out.append({
            "item_id": str(uuid.uuid4()),
            "day_index": day_index,
            "date": date_str,
            "title": idea.get("title", ""),
            "description": idea.get("description", ""),
            "hook": idea.get("hook", ""),
            "key_points": idea.get("key_points", []),
            "caption_direction": idea.get("caption_direction", ""),
            "keywords": idea.get("keywords", []),
            "cta": idea.get("cta", ""),
            "video_idea": idea.get("video_idea", {}),
            "upcoming_holidays": day_holidays,
            "format": idea.get("format", "image"),
            "content_type": _derive_content_type(territory),
            "territory": territory,
            "subject": idea.get("subject", ""),
            "creative_angle": creative_angle,
            "creative_device": {
                "category": device.get("category", ""), "device": device.get("device", ""),
                "label": device.get("device", ""),
            },
            "content_pillar": idea.get("content_pillar", ""),
            "customer_journey_stage": idea.get("customer_journey_stage", ""),
            "promised_business_outcome": idea.get("promised_business_outcome", ""),
            "creative_concept_name": idea.get("creative_concept_name") or idea.get("concept_name", ""),
            "carousel": idea.get("carousel") if is_carousel else None,
            "creative_direction": {
                "visual_style": idea.get("design_style", ""),
                "mood": (idea.get("creative_direction") or {}).get("mood", ""),
                "color_note": (idea.get("creative_direction") or {}).get("color_note", ""),
                "composition_note": idea.get("layout_direction", ""),
                "central_visual_idea": idea.get("central_visual_idea", ""),
            },
            "design_style": idea.get("design_style", ""),
            "layout_direction": idea.get("layout_direction", ""),
            "visual_metaphor": idea.get("visual_metaphor", ""),
            "required_assets": idea.get("required_assets", []),
            "designer_execution_notes": idea.get("designer_execution_notes", ""),
            "ai_image_prompt": idea.get("ai_image_prompt", ""),
            "exact_copy": idea.get("exact_copy") or {},
            "reasoning": reasoning,
            "data_provenance": data_provenance,
            "ad_opportunity": ad_opportunity,
            "primary_kpi": idea.get("primary_kpi", "engagement"),
            "selection_score": idea.get("selection_score", {}),
            "series_id": (
                series_ids_by_name.setdefault(recurring_series, str(uuid.uuid4()))
                if recurring_series else None
            ),
            "series_name": recurring_series,
            "creative_quality_review_note": anti_boring_notes.get(day_index),
            "diversity_check": {
                "passed": day_index not in flagged_set,
                "similarity_score": 1.0 if day_index in flagged_set else 0.0,
                "flagged_against_item_id": None,
            },
            "version_history": [],
            "regenerated_count": 1 if day_index in regenerated_day_indices else 0,
            "last_regenerated_reason": "diversity_auto" if day_index in regenerated_day_indices else None,
            "acted_on": False,
            "acted_on_draft_ids": [],
            "status": "pending",
            "performance": None,
        })

    items_out.sort(key=lambda it: it["day_index"])

    plan_id = str(uuid.uuid4())
    territory_counts: Dict[str, int] = {}
    content_type_counts: Dict[str, int] = {}
    for it in items_out:
        territory_counts[it["territory"]] = territory_counts.get(it["territory"], 0) + 1
        content_type_counts[it["content_type"]] = content_type_counts.get(it["content_type"], 0) + 1
    n_items = len(items_out) or 1

    doc = {
        "plan_id": plan_id,
        "user_id": user_id,
        "brand_id": brand_id,
        "status": "active",
        "period_start": period_start.strftime("%Y-%m-%d"),
        "period_end": period_end.strftime("%Y-%m-%d"),
        # Replaces the old "data_driven"|"trend_driven"|"ai" values, which
        # literally advertised performance/trend influence that no longer
        # exists here (PRD §2).
        "generation_method": "framework_driven",
        "framework_version": framework["framework_version"],
        "pipeline_version": "2026-09-v1",
        "platforms": platforms,
        "carousel_slots": [it["day_index"] for it in items_out if it["format"] == "carousel"],
        "intelligence_snapshot": {
            "holidays": list(holidays_by_date.values()),
            "cultural_moments": cultural_moments_all[:10],
        },
        "territory_mix": {k: round(v / n_items, 2) for k, v in territory_counts.items()},
        "content_mix": {k: round(v / n_items, 2) for k, v in content_type_counts.items()},
        "items": items_out,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    # Pipeline only — persistence is the caller's job (generate_plan_v2 inserts;
    # _run_plan_generation_bg fills the pre-created 'generating' placeholder).
    return doc


# ── Regeneration with versioning (diverges from v1's overwrite-in-place) ───────

# PRD §46 — the written text vs. the visual direction, the two halves
# "copy only" and "creative only" each keep the other side of untouched.
# "hook" is narrower still: just the opening line, not the rest of the copy.
_REGEN_COPY_FIELDS = (
    "title", "description", "hook", "key_points", "caption_direction",
    "keywords", "cta", "video_idea", "exact_copy", "carousel",
)
_REGEN_CREATIVE_FIELDS = (
    "ai_image_prompt", "design_style", "layout_direction", "visual_metaphor",
    "required_assets", "designer_execution_notes",
)
_VALID_REGEN_SCOPES = {"full", "hook", "copy", "creative", "format"}


async def regenerate_item_v2(
    plan_id: str,
    item_index: int,
    user_id: str,
    db: AsyncIOMotorDatabase,
    brand_id: Optional[str] = None,
    reason: str = "",
    regen_scope: str = "full",
    new_format: Optional[str] = None,
) -> Dict[str, Any]:
    if regen_scope not in _VALID_REGEN_SCOPES:
        raise ValueError(f"Unknown regenerate scope {regen_scope!r} — must be one of {sorted(_VALID_REGEN_SCOPES)}")
    if regen_scope == "format" and not new_format:
        raise ValueError("new_format is required when scope is 'format'")

    scope = _cal_v2_scope(user_id, brand_id)
    plan = await db[COLLECTION].find_one({**scope, "plan_id": plan_id}, {"_id": 0})
    if not plan:
        raise ValueError("Plan not found")
    item = next((it for it in plan["items"] if it["day_index"] == item_index), None)
    if item is None:
        raise ValueError(f"Item {item_index} not found in plan")

    # Re-fetch the live brand profile (not a frozen snapshot) — a single-item
    # regen always reflects the CURRENT brand context, same choice v1's
    # frozen-snapshot regenerate does NOT make.
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    profile_result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
    raw_profile = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
    brand = BrandProfileService.to_brand_context(raw_profile) if raw_profile else {}

    existing_assets_summary = await _get_existing_assets_summary(user_id, brand_id, db)

    # Keeps the item's already-approved territory/subject/angle/device/
    # format fixed — a manual regenerate re-executes final-copy only,
    # same "don't reinterpret the concept" rule the auto-regen path follows.
    device = item.get("creative_device") or {}
    original_format = item.get("format", "image")
    # "format" scope is a deliberate override — every other scope keeps the
    # item's already-approved format exactly as it was.
    effective_format = new_format if regen_scope == "format" else original_format
    slide_count = (len(((item.get("carousel") or {}).get("slides")) or []) or 3) if effective_format == "carousel" else 3
    concept = {
        "territory": item.get("territory", ""),
        "subject": item.get("subject", ""),
        "angle": item.get("creative_angle", ""),
        "creative_device": {"category": device.get("category", ""), "device": device.get("device", "")},
        "format_hint": effective_format,
        "objective": item.get("primary_kpi", "engagement"),
        "audience_segment": brand.get("target_audience", ""),
        "concept_name": item.get("creative_concept_name", ""),
        "day_index": item_index,
        "date": item.get("date", ""),
        "format": effective_format,
        "carousel_slide_count": slide_count,
    }

    chunk_items = await _generate_final_copy(
        brand=brand, concepts_chunk=[concept], platforms=plan.get("platforms") or [],
        existing_assets_summary=existing_assets_summary, force=True,
    )
    if not chunk_items:
        raise RuntimeError("Regeneration produced no result")
    new_idea = chunk_items[0]
    is_carousel = concept["format"] == "carousel"

    # ── Version snapshot BEFORE overwrite — mirrors blog_generation_service.py's
    # edit_history $push shape, the one place V2 must diverge from v1's
    # regenerate_day (which overwrites with zero history).
    editable_fields = [
        "title", "description", "hook", "key_points", "caption_direction",
        "keywords", "cta", "video_idea", "exact_copy", "carousel",
        "ai_image_prompt", "creative_direction", "design_style", "layout_direction",
        "visual_metaphor", "required_assets", "designer_execution_notes",
        "creative_concept_name",
    ]
    snapshot = {f: item.get(f) for f in editable_fields}
    version_entry = {"snapshot": snapshot, "edited_at": datetime.utcnow().isoformat(),
                      "reason": reason, "scope": regen_scope}

    # Every field the regeneration COULD touch, keyed plainly (not yet
    # prefixed with the Mongo "items.$[it]." path) so scope can filter this
    # down before anything is sent to the DB.
    all_candidate_fields: Dict[str, Any] = {
        "title": new_idea.get("title", ""),
        "description": new_idea.get("description", ""),
        "hook": new_idea.get("hook", ""),
        "key_points": new_idea.get("key_points", []),
        "caption_direction": new_idea.get("caption_direction", ""),
        "keywords": new_idea.get("keywords", []),
        "cta": new_idea.get("cta", ""),
        "video_idea": new_idea.get("video_idea", {}),
        "exact_copy": new_idea.get("exact_copy") or {},
        "carousel": new_idea.get("carousel") if is_carousel else None,
        "ai_image_prompt": new_idea.get("ai_image_prompt", ""),
        "creative_direction": {
            "visual_style": new_idea.get("design_style", ""),
            "mood": (new_idea.get("creative_direction") or {}).get("mood", ""),
            "color_note": (new_idea.get("creative_direction") or {}).get("color_note", ""),
            "composition_note": new_idea.get("layout_direction", ""),
            "central_visual_idea": new_idea.get("central_visual_idea", ""),
        },
        "design_style": new_idea.get("design_style", ""),
        "layout_direction": new_idea.get("layout_direction", ""),
        "visual_metaphor": new_idea.get("visual_metaphor", ""),
        "required_assets": new_idea.get("required_assets", []),
        "designer_execution_notes": new_idea.get("designer_execution_notes", ""),
    }

    # PRD §46 — which half of the regeneration actually lands, per scope.
    # "full" and "format" touch everything (a format conversion needs a
    # genuinely different structure — a carousel needs slides, a video
    # needs a video_idea — so it can't keep the old copy or direction).
    if regen_scope == "hook":
        # Narrowest level: just the opening line. exact_copy is merged so
        # the existing caption/hashtags survive untouched, only the headline
        # — the copy's own expression of the hook — changes with it.
        fields_to_apply = {
            "hook": all_candidate_fields["hook"],
            "exact_copy": {**(item.get("exact_copy") or {}), "headline": (all_candidate_fields["exact_copy"] or {}).get("headline", "")},
        }
    elif regen_scope == "copy":
        fields_to_apply = {k: all_candidate_fields[k] for k in _REGEN_COPY_FIELDS}
    elif regen_scope == "creative":
        fields_to_apply = {k: all_candidate_fields[k] for k in _REGEN_CREATIVE_FIELDS}
        fields_to_apply["creative_direction"] = all_candidate_fields["creative_direction"]
    else:  # "full" or "format"
        fields_to_apply = all_candidate_fields

    update_fields = {f"items.$[it].{k}": v for k, v in fields_to_apply.items()}
    update_fields["items.$[it].format"] = effective_format
    update_fields["items.$[it].reasoning"] = new_idea.get("reasoning") or item.get("reasoning", "")
    update_fields["items.$[it].creative_concept_name"] = (
        new_idea.get("creative_concept_name") or item.get("creative_concept_name", ""))
    update_fields["items.$[it].regenerated_count"] = item.get("regenerated_count", 0) + 1
    update_fields["items.$[it].last_regenerated_reason"] = f"manual:{regen_scope}"
    update_fields["updated_at"] = datetime.utcnow().isoformat()

    await db[COLLECTION].update_one(
        {**scope, "plan_id": plan_id},
        {"$set": update_fields, "$push": {"items.$[it].version_history": version_entry}},
        array_filters=[{"it.day_index": item_index}],
    )
    if regen_scope == "format" and original_format != effective_format:
        # carousel_slots is a derived summary array at the plan level
        # (used elsewhere to quickly locate carousel items without
        # scanning every item) — keep it in sync with the format change.
        if effective_format == "carousel":
            await db[COLLECTION].update_one({**scope, "plan_id": plan_id}, {"$addToSet": {"carousel_slots": item_index}})
        elif original_format == "carousel":
            await db[COLLECTION].update_one({**scope, "plan_id": plan_id}, {"$pull": {"carousel_slots": item_index}})
    return await db[COLLECTION].find_one({**scope, "plan_id": plan_id}, {"_id": 0})


async def get_item_versions(
    plan_id: str, item_index: int, user_id: str, db: AsyncIOMotorDatabase, brand_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    scope = _cal_v2_scope(user_id, brand_id)
    plan = await db[COLLECTION].find_one({**scope, "plan_id": plan_id}, {"_id": 0, "items": 1})
    if not plan:
        raise ValueError("Plan not found")
    item = next((it for it in plan["items"] if it["day_index"] == item_index), None)
    if item is None:
        raise ValueError(f"Item {item_index} not found")
    return item.get("version_history", [])


async def approve_item_v2(
    plan_id: str, item_index: int, user_id: str, db: AsyncIOMotorDatabase, brand_id: Optional[str] = None,
) -> Dict[str, Any]:
    scope = _cal_v2_scope(user_id, brand_id)
    await db[COLLECTION].update_one(
        {**scope, "plan_id": plan_id},
        {"$set": {"items.$[it].status": "approved", "updated_at": datetime.utcnow().isoformat()}},
        array_filters=[{"it.day_index": item_index}],
    )
    return await db[COLLECTION].find_one({**scope, "plan_id": plan_id}, {"_id": 0})


async def mark_acted_on_v2(
    plan_id: str, item_index: int, draft_ids: List[str], user_id: str, db: AsyncIOMotorDatabase, brand_id: Optional[str] = None,
) -> None:
    scope = _cal_v2_scope(user_id, brand_id)
    await db[COLLECTION].update_one(
        {**scope, "plan_id": plan_id},
        {
            "$set": {"items.$[it].acted_on": True},
            "$push": {"items.$[it].acted_on_draft_ids": {"$each": draft_ids}},
        },
        array_filters=[{"it.day_index": item_index}],
    )


async def sync_item_performance(
    plan_id: str, user_id: str, db: AsyncIOMotorDatabase, brand_id: Optional[str] = None,
) -> int:
    """Manual/cron-triggered performance feedback — pulls metrics for every
    acted-on item's linked drafts and stores a snapshot for user-facing
    display. Not a live webhook. This is the ONLY function in this file
    allowed to touch performance data, and only AFTER publication — it must
    never feed back into generate_plan_v2 or any of the pipeline above
    (PRD §2, §15)."""
    scope = _cal_v2_scope(user_id, brand_id)
    plan = await db[COLLECTION].find_one({**scope, "plan_id": plan_id}, {"_id": 0, "items": 1})
    if not plan:
        raise ValueError("Plan not found")

    synced = 0
    for item in plan["items"]:
        if not item.get("acted_on") or not item.get("acted_on_draft_ids"):
            continue
        draft_id = item["acted_on_draft_ids"][0]
        draft = await db["content_drafts"].find_one({"id": draft_id}, {"_id": 0, "performance_metrics": 1, "metrics": 1})
        metrics = (draft or {}).get("performance_metrics") or (draft or {}).get("metrics") or {}
        if not metrics:
            continue
        await db[COLLECTION].update_one(
            {**scope, "plan_id": plan_id},
            {"$set": {
                "items.$[it].performance": {
                    "draft_id": draft_id, "metrics_snapshot": metrics,
                    "last_synced_at": datetime.utcnow().isoformat(),
                },
            }},
            array_filters=[{"it.day_index": item["day_index"]}],
        )
        synced += 1
    return synced
