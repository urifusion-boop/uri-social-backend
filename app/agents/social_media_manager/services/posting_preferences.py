"""
Brand Playbook "Posting & Workflow" settings, in one place.

The Playbook saves an approval mode, a posting cadence and posting-time
preferences. This module turns those saved values into behaviour:

- approval_mode()      — review / auto / hybrid (the Playbook page and the
                         onboarding flow saved different spellings of these).
- cadence_interval()   — how often the Auto tab generates posts.
- next_post_time()     — when a post should go out, from the preferred
                         time-of-day slots, in the brand's local time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

# ── Approval ──────────────────────────────────────────────────────────────────

REVIEW, AUTO, HYBRID = "review", "auto", "hybrid"

_APPROVAL_ALIASES = {
    "review": REVIEW, "review first": REVIEW, "review before publishing": REVIEW,
    "auto": AUTO, "auto-publish": AUTO, "auto publish": AUTO,
    "hybrid": HYBRID,
}


def approval_mode(profile: Optional[Dict[str, Any]]) -> str:
    """The brand's approval mode. Anything unknown or unset means review —
    nothing is ever published without review unless the user chose it."""
    raw = str((profile or {}).get("approval_workflow") or "").strip().lower()
    return _APPROVAL_ALIASES.get(raw, REVIEW)


def auto_publishes_recurring(profile: Optional[Dict[str, Any]]) -> bool:
    """Auto-publish and Hybrid both publish the Auto tab's recurring posts
    without review. Hand-made posts always wait as drafts."""
    return approval_mode(profile) in (AUTO, HYBRID)


# ── Cadence ───────────────────────────────────────────────────────────────────

_CADENCE_INTERVALS = {
    "daily": timedelta(days=1),
    "3–4x per week": timedelta(days=2),   # ~3.5 posts a week
    "3-4x per week": timedelta(days=2),
    "2–3x per week": timedelta(days=3),   # ~2.3 posts a week
    "2-3x per week": timedelta(days=3),
    "weekly": timedelta(weeks=1),
    "bi-weekly": timedelta(weeks=2),
    "biweekly": timedelta(weeks=2),
}


def cadence_interval(profile: Optional[Dict[str, Any]], fallback_frequency: str = "daily") -> timedelta:
    """Time between Auto-tab runs. The Playbook's posting cadence wins; without
    one, the Auto tab's own daily/weekly setting is used as before."""
    cadence = str((profile or {}).get("posting_cadence") or "").strip().lower()
    if cadence in _CADENCE_INTERVALS:
        return _CADENCE_INTERVALS[cadence]
    return timedelta(weeks=1) if fallback_frequency == "weekly" else timedelta(days=1)


# ── Posting times ─────────────────────────────────────────────────────────────

# Playbook slot label -> local hour posts go out (middle of the window).
TIME_SLOTS = {
    "Early Morning (6-9am)": 7,
    "Late Morning (9am-12pm)": 10,
    "Afternoon (12-4pm)": 13,
    "Evening (4-8pm)": 18,
    "Night (8pm-12am)": 21,
}

# "AI optimal" with no per-account data yet: commonly strong hours per platform.
_DEFAULT_HOURS = {
    "facebook": [13, 19], "instagram": [11, 19], "linkedin": [9, 12], "twitter": [9, 17],
    "x": [9, 17], "tiktok": [12, 19],
}
_FALLBACK_HOURS = [10, 18]


def brand_timezone(profile: Optional[Dict[str, Any]]) -> ZoneInfo:
    """The brand's local timezone, inferred from its Playbook region. Lagos time
    (WAT) when the region doesn't say — most URI brands are Nigerian."""
    region = str((profile or {}).get("region") or "").lower()
    if "kenya" in region or "east africa" in region:
        name = "Africa/Nairobi"
    elif "south africa" in region:
        name = "Africa/Johannesburg"
    elif "ghana" in region:
        name = "Africa/Accra"
    elif "united kingdom" in region or region in ("uk", "england"):
        name = "Europe/London"
    elif "united states" in region or region in ("us", "usa"):
        name = "America/New_York"
    elif "global" in region or "international" in region:
        name = "UTC"
    else:
        name = "Africa/Lagos"
    return ZoneInfo(name)


def _slot_hours(labels: str) -> List[int]:
    return sorted({TIME_SLOTS[l.strip()] for l in str(labels or "").split(",") if l.strip() in TIME_SLOTS})


def preferred_hours(profile: Optional[Dict[str, Any]], platform: str = "") -> List[int]:
    """Local hours this brand prefers to post at on `platform`."""
    profile = profile or {}
    prefs = profile.get("posting_time_prefs") or {}
    mode = str(profile.get("posting_time_mode") or "").lower()
    platform = (platform or "").lower()
    if mode == "custom":
        hours = _slot_hours(prefs.get(platform) or "") or _slot_hours(prefs.get("general") or "")
        if hours:
            return hours
    elif mode == "general":
        hours = _slot_hours(prefs.get("general") or "")
        if hours:
            return hours
    return _DEFAULT_HOURS.get(platform, _FALLBACK_HOURS)


def next_post_time(
    profile: Optional[Dict[str, Any]],
    platform: str = "",
    now: Optional[datetime] = None,
    min_lead: timedelta = timedelta(minutes=30),
) -> datetime:
    """The next preferred posting time for `platform`, at least `min_lead`
    from now. Returned as a naive UTC datetime, like the rest of scheduling."""
    tz = brand_timezone(profile)
    now_utc = (now or datetime.utcnow()).replace(tzinfo=timezone.utc)
    earliest = now_utc + min_lead
    local_day = earliest.astimezone(tz).date()
    for day_offset in range(0, 8):
        day = local_day + timedelta(days=day_offset)
        for hour in preferred_hours(profile, platform):
            candidate = datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz)
            if candidate >= earliest:
                return candidate.astimezone(timezone.utc).replace(tzinfo=None)
    return earliest.replace(tzinfo=None)  # unreachable with a non-empty hour list
