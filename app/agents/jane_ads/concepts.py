"""
Testing several ads in one campaign (PRD: explore → validate → scale).

Each ad a campaign tests is a genuinely different way in: a different reason to buy in
a different visual structure, or the client's own photo or video. They run in one ad
set by default, so a small budget can still test. A budget that gives each ad Meta's
daily floor can instead give each its own ad set with an equal share (ABO).
"""
from __future__ import annotations

import re
from typing import Optional

from . import constants as C
from .growth.stages import recommend_structure

ANGLES = [
    ("problem", "Lead with the problem the customer has and show this as the fix.",
     "Lay the image out like a phone Notes-app list: the problem, then the fix."),
    ("offer", "Lead with the offer itself: what they get and the real price if one is given.",
     "Lay the image out as a bold product-hero poster with the offer as the main text."),
    ("outcome", "Lead with the result or feeling the customer gets after buying.",
     "Show the result in a real-life scene, with very little text."),
    ("versus", "Compare the old way of doing this with this way. Name no competitor.",
     "Lay the image out as a simple two-column before-and-after comparison."),
    ("announcement", "Say it as news from the business itself: what's available now.",
     "Lay the image out like a breaking-news banner announcing it."),
    ("post", "Say it in the business's own voice, as a short punchy opinion.",
     "Lay the image out like a screenshot of a short social post from the business's "
     "own account. It must not look like a customer wrote it."),
]

MAX_ADS = len(ANGLES)  # most ads one campaign can test
MAX_JANE_MADE = MAX_ADS

SINGLE_AD_SET = "single_ad_set"
ABO = "abo"
# Several ad sets, each holding some of the ads (PRD WS5's "2 ad sets, 3–4 creatives").
GROUPED = "grouped"

# PRD WS5 "Initial Budget Configuration Hypothesis": the daily budget decides how many ads a
# Meta campaign tests and how many ad sets they run in. (lowest daily ₦, ad sets, ads, label)
# Where the PRD gives a range ("1–2 ad sets"), the upper number starts halfway up the band.
# Starting hypotheses, not fixed rules: the owner can still change both before launch.
BUDGET_TIERS = [
    (150_000.0, 3, 6, "₦150,000+ a day"),
    (100_000.0, 3, 6, "₦75,000–₦150,000 a day"),
    (75_000.0, 2, 4, "₦75,000–₦150,000 a day"),
    (30_000.0, 2, 4, "₦30,000–₦75,000 a day"),
    (20_000.0, 2, 3, "₦15,000–₦30,000 a day"),
    (15_000.0, 1, 3, "₦15,000–₦30,000 a day"),
    (5_000.0, 1, 2, "₦5,000–₦15,000 a day"),
]

_DURATION = re.compile(r"\b(?:for|over|next|run(?:s|ning)?(?: it)?(?: for)?)\s+(?:the\s+next\s+)?"
                       r"(\d{1,3}|a|one)\s*(day|week|month)s?\b", re.I)
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}


def stated_days(message: str) -> Optional[int]:
    """How long the client asked this to run, in days. The last mention wins, because
    the message carries the whole conversation and a later line corrects an earlier one."""
    found = _DURATION.findall(message or "")
    if not found:
        return None
    count, unit = found[-1]
    n = 1 if count.lower() in ("a", "one") else int(count)
    return n * _UNIT_DAYS[unit.lower()] or None


def days_for_budget(stated: int, budget_ngn: float, floor_ngn: float) -> int:
    """The stated duration, shortened only when it would leave less than the daily floor."""
    days = min(stated, C.MAX_CAMPAIGN_DAYS)
    if floor_ngn > 0:
        days = min(days, int(budget_ngn // floor_ngn))
    return max(1, days)


def max_concepts(budget_ngn: float, days: int, target_ngn: Optional[float] = None) -> int:
    """How many ads could each have their own ad set at this budget, by Meta's daily
    floor and (with a target) the spend each needs before it can be judged."""
    days = max(1, int(days or 1))
    rec = recommend_structure(budget_ngn / days, days, target_ngn)
    return max(1, min(MAX_ADS, int(rec.get("concepts") or 1)))


def budget_structure(daily_ngn: float) -> dict:
    """How many ads, in how many ad sets, a Meta campaign at this daily budget should test.

    Never more ad sets than the budget can give Meta's daily floor each."""
    daily = max(0.0, float(daily_ngn or 0))
    ad_sets, ads, band = 1, 1, "under ₦5,000 a day"
    for lowest, n_sets, n_ads, label in BUDGET_TIERS:
        if daily >= lowest:
            ad_sets, ads, band = n_sets, n_ads, label
            break
    ad_sets = max(1, min(ad_sets, int(daily // C.MIN_DAILY_SPEND_NGN) or 1))
    ads = max(ads, ad_sets)
    if ads == 1:
        why = "At this budget one ad in one ad set learns fastest."
    elif ad_sets == 1:
        why = f"At {band}, {ads} different ads in one ad set let Meta find the one that works."
    else:
        why = (f"At {band}, {ads} different ads across {ad_sets} ad sets give each idea its own "
               "budget while Meta still learns quickly.")
    return {"ads": ads, "ad_sets": ad_sets, "band": band, "why": why}


def layout_for_budget(budget_ngn: float, days: int, ads: int) -> tuple[str, int]:
    """The ad layout (and ad-set count) the budget table recommends for this many ads."""
    daily = float(budget_ngn or 0) / max(1, int(days or 1))
    want = budget_structure(daily)["ad_sets"]
    k = max(1, min(want, ads))
    # budget_structure already keeps every ad set above Meta's daily floor.
    if ads <= 1 or k <= 1:
        return SINGLE_AD_SET, 1
    return (ABO, ads) if k == ads else (GROUPED, k)


def ad_set_for(index: int, ads: int, layout: str, ad_set_count: int) -> int:
    """Which ad set (0-based) the ad at `index` runs in."""
    if layout == ABO:
        return index
    if layout == GROUPED and ad_set_count > 1:
        return index % min(ad_set_count, ads)
    return 0


def ad_set_total(ads: int, layout: str, ad_set_count: int) -> int:
    if layout == ABO:
        return max(1, ads)
    if layout == GROUPED and ad_set_count > 1:
        return min(ad_set_count, ads)
    return 1


def can_split(budget_ngn: float, days: int, ads: int) -> bool:
    return ads > 1 and budget_ngn / max(1, int(days or 1)) / ads >= C.MIN_DAILY_SPEND_NGN


def next_angle(used: list[str]) -> str:
    """The first angle this campaign isn't already testing."""
    for key, _, _ in ANGLES:
        if key not in used:
            return key
    return ANGLES[0][0]


def layout_for(angle: str) -> str:
    """How this angle's picture should be laid out, for the image model."""
    layout = next((layout for key, _, layout in ANGLES if key == angle), "")
    # A layout asks for more text, and text near the edge gets cut off in the final crop.
    return f"{layout} Keep every word well inside the edges, with wide margins on all sides." if layout else ""


def brief_for(description: str, angle: str) -> str:
    """The campaign description with this ad's angle and visual structure added."""
    for key, line, layout in ANGLES:
        if key == angle:
            return (f"{description.strip()}\n\nCreative angle for this version: {line} {layout} "
                    "Its headline and opening line must come from this angle and not read like "
                    "other versions of the ad.")
    return description
