"""
Content Calendar Service
Generates and manages 7-day data-driven content plans.

Generation pipeline (PRD Phase 1):
  1. Fetch user performance data  (PerformanceAnalyticsService)
  2. Fetch industry trend keywords (TrendDataService)
  3. Generate + score ideas        (IdeaScoringService)
  4. Select top 7 with content mix
  Falls back to pure AI generation when performance data is sparse (<5 posts).
"""

import json
import re
import uuid
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.AIService import AIService
from app.domain.models.chat_model import ChatModel
from app.services.TrendDataService import TrendDataService
from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService
from app.services.IdeaScoringService import IdeaScoringService

COLLECTION = "content_calendar_plans"

CONTENT_TYPES = ["educational", "relatable", "promotional", "behind_the_scenes", "engagement"]

# Four default mix variants — different type distributions so the shown
# percentages rotate each week rather than always reading the same numbers.
DEFAULT_MIX_VARIANTS: List[List[str]] = [
    # Variant 0 — Educational + relatable focus (edu 29%, rel 29%)
    ["educational", "relatable", "promotional", "educational", "behind_the_scenes", "relatable", "engagement"],
    # Variant 1 — Promotional + community push (pro 29%, rel 29%)
    ["promotional", "relatable", "educational", "engagement", "relatable", "promotional", "behind_the_scenes"],
    # Variant 2 — Educational-heavy week (edu 43%)
    ["educational", "promotional", "educational", "relatable", "engagement", "educational", "behind_the_scenes"],
    # Variant 3 — Community + engagement focus (rel 29%, eng 29%)
    ["relatable", "engagement", "educational", "promotional", "relatable", "behind_the_scenes", "engagement"],
]

# Keep the original as the default for backward-compat references
DEFAULT_MIX = DEFAULT_MIX_VARIANTS[0]

# Industry-specific mix overrides — also have 4 variants per industry
INDUSTRY_MIX: Dict[str, List[str]] = {
    "e-commerce": [
        "promotional", "educational", "relatable", "promotional",
        "behind_the_scenes", "engagement", "educational",
    ],
    "technology": [
        "educational", "educational", "relatable", "behind_the_scenes",
        "promotional", "engagement", "relatable",
    ],
    "food": [
        "behind_the_scenes", "promotional", "relatable", "educational",
        "engagement", "promotional", "relatable",
    ],
    "fashion": [
        "behind_the_scenes", "promotional", "relatable", "educational",
        "promotional", "engagement", "behind_the_scenes",
    ],
    "finance": [
        "educational", "educational", "engagement", "relatable",
        "educational", "promotional", "behind_the_scenes",
    ],
    "health": [
        "educational", "relatable", "engagement", "behind_the_scenes",
        "educational", "promotional", "relatable",
    ],
    "real estate": [
        "educational", "promotional", "behind_the_scenes", "relatable",
        "promotional", "engagement", "educational",
    ],
}

CONTENT_TYPE_LABELS = {
    "educational": "Educational",
    "relatable": "Relatable",
    "promotional": "Promotional",
    "behind_the_scenes": "Behind the Scenes",
    "engagement": "Engagement / Question",
}

WEEK_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _get_monday(ref: datetime) -> datetime:
    """Return the Monday of the week containing ref (midnight UTC)."""
    dow = ref.weekday()  # 0=Mon
    monday = ref - timedelta(days=dow)
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _pick_mix(industry: str, week_number: int = 0) -> List[str]:
    industry_lower = (industry or "").lower()
    for key, mix in INDUSTRY_MIX.items():
        if key in industry_lower:
            # Rotate the industry mix by week so day assignments change,
            # then swap one type slot so the ratios shift slightly
            rotation = week_number % 7
            return mix[rotation:] + mix[:rotation]
    # Use one of the 4 variants so the reported percentages change each week
    variant_idx = week_number % len(DEFAULT_MIX_VARIANTS)
    base = DEFAULT_MIX_VARIANTS[variant_idx]
    rotation = week_number % 7
    return base[rotation:] + base[:rotation]


# Topic label → content_type bucket
_TOPIC_TO_CONTENT_TYPE: Dict[str, str] = {
    "education":   "educational",
    "finance":     "educational",
    "technology":  "educational",
    "health":      "educational",
    "real estate": "educational",
    "marketing":   "educational",
    "business":    "educational",
    "motivation":  "relatable",
    "fashion":     "relatable",
    "food":        "relatable",
    "offer":       "promotional",
    "story":       "behind_the_scenes",
}


