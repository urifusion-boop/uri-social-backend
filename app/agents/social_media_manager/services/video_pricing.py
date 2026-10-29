"""
What a generated video costs the customer, in credits.

The prices live here, on the server, and nowhere else: the frontend only ever displays a quote the
backend returned, and the amount charged is recomputed from the scenes that will actually be
rendered, never from a number the client sent.

The matrix is the launch table from "Video Generation Credit Pricing" v1.0 (28 Oct 2026), which is
still a proposal pending Product/Finance sign-off. Change the numbers here (and bump
PRICING_VERSION) to reprice; nothing else needs to move.

Durations between the table's columns (10s, 20s, 40s, 50s...) are priced by straight-line
interpolation between the neighbouring columns and rounded UP, so a longer video never costs less
than a shorter one and no price falls below the cell it grew from.
"""
from typing import Dict, Iterable, Optional

PRICING_VERSION = "2026-10-28.1"

# What one credit is worth, for DISPLAY only ("about N naira"). Not used to charge anything: charges are
# in credits. The Pro and Agency plans sell a credit at this price; Starter and Growth sell it for less
# (about 750 and 714), so the real revenue per credit depends on the customer's plan.
CREDIT_NAIRA_REFERENCE = 800

# credits by video type and total length in seconds
VIDEO_PRICES: Dict[str, Dict[int, int]] = {
    "quick_video":         {5: 2, 15: 3,  30: 5,  60: 9},
    "animate_product":     {5: 2, 15: 3,  30: 5,  60: 9},
    "talking_nigerian":    {5: 2, 15: 3,  30: 6,  60: 11},
    "social_video":        {5: 2, 15: 3,  30: 6,  60: 12},
    "high_quality":        {5: 2, 15: 4,  30: 8,  60: 15},
    "complex_cinematic":   {5: 2, 15: 5,  30: 10, 60: 19},
    "talking_standard":    {5: 3, 15: 9,  30: 18, 60: 35},
    "advanced_references": {5: 5, 15: 14, 30: 26, 60: 52},
}

MAX_VIDEO_SECONDS = 60
MAX_SCENES = 12
_MIN_SCENE_SECONDS, _MAX_SCENE_SECONDS = 1, 15   # the longest any one clip model will run
_DEFAULT_SCENE_SECONDS = 5
_NIGERIAN_SUFFIX = " (Nigerian)"


class PricingError(ValueError):
    """The request cannot be priced (too long, nothing to render)."""


def video_type_for(outcome: str, avatar_voice: str = "") -> str:
    """The price row for an outcome. Talking videos split by voice: a Nigerian voice is cheaper to make.

    An unknown outcome is priced as Quick Video because that is the route the generator falls back to."""
    if outcome == "talking_dialogue":
        return "talking_nigerian" if (avatar_voice or "").endswith(_NIGERIAN_SUFFIX) else "talking_standard"
    mapping = {
        "quick_video": "quick_video", "animate_product": "animate_product", "social_video": "social_video",
        "high_quality": "high_quality", "complex_cinematic": "complex_cinematic",
        "advanced_references": "advanced_references",
    }
    return mapping.get(outcome, "quick_video")


def billable_seconds(scenes: Iterable[dict]) -> int:
    """Seconds we will actually render: each scene's length, bounded to what a clip model can run.

    Computed from the scenes themselves, so a tampered storyboard cannot buy a long video at a short
    price: a scene claiming 600 seconds is billed as the 15 it can actually run."""
    total = 0
    for scene in scenes:
        try:
            seconds = int(round(float((scene or {}).get("duration_seconds") or _DEFAULT_SCENE_SECONDS)))
        except (TypeError, ValueError):
            seconds = _DEFAULT_SCENE_SECONDS
        total += max(_MIN_SCENE_SECONDS, min(_MAX_SCENE_SECONDS, seconds))
    return total


def credits_for(video_type: str, seconds: int) -> int:
    """Whole credits for `seconds` of `video_type`, rounded up."""
    prices = VIDEO_PRICES[video_type]
    if seconds <= 0:
        raise PricingError("There is nothing to render.")
    if seconds > MAX_VIDEO_SECONDS:
        raise PricingError(f"Videos can be at most {MAX_VIDEO_SECONDS} seconds.")
    columns = sorted(prices)
    if seconds <= columns[0]:
        return prices[columns[0]]
    for low, high in zip(columns, columns[1:]):
        if low <= seconds <= high:
            numerator = prices[low] * (high - low) + (prices[high] - prices[low]) * (seconds - low)
            return -(-numerator // (high - low))        # integer ceil: never rounds down
    return prices[columns[-1]]


def quote_for(storyboard: dict, outcome: str, avatar_voice: str = "") -> dict:
    """The price of rendering this storyboard, from the server's own table."""
    scenes = (storyboard or {}).get("scenes") or []
    if not scenes:
        raise PricingError("There is nothing to render.")
    if len(scenes) > MAX_SCENES:
        raise PricingError(f"A video can have at most {MAX_SCENES} scenes ({MAX_VIDEO_SECONDS} seconds).")
    seconds = billable_seconds(scenes)
    video_type = video_type_for(outcome, avatar_voice)
    return {
        "pricing_version": PRICING_VERSION,
        "video_type": video_type,
        "scenes": len(scenes),
        "seconds": seconds,
        "credits_required": credits_for(video_type, seconds),
    }


def refund_for_failed_scenes(credits_charged: int, total_scenes: int, delivered_scenes: int) -> int:
    """Credits to give back when only some scenes came out.

    The share of the price that belongs to the scenes we failed to deliver, rounded in the customer's
    favour and never more than was charged. Nothing delivered = everything back."""
    if credits_charged <= 0 or total_scenes <= 0:
        return 0
    delivered = max(0, min(delivered_scenes, total_scenes))
    if delivered == 0:
        return credits_charged
    failed = total_scenes - delivered
    return min(credits_charged, -(-(credits_charged * failed) // total_scenes))
