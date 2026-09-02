# app/agents/content_calendar_v2/services/content_calendar_v2_service.py
"""
Content Calendar V2 — 30-day content intelligence engine.

Staging-only, fully isolated from the v1 7-day Content Calendar
(app/agents/social_media_manager/services/content_calendar_service.py):
own collection (content_calendar_v2_plans), own package, own router prefix.
Scoped to the PRD's own §48 MVP list, built in §54 priority order — see
/Users/macintoshhd/.claude/plans/enchanted-wiggling-treehouse.md for the
full plan this was built against.

Reuses v1's pure signal-gathering/validation building blocks by import
(never forked) — v1's own services already implement Layers 2/3/4
(Performance/Trend/Calendar Intelligence) correctly:
  - PerformanceAnalyticsService, TrendDataService, HolidayCalendarService,
    CulturalMomentService, IndustryTrendService, ContentExplainerService
  - _STAGE_GUIDANCE, _business_pulse_freshness_str, _validate_day,
    _pick_mix_from_performance, DEFAULT_MIX_VARIANTS, INDUSTRY_MIX,
    POST_FORMATS, HOOK_STYLES, POST_FORMAT_TO_KEY, CONTENT_TYPES,
    CONTENT_TYPE_LABELS

Genuinely new here (v1 has no equivalent): chunked 30-day generation,
carousel-slot assignment, ad-opportunity scoring + copy, creative-diversity
validation, append-only versioning (mirrors blog_generation_service.py's
edit_history $push pattern), approval, performance sync.
"""
from __future__ import annotations

import json
import random
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.AIService import AIService
from app.services.TrendDataService import TrendDataService
from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService

from app.agents.social_media_manager.services.holiday_calendar_service import HolidayCalendarService
from app.agents.social_media_manager.services.content_explainer_service import ContentExplainerService
from app.agents.social_media_manager.services.cultural_moment_service import CulturalMomentService
from app.agents.social_media_manager.services.industry_trend_service import IndustryTrendService

# Pure, stateless helpers reused directly from v1 — see module docstring.
from app.agents.social_media_manager.services.content_calendar_service import (
    _STAGE_GUIDANCE,
    _business_pulse_freshness_str,
    _validate_day,
    _pick_mix_from_performance,
    DEFAULT_MIX_VARIANTS,
    POST_FORMATS,
    HOOK_STYLES,
    POST_FORMAT_TO_KEY,
    CONTENT_TYPES,
    CONTENT_TYPE_LABELS,
)

from ..models import AdCopyV2, AdOpportunityV2

COLLECTION = "content_calendar_v2_plans"
PLAN_DAYS = 30
CHUNK_SIZE = 6          # ~5 chunks of 6 for 30 days — v1's own 7-item cap is
                         # evidence larger single structured-JSON calls degrade
CAROUSEL_COUNT = 3      # PRD §9 — exactly 3, each exactly 3 slides, no exceptions


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


# ── Carousel slot assignment (deterministic, no LLM call — PRD §9) ─────────────

def _assign_carousel_slots(
    content_type_mix: List[str],
    holiday_dates: List[str],
) -> List[int]:
    """Pick exactly CAROUSEL_COUNT day-indexes (0..PLAN_DAYS-1) for carousels,
    spread across different weeks, biased toward content types that suit a
    3-slide how-to/comparison structure and toward holiday-adjacent days."""
    chunk_of = lambda i: i // 7  # noqa: E731 — which ~week a day falls in

    def score(i: int) -> float:
        ct = content_type_mix[i]
        s = 0.0
        if ct == "educational":
            s += 3.0
        elif ct == "promotional":
            s += 1.5
        elif ct == "relatable":
            s += 1.0
        if holiday_dates and i < len(holiday_dates) and holiday_dates[i]:
            s += 1.5
        return s

    ranked = sorted(range(PLAN_DAYS), key=score, reverse=True)
    chosen: List[int] = []
    used_chunks: set = set()
    for i in ranked:
        if len(chosen) >= CAROUSEL_COUNT:
            break
        c = chunk_of(i)
        if c in used_chunks:
            continue
        chosen.append(i)
        used_chunks.add(c)
    # Fallback: if fewer than 3 distinct chunks had a candidate (very small
    # plan windows, edge case), fill remaining slots from whatever's left.
    if len(chosen) < CAROUSEL_COUNT:
        for i in ranked:
            if len(chosen) >= CAROUSEL_COUNT:
                break
            if i not in chosen:
                chosen.append(i)
    return sorted(chosen[:CAROUSEL_COUNT])


