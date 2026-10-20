"""
Small, pure rules shared by the 30-day content calendar and the caption
writer. They lived in the old weekly calendar (content_calendar_service.py),
which was replaced by the 30-day calendar and then deleted.
"""
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

# What each business_stage means for tone — one source of truth for every
# prompt that uses it.
_STAGE_GUIDANCE = {
    "new": "a brand-new business — lean into trust-building, founder story, and credibility-establishing content",
    "growing": "a growing business with early traction — balance trust-building with momentum/social-proof content",
    "established": "an established business with a steady base — lean into authority, depth, and loyalty content",
    "market_leader": "a market-leading business — lean into authority, scale, and category-defining content",
}


def _business_pulse_freshness_str(updated_at: Optional[datetime]) -> Optional[str]:
    """
    Render a human-readable freshness note for Business Pulse data (current
    promotions/campaigns/news/milestones), so the model can gauge how much
    weight to give it. Called from both _generate_ideas() and regenerate_day()
    so the "how stale is stale" threshold only lives in one place.
    """
    if not updated_at:
        return None
    age_days = (datetime.utcnow() - updated_at).days
    if age_days <= 21:
        return f"as of {age_days} day{'s' if age_days != 1 else ''} ago"
    return f"as of {age_days} days ago — likely stale, treat with caution"


# Hard completeness rules for one generated content idea.
VIDEO_FORMATS = {
    "talking_head", "product_demo", "testimonial",
    "tutorial", "behind_the_scenes", "trend_based",
}

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _extract_promised_count(title: str) -> Optional[int]:
    """If a title promises a specific count ("Three things...", "5 ways...",
    "Top 4 mistakes"), return that count so key_points can be checked against
    it. None if the title doesn't make a countable claim."""
    t = title.lower()
    digit_match = re.search(r"\b(\d{1,2})\b", t)
    if digit_match:
        n = int(digit_match.group(1))
        if 2 <= n <= 10:
            return n
    for word, n in _NUMBER_WORDS.items():
        if n >= 2 and re.search(rf"\b{word}\b", t):
            return n
    return None


def _validate_day(idea: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable completeness/coherence failures for one
    AI-generated day; an empty list means the day is usable as-is."""
    issues: List[str] = []
    title = str(idea.get("title") or "").strip()
    if not title:
        issues.append("title is empty")

    hook = str(idea.get("hook") or "").strip()
    if len(hook) < 10:
        issues.append("hook is missing or too short — must be an actual opening line")

    key_points = idea.get("key_points")
    if not isinstance(key_points, list) or len(key_points) < 2:
        issues.append("key_points must be a list of at least 2 concrete points")
    else:
        thin = [p for p in key_points if len(str(p).strip()) < 15]
        if thin:
            issues.append(f"{len(thin)} key_point(s) are too thin to be usable")
        promised = _extract_promised_count(title)
        if promised and len(key_points) < promised:
            issues.append(
                f"title implies {promised} points but only {len(key_points)} key_points were given"
            )

    caption_direction = str(idea.get("caption_direction") or "").strip()
    if len(caption_direction) < 15:
        issues.append("caption_direction is missing or too short")

    keywords = idea.get("keywords")
    if not isinstance(keywords, list) or len(keywords) < 1:
        issues.append("keywords must have at least 1 entry")

    if not str(idea.get("cta") or "").strip():
        issues.append("cta is empty")

    video_idea = idea.get("video_idea")
    if not isinstance(video_idea, dict):
        issues.append("video_idea is missing")
    else:
        if video_idea.get("format") not in VIDEO_FORMATS:
            issues.append(
                f"video_idea.format {video_idea.get('format')!r} is not a recognised format — "
                f"it MUST be exactly one of: {', '.join(sorted(VIDEO_FORMATS))} (no other wording, no synonyms)"
            )
        if len(str(video_idea.get("hook") or "").strip()) < 10:
            issues.append("video_idea.hook is missing or too short")
        if not video_idea.get("talking_points"):
            issues.append("video_idea.talking_points is empty")
        if not video_idea.get("scenes"):
            issues.append("video_idea.scenes is empty")
        if not str(video_idea.get("cta") or "").strip():
            issues.append("video_idea.cta is empty")

    return issues