def _pick_mix_from_performance(
    performance: Dict[str, Any],
    industry: str,
    brand: Optional[Dict[str, Any]] = None,
    week_number: int = 0,
) -> List[str]:
    """
    Build a personalised 7-day content-type mix driven by this user's
    performance data.  Falls back to the industry/default mix when there
    is no historical data.
    """
    if not performance or not performance.get("has_data"):
        return _pick_mix(industry, week_number)

    avg_by_topic: Dict[str, float] = performance.get("avg_engagement_by_topic", {})
    if not avg_by_topic:
        return _pick_mix(industry, week_number)

    # Map each measured topic → content_type and accumulate weighted scores
    type_scores: Dict[str, float] = {t: 0.0 for t in CONTENT_TYPES}
    for topic, eng in avg_by_topic.items():
        ct = _TOPIC_TO_CONTENT_TYPE.get(topic.lower(), "relatable")
        type_scores[ct] = max(type_scores[ct], eng)  # take the best topic score per type

    # Boost based on brand primary_goal
    primary_goal = ((brand or {}).get("primary_goal") or "").lower()
    if "sales" in primary_goal or "revenue" in primary_goal or "convert" in primary_goal:
        type_scores["promotional"] = type_scores["promotional"] * 1.5 + 0.1
    elif "community" in primary_goal or "relationship" in primary_goal:
        type_scores["relatable"] = type_scores["relatable"] * 1.4 + 0.1
        type_scores["behind_the_scenes"] = type_scores["behind_the_scenes"] * 1.3 + 0.1
    elif "awareness" in primary_goal or "audience" in primary_goal or "grow" in primary_goal:
        type_scores["educational"] = type_scores["educational"] * 1.4 + 0.1
        type_scores["engagement"] = type_scores["engagement"] * 1.2 + 0.1

    # Always ensure every type has a non-zero floor so it can appear
    for ct in CONTENT_TYPES:
        if type_scores[ct] == 0.0:
            type_scores[ct] = 0.5

    # Distribute 7 slots proportionally to scores, minimum 1 slot per type
    total_score = sum(type_scores.values()) or 1.0
    raw_slots = {ct: max(1, round((s / total_score) * 7)) for ct, s in type_scores.items()}

    # Adjust to exactly 7 slots
    while sum(raw_slots.values()) > 7:
        # Remove a slot from the lowest-scoring over-allocated type
        excess = [(ct, raw_slots[ct]) for ct in raw_slots if raw_slots[ct] > 1]
        excess.sort(key=lambda x: type_scores[x[0]])
        raw_slots[excess[0][0]] -= 1
    while sum(raw_slots.values()) < 7:
        # Add a slot to the highest-scoring type
        best = max(type_scores, key=type_scores.get)
        raw_slots[best] += 1

    # Build ordered 7-day list: spread types across the week sensibly
    # Order: educational early week, promotional mid-week, engagement/relatable end
    day_type_order = ["educational", "relatable", "educational", "promotional",
                      "behind_the_scenes", "engagement", "relatable"]
    mix: List[str] = []
    remaining = dict(raw_slots)
    for preferred in day_type_order:
        if remaining.get(preferred, 0) > 0:
            mix.append(preferred)
            remaining[preferred] -= 1
        else:
            # Pick the type with most remaining slots
            fallback = max(remaining, key=lambda k: remaining[k])
            mix.append(fallback)
            remaining[fallback] -= 1

    print(f"[Calendar] Personalised mix from performance: {mix} (scores: {type_scores})")
    return mix


def _compute_mix_ratios(mix: List[str]) -> Dict[str, float]:
    counts: Dict[str, int] = {t: 0 for t in CONTENT_TYPES}
    for t in mix:
        counts[t] = counts.get(t, 0) + 1
    total = len(mix) or 7
    return {t: round(c / total, 2) for t, c in counts.items()}