# ── Ad-opportunity scoring (rule-based, PRD §19) ────────────────────────────────

def _score_ad_opportunity(
    item: Dict[str, Any],
    performance: Dict[str, Any],
    near_holiday: bool,
) -> float:
    score = 0.0
    if item.get("content_type") == "promotional":
        score += 45.0
    elif item.get("content_type") == "educational":
        score += 15.0
    avg_by_topic = (performance or {}).get("avg_engagement_by_topic") or {}
    title_lower = (item.get("title") or "").lower()
    for topic, eng in avg_by_topic.items():
        if topic.lower() in title_lower:
            score += min(eng, 30.0)
            break
    if near_holiday and item.get("content_type") in ("promotional", "educational"):
        score += 10.0
    if item.get("cta"):
        score += 10.0
    return min(round(score, 1), 100.0)


_ANGLE_BY_CONTENT_TYPE: Dict[str, str] = {
    "promotional": "offer",
    "educational": "problem_first",
    "relatable": "outcome_first",
    "engagement": "social_proof",
    "behind_the_scenes": "social_proof",
}


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

    prompt = f"""Adapt this organic social post idea into paid-ad copy for {brand_name}.

Organic idea: "{item.get('title', '')}"
Hook: "{item.get('hook', '')}"
Key points: {', '.join(str(p) for p in (item.get('key_points') or [])[:4])}
{f'USP: {usp}' if usp else ''}

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
write around it instead (PRD ad-safety rule: no fabricated claims).

Return ONLY valid JSON: {{"headline": "...", "primary_text": "...", "short_copy": "...", "cta": "...", "image_prompt": "..."}}
"""
    try:
        ai_request = AIService.build_ai_model(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o",
            temperature=0.8,
        )
        response = await AIService.chat_completion(ai_request)
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        return AdCopyV2(**{k: data.get(k, "") for k in ("headline", "primary_text", "short_copy", "cta", "image_prompt")})
    except Exception as exc:
        print(f"[CalendarV2] ad copy generation failed: {exc}", flush=True)
        return AdCopyV2()


# ── Creative diversity validation (rule-based + LLM self-check, PRD §18) ───────

def _rule_based_diversity_issues(items: List[Dict[str, Any]]) -> Dict[int, str]:
    """Deterministic half of the diversity check: duplicate/near-duplicate
    hook openings, and repeated content_type+format on adjacent days."""
    issues: Dict[int, str] = {}
    seen_openings: Dict[str, int] = {}
    for i, item in enumerate(items):
        hook = str(item.get("hook") or "").strip().lower()
        opening = " ".join(hook.split()[:5])
        if opening and opening in seen_openings:
            issues[i] = f"hook opens the same way as day {seen_openings[opening]}"
        elif opening:
            seen_openings[opening] = i
        if i > 0:
            prev = items[i - 1]
            if (item.get("content_type") == prev.get("content_type")
                    and item.get("format") == prev.get("format")):
                issues[i] = issues.get(i, "") + "; repeats prior day's content_type+format pair"
    return issues


async def _llm_diversity_check(items: List[Dict[str, Any]]) -> List[int]:
    """One extra LLM call across all items' titles/hooks/key_points asking
    which pairs are substantially the same idea reworded — a cheap stand-in
    for embedding-based semantic similarity (real embeddings noted as a
    fast-follow in the plan, not built this pass)."""
    listing = "\n".join(
        f"{i}: {it.get('title', '')} — {it.get('hook', '')}"
        for i, it in enumerate(items)
    )
    prompt = f"""Below are {len(items)} social media post ideas for one business. Which
indexes, if any, are substantially the SAME underlying idea reworded (not
just sharing a content_type — genuinely the same angle/message)?

{listing}

Return ONLY a JSON array of indexes that should be regenerated because they
duplicate another idea in the list, e.g. [4, 11] or [] if none duplicate.
"""
    try:
        ai_request = AIService.build_ai_model(
            messages=[{"role": "user", "content": prompt}], model="gpt-4o", temperature=0.3,
        )
        response = await AIService.chat_completion(ai_request)
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw.strip())
        return [i for i in parsed if isinstance(i, int) and 0 <= i < len(items)]
    except Exception as exc:
        print(f"[CalendarV2] LLM diversity check failed (non-fatal): {exc}", flush=True)
        return []


