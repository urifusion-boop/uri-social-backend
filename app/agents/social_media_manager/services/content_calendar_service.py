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
) -> List[Dict[str, Any]]:
    """Call AI once for all 7 day ideas. Returns list of 7 dicts."""
    brand_name = brand.get("brand_name") or "the brand"
    industry = brand.get("industry") or "business"
    voice = brand.get("derived_voice") or brand.get("brand_voice") or "professional and engaging"
    audience = brand.get("target_audience") or "general audience"
    pillars = brand.get("content_pillars") or []
    pillars_str = ", ".join(pillars) if pillars else "not specified"
    platforms_str = ", ".join(platforms) if platforms else "social media"

    days_block = "\n".join(
        f"Day {i} ({WEEK_DAYS[i]}) → type: {mix[i]} ({CONTENT_TYPE_LABELS[mix[i]]})"
        for i in range(7)
    )

    prompt = f"""You are a social media strategist for {brand_name}, a {industry} brand.
Brand voice: {voice}
Target audience: {audience}
Content pillars: {pillars_str}
Platforms: {platforms_str}
Week starting: {week_start}

Generate a 7-day social media content plan. For each day produce:
- title: a short, specific content idea title (max 10 words)
- description: 1-2 sentences expanding the idea with concrete detail

Day assignments (you must use these content types exactly):
{days_block}

Return ONLY a valid JSON array of exactly 7 objects in this format:
[
  {{"day_index": 0, "title": "...", "description": "..."}},
  {{"day_index": 1, "title": "...", "description": "..."}},
  ...
  {{"day_index": 6, "title": "...", "description": "..."}}
]

Rules:
- Make each idea specific and actionable, not generic
- Match the brand voice and audience
- Promotional ideas should highlight a real product/service value
- Educational ideas should share a genuinely useful insight
- Relatable ideas should feel human and real
- Engagement ideas should include a question or poll prompt
- Behind the scenes ideas should show the real work behind the brand
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

    ideas = await _generate_ideas(brand, mix, week_start, platforms)

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
            "brand_voice": brand.get("derived_voice") or brand.get("brand_voice"),
            "target_audience": brand.get("target_audience"),
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
    industry = brand.get("brand_voice") or brand.get("industry") or "business"
    voice = brand.get("brand_voice") or "professional and engaging"
    audience = brand.get("target_audience") or "general audience"
    platforms_str = ", ".join(platforms) if platforms else "social media"
    day_name = WEEK_DAYS[day_index]

    prompt = f"""You are a social media strategist for {brand_name}.
Brand voice: {voice}
Target audience: {audience}
Platforms: {platforms_str}

Generate ONE new {CONTENT_TYPE_LABELS[content_type]} content idea for {day_name}.
Make it different from: "{day.get('title', '')}"

Return ONLY a valid JSON object:
{{"title": "...", "description": "..."}}

The title should be max 10 words. The description should be 1-2 sentences with concrete detail.
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