async def _generate_ideas(
    brand: Dict[str, Any],
    mix: List[str],
    week_start: str,
    platforms: List[str],
    previous_titles: Optional[List[str]] = None,
    trend_keywords: Optional[List[Dict[str, Any]]] = None,
    performance: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    """Call AI once for all 7 day ideas. Returns list of 7 dicts."""
    brand_name = brand.get("brand_name") or "the brand"
    industry = brand.get("industry") or "business"
    voice = brand.get("brand_voice") or brand.get("derived_voice") or "professional and engaging"
    audience = brand.get("target_audience") or "general audience"
    pillars = brand.get("content_pillars") or []
    pillars_str = ", ".join(pillars) if pillars else "not specified"
    platforms_str = ", ".join(platforms) if platforms else "social media"

    # Rich brand details
    tagline = brand.get("tagline", "")
    description = brand.get("business_description") or brand.get("product_description", "")
    products = brand.get("key_products_services") or []
    products_str = ", ".join(products) if products else ""
    formats = brand.get("preferred_formats") or []
    formats_str = ", ".join(formats) if formats else ""
    cta_styles = brand.get("cta_styles") or []
    cta_str = ", ".join(cta_styles) if cta_styles else ""
    region = brand.get("region", "")
    primary_goal = brand.get("primary_goal", "")
    audience_age = brand.get("audience_age_range", "")
    key_dates = brand.get("key_dates", "")
    guardrails = brand.get("guardrails") or {}
    avoid = guardrails.get("avoid_topics") or guardrails.get("avoid") or []
    avoid_str = ", ".join(avoid) if avoid else ""
    platform_tones = brand.get("platform_tones") or {}
    voice_sample = brand.get("voice_sample", "")
    website = brand.get("website", "")
    posting_cadence = brand.get("posting_cadence", "")

    # Build platform-specific tone block if different per platform
    platform_tone_block = ""
    same_tone = brand.get("same_tone_everywhere", True)
    if not same_tone and platform_tones:
        lines = [f"  - {p}: {t}" for p, t in platform_tones.items() if t]
        if lines:
            platform_tone_block = "Platform-specific tones:\n" + "\n".join(lines)

    days_block = "\n".join(
        f"Day {i} ({WEEK_DAYS[i]}) → type: {mix[i]} ({CONTENT_TYPE_LABELS[mix[i]]})"
        for i in range(7)
    )

    avoid_repeat_block = ""
    if previous_titles:
        non_empty = [t for t in previous_titles if t.strip()]
        if non_empty:
            avoid_repeat_block = (
                "\nRecent content titles (do NOT repeat these ideas or similar angles — "
                "every idea must feel genuinely fresh and distinct from all of these):\n"
                + "\n".join(f"- {t}" for t in non_empty[:28])
            )

    # When force-regenerating, inject a random token so the model cannot return
    # a cached/memorised response identical to the previous generation.
    force_token_block = ""
    if force:
        token = secrets.token_hex(6)
        force_token_block = f"\n[Regeneration token: {token}] This is a fresh regeneration — produce completely different ideas from any previous plan.\n"

    # ── Market Intel block (Google Trends) ────────────────────────────────────
    market_intel_block = ""
    if trend_keywords:
        top_trends = trend_keywords[:6]
        trend_lines = []
        for kw in top_trends:
            score = kw.get("trend_score", 0)
            kw_type = kw.get("type", "trending")
            growth = kw.get("growth_rate", 0)
            suffix = f" (+{growth:.0f}% on Google)" if kw_type == "rising" and growth else f" (score: {score})"
            trend_lines.append(f"  - {kw['keyword']}{suffix}")
        market_intel_block = "Current trending topics in this industry (Market Intel — prioritise these):\n" + "\n".join(trend_lines)

    # ── Performance Intelligence block ────────────────────────────────────────
    performance_block = ""
    if performance and performance.get("has_data"):
        perf_lines = []
        top_topics = performance.get("top_topics", [])
        if top_topics:
            perf_lines.append(f"Top performing topics for this account: {', '.join(top_topics[:5])}")
        top_formats = performance.get("top_formats", [])
        if top_formats:
            perf_lines.append(f"Best performing format: {top_formats[0]}")
        best_hour = performance.get("best_posting_hour")
        if best_hour is not None:
            perf_lines.append(f"Best posting time: {best_hour}:00")
        if perf_lines:
            performance_block = "Account performance signals (use these to inform content angles):\n" + "\n".join(f"  - {l}" for l in perf_lines)

    brand_block = f"Brand: {brand_name}"
    if tagline:
        brand_block += f' — "{tagline}"'
    if industry:
        brand_block += f"\nIndustry: {industry}"
    if description:
        brand_block += f"\nWhat they do: {description}"
    if products_str:
        brand_block += f"\nKey products/services: {products_str}"
    if website:
        brand_block += f"\nWebsite: {website}"

    audience_block = f"Target audience: {audience}"
    if audience_age:
        audience_block += f" (age {audience_age})"
    if region:
        audience_block += f", {region} market"
    if primary_goal:
        audience_block += f"\nPrimary business goal: {primary_goal}"

    voice_block = f"Brand voice: {voice}"
    if voice_sample:
        voice_block += f'\nVoice example: "{voice_sample[:200]}"'
    if platform_tone_block:
        voice_block += f"\n{platform_tone_block}"
    if cta_str:
        voice_block += f"\nPreferred CTAs: {cta_str}"

    extras = []
    if formats_str:
        extras.append(f"Preferred content formats: {formats_str}")
    if key_dates:
        extras.append(f"Upcoming key dates: {key_dates}")
    if posting_cadence:
        extras.append(f"Posting cadence: {posting_cadence}")
    if avoid_str:
        extras.append(f"Topics/themes to avoid: {avoid_str}")
    extras_block = "\n".join(extras)

    prompt = f"""You are a senior social media strategist creating a 7-day content plan.

{brand_block}

{audience_block}

{voice_block}

Content pillars: {pillars_str}
Platforms: {platforms_str}
Week starting: {week_start}
{extras_block}
{market_intel_block}
{performance_block}
{avoid_repeat_block}
{force_token_block}
Generate a 7-day content plan. For each day produce:
- title: a short, punchy content idea title (max 10 words) — make it feel native to the platform and brand
- description: 2-3 sentences with a concrete, specific angle. Include what to say, who it speaks to, and why it matters for this brand right now.

Day assignments (use these content types exactly):
{days_block}

Return ONLY a valid JSON array of exactly 7 objects:
[
  {{"day_index": 0, "title": "...", "description": "..."}},
  {{"day_index": 1, "title": "...", "description": "..."}},
  ...
  {{"day_index": 6, "title": "...", "description": "..."}}
]

Rules:
- GROUND each idea in the trending topics and performance signals provided above — these are real signals, not generic suggestions
- Every day must have a DIFFERENT angle, format feel, and hook — no two titles should start with the same phrase
- Never use list-post titles like "5 ways to..." or "5 mistakes..." more than once across the 7 days
- Be SPECIFIC to this brand — use real product/service names, real audience pain points, real industry context
- Promotional: highlight a genuine product/service benefit with a clear value statement
- Educational: share an insight directly relevant to this brand's industry and audience
- Relatable: tap into a real emotion or experience the target audience would recognise
- Engagement: pose a specific question or poll that this brand's followers would genuinely answer — vary the question style (poll, fill-in-the-blank, debate, personal story prompt)
- Behind the scenes: ROTATE each week between these distinct angles — workspace setup, product/service creation process, team doing actual work (NOT "Meet the team" introductions), packaging/delivery moment, before-and-after of a real project, client prep or discovery call, tool/workflow walkthrough. Do NOT default to team introduction posts.
- Match the brand voice exactly — if casual, be casual; if bold, be bold
- Never be generic — every idea should be impossible to copy-paste to a different brand
"""

    ai_request = AIService.build_ai_model(
        messages=[{"role": "user", "content": prompt}],
        model="gpt-4o" if force else "gpt-4o-mini",
        temperature=0.95 if force else 0.88,
    )
    print(f"[Calendar] _generate_ideas model={'gpt-4o' if force else 'gpt-4o-mini'} temperature={0.95 if force else 0.8} force={force}", flush=True)
    response = await AIService.chat_completion(ai_request)
    if isinstance(response, dict) and response.get("error"):
        raise ValueError(response["error"])

    raw_text = response.choices[0].message.content.strip()
    # Strip markdown fences if present
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    raw_text = raw_text.strip()

    ideas = json.loads(raw_text)
    if not isinstance(ideas, list) or len(ideas) != 7:
        raise ValueError(f"AI returned unexpected format: {raw_text[:200]}")
    return ideas


async def get_active_plan(
    user_id: str,
    db: AsyncIOMotorDatabase,
    week_start: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if week_start is None:
        week_start = _get_monday(datetime.utcnow()).strftime("%Y-%m-%d")
    doc = await db[COLLECTION].find_one(
        {"user_id": user_id, "week_start": week_start, "status": "active"},
        {"_id": 0},
    )
    return doc


async def generate_plan(
    user_id: str,
    platforms: List[str],
    brand: Dict[str, Any],
    db: AsyncIOMotorDatabase,
    force: bool = False,
) -> Dict[str, Any]:
    now = datetime.utcnow()
    monday = _get_monday(now)
    week_start = monday.strftime("%Y-%m-%d")

    previous_titles: List[str] = []

    if force:
        # Grab current week's titles so AI doesn't regenerate the same ideas
        existing_active = await get_active_plan(user_id, db, week_start)
        if existing_active:
            previous_titles = [d.get("title", "") for d in existing_active.get("days", []) if d.get("title")]
        await db[COLLECTION].update_many(
            {"user_id": user_id, "week_start": week_start, "status": "active"},
            {"$set": {"status": "archived"}},
        )
        # Bust all v2 trend cache entries for this industry so we get fresh keywords
        industry_temp = brand.get("industry", "")
        if industry_temp:
            try:
                await db["trends_cache"].delete_many(
                    {"_id": {"$regex": f"^v2:{re.escape(industry_temp.lower())}:"}}
                )
                print(f"[Calendar] Busted trend cache for '{industry_temp}' on force-regenerate")
            except Exception:
                pass
    else:
        existing = await get_active_plan(user_id, db, week_start)
        if existing:
            return existing

    industry = brand.get("industry", "")

    # Collect titles from last 4 weeks to prevent idea repetition
    for weeks_ago in range(1, 5):
        past_monday = (monday - timedelta(days=7 * weeks_ago)).strftime("%Y-%m-%d")
        past_plan = await db[COLLECTION].find_one(
            {"user_id": user_id, "week_start": past_monday},
            {"_id": 0, "days": 1},
        )
        if past_plan:
            previous_titles += [d.get("title", "") for d in past_plan.get("days", []) if d.get("title")]

    # ── Data signals ──────────────────────────────────────────────────────────
    performance = await PerformanceAnalyticsService.get_user_performance(user_id, db)
    trend_keywords = await TrendDataService.get_trending_keywords(industry, db=db)

    # ── Personalised content mix from performance data ────────────────────────
    week_number = monday.isocalendar()[1]
    if force:
        # On regenerate, pick a random variant + rotation so the type layout
        # genuinely changes (same week → same week_number, so we must randomise)
        variant_idx = secrets.randbelow(len(DEFAULT_MIX_VARIANTS))
        base = DEFAULT_MIX_VARIANTS[variant_idx][:]
        rotation = secrets.randbelow(6) + 1  # always rotate at least 1 slot
        mix = base[rotation:] + base[:rotation]
        print(f"[Calendar] Force-regen: variant={variant_idx} rotation={rotation} → mix={mix}")
    else:
        mix = _pick_mix_from_performance(performance, industry, brand, week_number=week_number)
        print(f"[Calendar] Week {week_number} → mix={mix}")

    generation_method = "ai"  # default fallback
    days: List[Dict[str, Any]] = []

    # ── AI generation (always used — grounded in trend + performance signals) ─
    try:
        if trend_keywords or performance.get("has_data"):
            generation_method = "data_driven" if performance.get("has_data") else "trend_driven"
        ai_ideas = await _generate_ideas(
            brand, mix, week_start, platforms,
            previous_titles=previous_titles,
            trend_keywords=trend_keywords or [],
            performance=performance,
            force=force,
        )
    except Exception as exc:
        print(f"[Calendar] AI generation failed: {exc}")
        ai_ideas = []

    for i, idea in enumerate(ai_ideas):
        day_date = (monday + timedelta(days=i)).strftime("%Y-%m-%d")
        # Score against real signals for transparency
        kw = idea.get("keyword", "")
        trend_score = 0
        perf_score = 0
        if trend_keywords:
            match = next((t for t in trend_keywords if t["keyword"].lower() in idea.get("title", "").lower()), None)
            if match:
                trend_score = match.get("trend_score", 0)
        if performance.get("has_data"):
            top_topics = performance.get("top_topics", [])
            title_lower = idea.get("title", "").lower()
            if any(t.lower() in title_lower for t in top_topics):
                perf_score = 75
        days.append({
            "day_index":           i,
            "date":                day_date,
            "content_type":        mix[i],
            "title":               idea.get("title", ""),
            "description":         idea.get("description", ""),
            "keyword":             kw,
            "trend_score":         trend_score,
            "performance_score":   perf_score,
            "format_score":        0,
            "final_score":         round((trend_score * 0.4) + (perf_score * 0.4), 1),
            "reason":              idea.get("reason", "Generated using Market Intel + performance data"),
            "format":              (performance.get("top_formats") or ["image"])[0],
            "platforms":           platforms,
            "acted_on":            False,
            "acted_on_draft_ids":  [],
            "regenerated_count":   0,
            "last_regenerated_at": None,
        })

    # Guard: if both pipelines failed, restore the archived plan rather than
    # persisting an empty plan that would leave the user with a broken calendar.
    if not days:
        print(f"[Calendar] Both pipelines failed for user {user_id} — restoring previous plan")
        restored = await db[COLLECTION].find_one_and_update(
            {"user_id": user_id, "week_start": week_start, "status": "archived"},
            {"$set": {"status": "active"}},
            sort=[("generated_at", -1)],
            return_document=True,
        )
        if restored:
            restored.pop("_id", None)
            return restored
        raise RuntimeError("Content generation failed and no previous plan to restore. Please try again.")

    plan_id = str(uuid.uuid4())
    doc = {
        "plan_id":          plan_id,
        "user_id":          user_id,
        "week_start":       week_start,
        "generated_at":     now.isoformat(),
        "status":           "active",
        "generation_method":generation_method,
        "platforms":        platforms,
        "days":             days,
        "content_mix":      _compute_mix_ratios(mix),
        "data_signals": {
            "post_count":       performance.get("post_count", 0),
            "top_topics":       performance.get("top_topics", []),
            "top_formats":      performance.get("top_formats", []),
            "trend_source":     trend_keywords[0].get("source", "none") if trend_keywords else "none",
            "trend_kw_count":   len(trend_keywords),
        },
        "brand_snapshot": {
            "brand_name":          brand.get("brand_name"),
            "industry":            brand.get("industry"),
            "tagline":             brand.get("tagline"),
            "business_description":brand.get("business_description") or brand.get("product_description"),
            "key_products_services":brand.get("key_products_services") or [],
            "brand_voice":         brand.get("brand_voice") or brand.get("derived_voice"),
            "voice_sample":        brand.get("voice_sample"),
            "platform_tones":      brand.get("platform_tones") or {},
            "same_tone_everywhere":brand.get("same_tone_everywhere", True),
            "target_audience":     brand.get("target_audience"),
            "audience_age_range":  brand.get("audience_age_range"),
            "primary_goal":        brand.get("primary_goal"),
            "region":              brand.get("region"),
            "content_pillars":     brand.get("content_pillars") or [],
            "preferred_formats":   brand.get("preferred_formats") or [],
            "cta_styles":          brand.get("cta_styles") or [],
            "guardrails":          brand.get("guardrails") or {},
            "key_dates":           brand.get("key_dates"),
            "posting_cadence":     brand.get("posting_cadence"),
            "website":             brand.get("website"),
        },
    }
    await db[COLLECTION].insert_one({**doc, "_id": plan_id})
    return doc


def _build_description(idea: Dict[str, Any], brand: Dict[str, Any]) -> str:
    """Build a brief description for a data-driven idea."""
    keyword  = idea.get("keyword", "")
    industry = brand.get("industry", "business")
    audience = brand.get("target_audience", "your audience")
    reason   = idea.get("reason", "")
    desc = (
        f"A {idea.get('content_type', 'content').replace('_', ' ')} post around "
        f'"{keyword}" — a trending topic in {industry} right now. '
        f"Speak directly to {audience}."
    )
    if reason:
        desc += f" Data signal: {reason}."
    return desc


async def regenerate_day(
    plan_id: str,
    day_index: int,
    user_id: str,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    plan = await db[COLLECTION].find_one(
        {"plan_id": plan_id, "user_id": user_id},
        {"_id": 0},
    )
    if not plan:
        raise ValueError("Plan not found")

    day = next((d for d in plan["days"] if d["day_index"] == day_index), None)
    if day is None:
        raise ValueError(f"Day {day_index} not found in plan")

    brand = plan.get("brand_snapshot") or {}
    mix = [d["content_type"] for d in sorted(plan["days"], key=lambda x: x["day_index"])]
    single_mix = [mix[day_index]]

    monday = datetime.strptime(plan["week_start"], "%Y-%m-%d")
    week_start = plan["week_start"]
    platforms = plan.get("platforms") or []

    # Build a targeted prompt for just one day
    content_type = mix[day_index]
    brand_name = brand.get("brand_name") or "the brand"
    industry = brand.get("industry") or "business"
    voice = brand.get("brand_voice") or "professional and engaging"
    audience = brand.get("target_audience") or "general audience"
    platforms_str = ", ".join(platforms) if platforms else "social media"
    day_name = WEEK_DAYS[day_index]
    tagline = brand.get("tagline", "")
    description = brand.get("business_description") or brand.get("product_description", "")
    products = brand.get("key_products_services") or []
    products_str = ", ".join(products) if products else ""
    pillars = brand.get("content_pillars") or []
    pillars_str = ", ".join(pillars) if pillars else ""
    voice_sample = brand.get("voice_sample", "")
    region = brand.get("region", "")
    guardrails = brand.get("guardrails") or {}
    avoid = guardrails.get("avoid_topics") or guardrails.get("avoid") or []
    avoid_str = ", ".join(avoid) if avoid else ""

    # Avoid all existing day titles in this plan
    existing_titles = [d.get("title", "") for d in plan["days"] if d.get("title") and d["day_index"] != day_index]

    brand_line = f"{brand_name}"
    if tagline:
        brand_line += f' ("{tagline}")'
    if industry:
        brand_line += f" — {industry}"
    if description:
        brand_line += f"\nWhat they do: {description}"
    if products_str:
        brand_line += f"\nProducts/services: {products_str}"

    avoid_block = ""
    if existing_titles:
        avoid_block += "\nAlready used this week (do not repeat):\n" + "\n".join(f"- {t}" for t in existing_titles)
    if avoid_str:
        avoid_block += f"\nTopics to avoid: {avoid_str}"

    prompt = f"""You are a senior social media strategist.

{brand_line}
Brand voice: {voice}{f' | Example: "{voice_sample[:150]}"' if voice_sample else ''}
Target audience: {audience}{f' | {region} market' if region else ''}
{f'Content pillars: {pillars_str}' if pillars_str else ''}
Platforms: {platforms_str}
{avoid_block}

Generate ONE new {CONTENT_TYPE_LABELS[content_type]} content idea for {day_name}.
Make it different from the previous idea: "{day.get('title', '')}"
Be specific to this brand — use real product names, audience pain points, industry context.

Return ONLY a valid JSON object:
{{"title": "...", "description": "..."}}

Title: max 10 words. Description: 2-3 sentences with a concrete, specific angle.
"""

    ai_request = AIService.build_ai_model(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.9,
    )
    response = await AIService.chat_completion(ai_request)
    if isinstance(response, dict) and response.get("error"):
        raise ValueError(response["error"])

    raw_text = response.choices[0].message.content.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    new_idea = json.loads(raw_text.strip())

    now = datetime.utcnow()
    await db[COLLECTION].update_one(
        {"plan_id": plan_id, "user_id": user_id},
        {
            "$set": {
                f"days.{day_index}.title": new_idea.get("title", ""),
                f"days.{day_index}.description": new_idea.get("description", ""),
                f"days.{day_index}.regenerated_count": day.get("regenerated_count", 0) + 1,
                f"days.{day_index}.last_regenerated_at": now.isoformat(),
            }
        },
    )

    updated = await db[COLLECTION].find_one({"plan_id": plan_id, "user_id": user_id}, {"_id": 0})
    return updated


async def mark_acted_on(
    plan_id: str,
    day_index: int,
    draft_ids: List[str],
    user_id: str,
    db: AsyncIOMotorDatabase,
) -> None:
    await db[COLLECTION].update_one(
        {"plan_id": plan_id, "user_id": user_id},
        {
            "$set": {f"days.{day_index}.acted_on": True},
            "$push": {f"days.{day_index}.acted_on_draft_ids": {"$each": draft_ids}},
        },
    )


async def get_today_suggestion(
    user_id: str,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    now = datetime.utcnow()
    week_start = _get_monday(now).strftime("%Y-%m-%d")
    plan = await get_active_plan(user_id, db, week_start)
    if not plan:
        return {"has_plan": False}

    today_str = now.strftime("%Y-%m-%d")
    today_day = next((d for d in plan["days"] if d["date"] == today_str), None)
    if today_day is None:
        return {"has_plan": True, "plan_id": plan["plan_id"], "today": None}

    return {
        "has_plan": True,
        "plan_id": plan["plan_id"],
        "day_index": today_day["day_index"],
        "today": today_day,
    }