# ── Chunked generation ──────────────────────────────────────────────────────────

def _validate_item_v2(idea: Dict[str, Any], is_carousel: bool) -> List[str]:
    """Extends v1's _validate_day with the new MVP-required fields."""
    issues = _validate_day(idea)
    if not str(idea.get("ai_image_prompt") or "").strip():
        issues.append("ai_image_prompt is empty")
    if not str(idea.get("reasoning") or "").strip():
        issues.append("reasoning is empty")
    if not idea.get("primary_kpi"):
        issues.append("primary_kpi is empty")
    if is_carousel:
        slides = ((idea.get("carousel") or {}).get("slides")) or []
        if len(slides) != 3:
            issues.append(f"carousel must have exactly 3 slides, got {len(slides)}")
    return issues


async def _generate_chunk_items(
    brand: Dict[str, Any],
    chunk_content_types: List[str],
    chunk_formats: List[str],   # "carousel" for slots picked by _assign_carousel_slots
    chunk_hooks: List[str],
    chunk_dates: List[str],     # "YYYY-MM-DD", index-aligned
    day_offset: int,            # absolute plan-day index of chunk[0]
    platforms: List[str],
    previous_titles: List[str],
    previous_key_points: List[str],
    trend_keywords: List[Dict[str, Any]],
    performance: Dict[str, Any],
    force: bool = False,
) -> List[Dict[str, Any]]:
    n = len(chunk_content_types)
    brand_name = brand.get("brand_name") or "the brand"
    industry = brand.get("industry") or "business"
    voice = brand.get("brand_voice") or brand.get("derived_voice") or "professional and engaging"
    audience = brand.get("target_audience") or "general audience"
    platforms_str = ", ".join(platforms) if platforms else "social media"
    tagline = brand.get("tagline", "")
    description = brand.get("business_description") or brand.get("product_description", "")
    region = brand.get("region", "")
    business_stage = brand.get("business_stage", "")
    usp = brand.get("unique_selling_proposition", "")
    price_range = brand.get("price_range", "")
    business_pulse = brand.get("business_pulse") or {}
    business_pulse_updated_at = brand.get("business_pulse_updated_at")

    stage_block = (
        f"Business stage: {business_stage} — {_STAGE_GUIDANCE.get(business_stage, '')}"
        if business_stage else ""
    )
    bp_freshness = _business_pulse_freshness_str(business_pulse_updated_at)
    bp_lines = [v for k, v in {
        "goal": business_pulse.get("current_period_goal"),
        "promotions": ", ".join(business_pulse.get("current_promotions") or []),
        "campaigns": ", ".join(business_pulse.get("current_campaigns") or []),
        "new products": ", ".join(business_pulse.get("new_products_services") or []),
        "milestones": ", ".join(business_pulse.get("recent_milestones") or []),
    }.items() if v]
    business_pulse_block = ""
    if bp_lines:
        freshness_note = f" ({bp_freshness})" if bp_freshness else ""
        business_pulse_block = f"Current business pulse{freshness_note}: " + "; ".join(bp_lines)

    days_block = "\n".join(
        f"Item {i} ({chunk_dates[i]}) → type: {chunk_content_types[i]} "
        f"({CONTENT_TYPE_LABELS.get(chunk_content_types[i], chunk_content_types[i])}) | "
        f"hook style: {chunk_hooks[i]} | "
        f"format: {'CAROUSEL — exactly 3 slides, PRD-mandated' if chunk_formats[i] == 'carousel' else chunk_formats[i]}"
        for i in range(n)
    )

    avoid_block = ""
    if previous_titles:
        avoid_block = "\nAlready used (do not repeat these ideas or angles):\n" + "\n".join(f"- {t}" for t in previous_titles[:40] if t)
    if previous_key_points:
        avoid_block += "\nUnderlying points already covered (do not reuse the substance):\n" + "\n".join(f"- {p}" for p in previous_key_points[:40] if p)

    market_intel_block = ""
    if trend_keywords:
        market_intel_block = "Trending keywords (use as angles where relevant):\n" + "\n".join(
            f"  - {kw.get('keyword')}" for kw in trend_keywords[:6]
        )

    performance_block = ""
    if performance and performance.get("has_data"):
        top_topics = performance.get("top_topics", [])
        if top_topics:
            performance_block = (
                f"PROVEN TOP TOPICS (real engagement history — weight heavily): {', '.join(top_topics[:5])}"
            )

    force_token = f"\n[Regen token: {secrets.token_hex(6)}] Produce genuinely different ideas from any prior generation.\n" if force else ""

    exact_copy_spec = """
- exact_copy: {"headline": "publish-ready headline/first-line", "caption": "the FULL publish-ready caption text, ready to post as-is", "hashtags": ["2-5 relevant hashtags, no # symbol"]}"""
    carousel_spec = """
- carousel: null UNLESS this item's format is CAROUSEL, in which case:
  {"slides": [
    {"slide_index": 0, "headline": "hook slide headline", "body": "hook slide body text", "visual_note": "what the slide should show"},
    {"slide_index": 1, "headline": "core info/insight headline", "body": "the substance", "visual_note": "..."},
    {"slide_index": 2, "headline": "conclusion + CTA headline", "body": "wrap-up + CTA", "visual_note": "..."}
  ]} — exactly 3 slides, no more, no fewer (PRD §9, non-negotiable)."""

    prompt = f"""You are a senior social media strategist producing part of a 30-day
content plan for {brand_name}{f' ("{tagline}")' if tagline else ''}.
Industry: {industry}. {f'What they do: {description}.' if description else ''}
Target audience: {audience}{f', {region} market' if region else ''}.
Brand voice: {voice}.
{f'USP: {usp}.' if usp else ''}
{f'Price positioning: {price_range}.' if price_range else ''}
{stage_block}
{business_pulse_block}
{performance_block}
{market_intel_block}
Platforms: {platforms_str}
{avoid_block}
{force_token}

Produce {n} COMPLETE, ready-to-publish content items — every field below is
required, no field may be a placeholder:

- title: max 10 words, punchy, specific to this brand
- hook: exact opening line (1 sentence)
- key_points: 2-5 concrete specific points
- description: 2-3 sentences tying the idea together
- caption_direction: 1-2 sentences of specific guidance for the caption
- keywords: 2-4 real keywords specific to this idea
- cta: one specific call-to-action sentence
- video_idea: {{"format": one of talking_head|product_demo|testimonial|tutorial|behind_the_scenes|trend_based, "hook": "...", "talking_points": ["..."], "scenes": ["..."], "cta": "..."}}
- holiday_reference: null unless a real, relevant holiday/observance genuinely
  falls on this item's date for {region or 'the audience region'} — never invent one{exact_copy_spec}{carousel_spec}
- ai_image_prompt: 1 concrete sentence describing the ideal AI-generated image
  for this post (subject, style, mood) — usable directly as an image-gen prompt
- creative_direction: {{"visual_style": "...", "mood": "...", "color_note": "...", "composition_note": "..."}}
- reasoning: 1-2 sentences on WHY this specific idea, for THIS day — reference
  a real signal above (a proven topic, a trend keyword, the business stage, or
  a gap in recent content) — this is shown to the user as "why this post?", so
  it must name something concrete, never generic filler like "this will engage your audience"
- primary_kpi: one of reach|engagement|leads|sales|awareness — whichever this
  specific item is actually optimized for

Never fabricate a specific statistic, named testimonial, exact customer count,
or price that wasn't given to you above — write around missing specifics
instead of inventing them.

Item assignments (follow type, hook style, and format exactly):
{days_block}

Return ONLY a valid JSON array of exactly {n} objects, each with day_offset
set to its absolute plan-day index:
[
  {{"day_offset": {day_offset}, "title": "...", "hook": "...", "key_points": ["..."], "description": "...",
    "caption_direction": "...", "keywords": ["..."], "cta": "...",
    "video_idea": {{"format": "talking_head", "hook": "...", "talking_points": ["..."], "scenes": ["..."], "cta": "..."}},
    "holiday_reference": null, "exact_copy": {{"headline": "...", "caption": "...", "hashtags": ["..."]}},
    "carousel": null, "ai_image_prompt": "...", "creative_direction": {{"visual_style": "...", "mood": "...", "color_note": "...", "composition_note": "..."}},
    "reasoning": "...", "primary_kpi": "engagement"}},
  ...
]

Rules: no two titles share an opening word; vary emotional tone across items;
be specific — real product/service names, real audience details; every item
must be impossible to copy-paste to a different brand.
"""

    async def _call_and_parse(full_prompt: str) -> List[Dict[str, Any]]:
        ai_request = AIService.build_ai_model(
            messages=[{"role": "user", "content": full_prompt}],
            model="gpt-4o",
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
        parsed = json.loads(raw.strip())
        if not isinstance(parsed, list) or len(parsed) != n:
            raise ValueError(f"expected {n} items, got: {raw[:200]}")
        return parsed

    correction_block = ""
    items: List[Dict[str, Any]] = []
    for attempt in range(2):
        try:
            items = await _call_and_parse(prompt + correction_block)
        except Exception as exc:
            if attempt == 0:
                raise
            print(f"[CalendarV2] chunk retry failed to parse ({exc}) — using best-effort", flush=True)
            break

        failures: Dict[int, List[str]] = {}
        for i, idea in enumerate(items):
            issues = _validate_item_v2(idea, is_carousel=(chunk_formats[i] == "carousel"))
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

    for i, idea in enumerate(items):
        idea["assigned_format"] = "carousel" if chunk_formats[i] == "carousel" else POST_FORMAT_TO_KEY.get(chunk_formats[i], "image")
    return items


# ── Main generation ──────────────────────────────────────────────────────────

async def get_active_plan(
    user_id: str,
    db: AsyncIOMotorDatabase,
    brand_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    scope = _cal_v2_scope(user_id, brand_id)
    return await db[COLLECTION].find_one({**scope, "status": "active"}, {"_id": 0}, sort=[("created_at", -1)])


async def generate_plan_v2(
    user_id: str,
    platforms: List[str],
    brand: Dict[str, Any],
    db: AsyncIOMotorDatabase,
    force: bool = False,
    brand_id: Optional[str] = None,
) -> Dict[str, Any]:
    scope = _cal_v2_scope(user_id, brand_id)
    now = datetime.utcnow()
    period_start = _get_period_start(now)
    period_end = period_start + timedelta(days=PLAN_DAYS - 1)

    if not force:
        existing = await get_active_plan(user_id, db, brand_id=brand_id)
        if existing:
            return existing
    else:
        await db[COLLECTION].update_many({**scope, "status": "active"}, {"$set": {"status": "archived"}})

    industry = brand.get("industry", "")
    region = brand.get("region", "")

    previous_titles: List[str] = []
    previous_key_points: List[str] = []
    async for past in db[COLLECTION].find({**scope}, {"_id": 0, "items.title": 1, "items.key_points": 1}).sort("created_at", -1).limit(2):
        for it in past.get("items", []):
            if it.get("title"):
                previous_titles.append(it["title"])
            previous_key_points += [str(p) for p in (it.get("key_points") or []) if p]

    performance = await PerformanceAnalyticsService.get_user_performance(user_id, db)
    trend_keywords = await TrendDataService.get_trending_keywords(industry, db=db)

    # Signals gathered once per week-chunk across the 30-day window (these
    # services are shaped for a 7-day window, same as v1 — call once per chunk).
    all_dates = [(period_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(PLAN_DAYS)]
    holidays_by_date: Dict[str, Dict[str, Any]] = {}
    cultural_moments_all: List[Dict[str, Any]] = []
    for chunk_start_idx in range(0, PLAN_DAYS, 7):
        chunk_week_start = all_dates[chunk_start_idx]
        for h in HolidayCalendarService.get_upcoming_holidays(chunk_week_start, region, industry) or []:
            holidays_by_date[h["date"]] = h
        cultural_moments_all += CulturalMomentService.get_trending_topics(industry, region, chunk_week_start) or []
    industry_best_practices = IndustryTrendService.get_industry_best_practices(industry)

    # ── Content-type mix, per-week-chunk (preserves v1's non-fixed-percentage property) ──
    content_type_mix: List[str] = []
    for chunk_idx, chunk_start_idx in enumerate(range(0, PLAN_DAYS, 7)):
        week_number = (period_start.isocalendar()[1] + chunk_idx) if not force else secrets.randbelow(52)
        week_mix = _pick_mix_from_performance(performance, industry, brand, week_number=week_number)
        content_type_mix += week_mix
    content_type_mix = content_type_mix[:PLAN_DAYS]

    holiday_flags = [1 if all_dates[i] in holidays_by_date else 0 for i in range(PLAN_DAYS)]
    carousel_slots = _assign_carousel_slots(content_type_mix, holiday_flags)

    # ── Format + hook-style rotation, per chunk ─────────────────────────────
    formats: List[str] = []
    hooks: List[str] = []
    for chunk_start_idx in range(0, PLAN_DAYS, CHUNK_SIZE):
        chunk_len = min(CHUNK_SIZE, PLAN_DAYS - chunk_start_idx)
        shuffled_formats = (POST_FORMATS * ((chunk_len // len(POST_FORMATS)) + 1))[:chunk_len]
        shuffled_hooks = (HOOK_STYLES * ((chunk_len // len(HOOK_STYLES)) + 1))[:chunk_len]
        random.shuffle(shuffled_formats)
        random.shuffle(shuffled_hooks)
        formats += shuffled_formats
        hooks += shuffled_hooks
    for slot in carousel_slots:
        formats[slot] = "carousel"

    # ── Generate in chunks ───────────────────────────────────────────────────
    all_items: List[Dict[str, Any]] = []
    running_titles = list(previous_titles)
    running_key_points = list(previous_key_points)
    for chunk_start_idx in range(0, PLAN_DAYS, CHUNK_SIZE):
        chunk_len = min(CHUNK_SIZE, PLAN_DAYS - chunk_start_idx)
        try:
            chunk_items = await _generate_chunk_items(
                brand=brand,
                chunk_content_types=content_type_mix[chunk_start_idx:chunk_start_idx + chunk_len],
                chunk_formats=formats[chunk_start_idx:chunk_start_idx + chunk_len],
                chunk_hooks=hooks[chunk_start_idx:chunk_start_idx + chunk_len],
                chunk_dates=all_dates[chunk_start_idx:chunk_start_idx + chunk_len],
                day_offset=chunk_start_idx,
                platforms=platforms,
                previous_titles=running_titles,
                previous_key_points=running_key_points,
                trend_keywords=trend_keywords or [],
                performance=performance,
                force=force,
            )
        except Exception as exc:
            print(f"[CalendarV2] chunk at offset {chunk_start_idx} failed: {exc}", flush=True)
            chunk_items = []
        all_items += chunk_items
        running_titles += [it.get("title", "") for it in chunk_items if it.get("title")]
        for it in chunk_items:
            running_key_points += [str(p) for p in (it.get("key_points") or []) if p]

    if not all_items:
        raise RuntimeError("Content Calendar V2 generation failed for every chunk — no items produced.")

    # ── Diversity check (rule-based + one LLM self-check pass) ──────────────
    rule_issues = _rule_based_diversity_issues(all_items)
    llm_flagged = await _llm_diversity_check(all_items)
    flagged_set = set(rule_issues.keys()) | set(llm_flagged)

    # ── Ad opportunity scoring ────────────────────────────────────────────────
    items_out: List[Dict[str, Any]] = []
    for i, idea in enumerate(all_items):
        day_index = idea.get("day_offset", i)
        if not isinstance(day_index, int) or not (0 <= day_index < PLAN_DAYS):
            day_index = i
        date_str = all_dates[day_index] if day_index < len(all_dates) else all_dates[i]
        is_carousel = day_index in carousel_slots

        near_holiday = date_str in holidays_by_date
        ad_score = _score_ad_opportunity(
            {"content_type": content_type_mix[day_index] if day_index < len(content_type_mix) else "", "title": idea.get("title", ""), "cta": idea.get("cta", "")},
            performance, near_holiday,
        )
        ad_opportunity: Optional[Dict[str, Any]] = None
        if ad_score >= 55.0:
            angle = _ANGLE_BY_CONTENT_TYPE.get(content_type_mix[day_index] if day_index < len(content_type_mix) else "", "outcome_first")
            ad_copy = await _write_calendar_ad_copy(idea, brand, angle)
            ad_opportunity = AdOpportunityV2(
                is_ad_candidate=True, score=ad_score, angle=angle, ad_copy=ad_copy,
                reason=f"Scored {ad_score}/100 — {content_type_mix[day_index] if day_index < len(content_type_mix) else 'n/a'} content"
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

        reasoning = idea.get("reasoning") or ContentExplainerService.explain_recommendation(
            content_type=content_type_mix[day_index] if day_index < len(content_type_mix) else "educational",
            topic=idea.get("title", ""),
            post_day=(period_start + timedelta(days=day_index)).strftime("%A"),
            primary_goal=brand.get("primary_goal", "engagement"),
            historical_performance=performance if performance.get("has_data") else None,
            upcoming_holidays=list(holidays_by_date.values()),
            trending_topics=cultural_moments_all,
            industry_best_practices=industry_best_practices,
        )

        data_provenance = {
            "price_range": "known" if brand.get("price_range") else "unknown",
            "unique_selling_proposition": "known" if brand.get("unique_selling_proposition") else "unknown",
            "testimonial_or_stat_claims": "unknown",  # never sourced — never fabricate, PRD §41/§42
        }

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
            "format": idea.get("assigned_format", "image"),
            "content_type": content_type_mix[day_index] if day_index < len(content_type_mix) else "educational",
            "carousel": idea.get("carousel") if is_carousel else None,
            "creative_direction": idea.get("creative_direction") or {},
            "ai_image_prompt": idea.get("ai_image_prompt", ""),
            "exact_copy": idea.get("exact_copy") or {},
            "reasoning": reasoning,
            "data_provenance": data_provenance,
            "ad_opportunity": ad_opportunity,
            "primary_kpi": idea.get("primary_kpi", "engagement"),
            "diversity_check": {
                "passed": day_index not in flagged_set,
                "similarity_score": 1.0 if day_index in flagged_set else 0.0,
                "flagged_against_item_id": None,
            },
            "version_history": [],
            "regenerated_count": 0,
            "acted_on": False,
            "acted_on_draft_ids": [],
            "status": "pending",
            "performance": None,
        })

    items_out.sort(key=lambda it: it["day_index"])

    plan_id = str(uuid.uuid4())
    doc = {
        "plan_id": plan_id,
        "user_id": user_id,
        "brand_id": brand_id,
        "status": "active",
        "period_start": period_start.strftime("%Y-%m-%d"),
        "period_end": period_end.strftime("%Y-%m-%d"),
        "generation_method": "data_driven" if performance.get("has_data") else ("trend_driven" if trend_keywords else "ai"),
        "platforms": platforms,
        "carousel_slots": carousel_slots,
        "intelligence_snapshot": {
            "performance_summary": {"has_data": performance.get("has_data", False), "top_topics": performance.get("top_topics", [])},
            "trend_keywords": [k.get("keyword") for k in (trend_keywords or [])[:10]],
            "holidays": list(holidays_by_date.values()),
            "cultural_moments": cultural_moments_all[:10],
            "industry_best_practices": industry_best_practices,
        },
        "content_mix": {t: round(content_type_mix.count(t) / len(content_type_mix), 2) for t in CONTENT_TYPES},
        "items": items_out,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    await db[COLLECTION].insert_one({**doc, "_id": plan_id})
    return doc


# ── Regeneration with versioning (diverges from v1's overwrite-in-place) ───────

async def regenerate_item_v2(
    plan_id: str,
    item_index: int,
    user_id: str,
    db: AsyncIOMotorDatabase,
    brand_id: Optional[str] = None,
    reason: str = "",
) -> Dict[str, Any]:
    scope = _cal_v2_scope(user_id, brand_id)
    plan = await db[COLLECTION].find_one({**scope, "plan_id": plan_id}, {"_id": 0})
    if not plan:
        raise ValueError("Plan not found")
    item = next((it for it in plan["items"] if it["day_index"] == item_index), None)
    if item is None:
        raise ValueError(f"Item {item_index} not found in plan")

    brand = {}  # V2 regenerates against the brand snapshot embedded in intelligence_snapshot's
    # absence is intentional for MVP — re-fetch the live brand profile instead, so a
    # single-item regen always reflects the CURRENT brand context (unlike v1's
    # frozen-snapshot choice) since V2 has no brand_snapshot field by design (see plan).
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    profile_result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
    raw_profile = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
    brand = BrandProfileService.to_brand_context(raw_profile) if raw_profile else {}

    other_titles = [it.get("title", "") for it in plan["items"] if it.get("title") and it["day_index"] != item_index]
    is_carousel = item.get("format") == "carousel"

    chunk_items = await _generate_chunk_items(
        brand=brand,
        chunk_content_types=[item.get("content_type", "educational")],
        chunk_formats=["carousel" if is_carousel else item.get("format", "image")],
        chunk_hooks=[random.choice(HOOK_STYLES)],
        chunk_dates=[item.get("date", "")],
        day_offset=item_index,
        platforms=plan.get("platforms") or [],
        previous_titles=other_titles,
        previous_key_points=[],
        trend_keywords=[],
        performance={},
        force=True,
    )
    if not chunk_items:
        raise RuntimeError("Regeneration produced no result")
    new_idea = chunk_items[0]

    # ── Version snapshot BEFORE overwrite — the one place V2 must diverge from
    # v1's regenerate_day, which overwrites with zero history. Mirrors
    # blog_generation_service.py's edit_history $push shape.
    editable_fields = [
        "title", "description", "hook", "key_points", "caption_direction",
        "keywords", "cta", "video_idea", "exact_copy", "carousel",
        "ai_image_prompt", "creative_direction",
    ]
    snapshot = {f: item.get(f) for f in editable_fields}
    version_entry = {"snapshot": snapshot, "edited_at": datetime.utcnow().isoformat(), "reason": reason}

    update_fields = {
        f"items.$[it].title": new_idea.get("title", ""),
        f"items.$[it].description": new_idea.get("description", ""),
        f"items.$[it].hook": new_idea.get("hook", ""),
        f"items.$[it].key_points": new_idea.get("key_points", []),
        f"items.$[it].caption_direction": new_idea.get("caption_direction", ""),
        f"items.$[it].keywords": new_idea.get("keywords", []),
        f"items.$[it].cta": new_idea.get("cta", ""),
        f"items.$[it].video_idea": new_idea.get("video_idea", {}),
        f"items.$[it].exact_copy": new_idea.get("exact_copy") or {},
        f"items.$[it].carousel": new_idea.get("carousel") if is_carousel else None,
        f"items.$[it].ai_image_prompt": new_idea.get("ai_image_prompt", ""),
        f"items.$[it].creative_direction": new_idea.get("creative_direction") or {},
        f"items.$[it].regenerated_count": item.get("regenerated_count", 0) + 1,
        "updated_at": datetime.utcnow().isoformat(),
    }
    await db[COLLECTION].update_one(
        {**scope, "plan_id": plan_id},
        {"$set": update_fields, "$push": {"items.$[it].version_history": version_entry}},
        array_filters=[{"it.day_index": item_index}],
    )
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
    """Manual/cron-triggered performance feedback (PRD §12/§48) — pulls
    metrics for every acted-on item's linked drafts and stores a snapshot.
    Not a live webhook (deferred, see plan's out-of-scope list)."""
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
