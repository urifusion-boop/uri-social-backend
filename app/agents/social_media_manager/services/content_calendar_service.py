"""
Content Calendar Service
Generates and manages 7-day AI content plans based on brand profile data.
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.AIService import AIService
from app.domain.models.chat_model import ChatModel

COLLECTION = "content_calendar_plans"

CONTENT_TYPES = ["educational", "relatable", "promotional", "behind_the_scenes", "engagement"]

# Default 7-day content mix (index = day_index 0-6, Mon-Sun)
DEFAULT_MIX = [
    "educational",
    "relatable",
    "promotional",
    "educational",
    "behind_the_scenes",
    "relatable",
    "engagement",
]

# Industry-specific mix overrides
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


def _pick_mix(industry: str) -> List[str]:
    industry_lower = (industry or "").lower()
    for key, mix in INDUSTRY_MIX.items():
        if key in industry_lower:
            return mix
    return DEFAULT_MIX


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
        avoid_repeat_block = f"\nTopics used last week (do NOT repeat these):\n" + "\n".join(f"- {t}" for t in previous_titles[:14])

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
{avoid_repeat_block}

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
- Be SPECIFIC to this brand — use real product/service names, real audience pain points, real industry context
- Promotional: highlight a genuine product/service benefit with a clear value statement
- Educational: share an insight directly relevant to this brand's industry and audience
- Relatable: tap into a real emotion or experience the target audience would recognise
- Engagement: pose a specific question or poll that this brand's followers would genuinely answer
- Behind the scenes: show real work, process, or people behind THIS brand specifically
- Match the brand voice exactly — if casual, be casual; if bold, be bold
- Never be generic — every idea should be impossible to copy-paste to a different brand
"""

    ai_request = AIService.build_ai_model(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
    )
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

    # Archive existing active plan for this week if force=True
    if force:
        await db[COLLECTION].update_many(
            {"user_id": user_id, "week_start": week_start, "status": "active"},
            {"$set": {"status": "archived"}},
        )
    else:
        existing = await get_active_plan(user_id, db, week_start)
        if existing:
            return existing

    industry = brand.get("industry", "")
    mix = _pick_mix(industry)

    # Fetch last week's titles to avoid repeating topics
    last_monday = (monday - timedelta(days=7)).strftime("%Y-%m-%d")
    last_plan = await db[COLLECTION].find_one(
        {"user_id": user_id, "week_start": last_monday},
        {"_id": 0, "days": 1},
    )
    previous_titles = [d.get("title", "") for d in (last_plan or {}).get("days", []) if d.get("title")]

    ideas = await _generate_ideas(brand, mix, week_start, platforms, previous_titles=previous_titles)

    days = []
    for i, idea in enumerate(ideas):
        day_date = (monday + timedelta(days=i)).strftime("%Y-%m-%d")
        days.append({
            "day_index": i,
            "date": day_date,
            "content_type": mix[i],
            "title": idea.get("title", ""),
            "description": idea.get("description", ""),
            "platforms": platforms,
            "acted_on": False,
            "acted_on_draft_ids": [],
            "regenerated_count": 0,
            "last_regenerated_at": None,
        })

    plan_id = str(uuid.uuid4())
    doc = {
        "plan_id": plan_id,
        "user_id": user_id,
        "week_start": week_start,
        "generated_at": now.isoformat(),
        "status": "active",
        "platforms": platforms,
        "days": days,
        "content_mix": _compute_mix_ratios(mix),
        "brand_snapshot": {
            "brand_name": brand.get("brand_name"),
            "industry": brand.get("industry"),
            "tagline": brand.get("tagline"),
            "business_description": brand.get("business_description") or brand.get("product_description"),
            "key_products_services": brand.get("key_products_services") or [],
            "brand_voice": brand.get("brand_voice") or brand.get("derived_voice"),
            "voice_sample": brand.get("voice_sample"),
            "platform_tones": brand.get("platform_tones") or {},
            "same_tone_everywhere": brand.get("same_tone_everywhere", True),
            "target_audience": brand.get("target_audience"),
            "audience_age_range": brand.get("audience_age_range"),
            "primary_goal": brand.get("primary_goal"),
            "region": brand.get("region"),
            "content_pillars": brand.get("content_pillars") or [],
            "preferred_formats": brand.get("preferred_formats") or [],
            "cta_styles": brand.get("cta_styles") or [],
            "guardrails": brand.get("guardrails") or {},
            "key_dates": brand.get("key_dates"),
            "posting_cadence": brand.get("posting_cadence"),
            "website": brand.get("website"),
        },
    }
    await db[COLLECTION].insert_one({**doc, "_id": plan_id})
    return doc


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
