"""
Jane + Ads — refusing to launch an audience too small to deliver.

Jane's pins are deliberately tight, and tight is usually right: a pocket concentrates
a small budget on the people who matter. Stack three pockets against a long interest
list and an age band, though, and the three filters multiply — a real campaign came
out at 38,600–45,400 people, which Ads Manager itself flags as "very narrow, which may
affect ad delivery". Meta still accepts it, spends unevenly, and the client pays for
the lesson.

So the audience is measured before launch, against Meta's own delivery_estimate, and
widened one rung at a time until it can deliver:

  1. As planned.
  2. Drop the interest filter, keep the areas. Interests are the cheapest thing to
     give up — the areas are what the client chose and can see, and an interest list
     this long is mostly Jane's inference anyway.
  3. Widen the areas to the whole city/state, keep whatever the step above left.

Each rung is measured, not assumed: widening is only kept when Meta says it actually
helped, so a floor that cannot be reached (a genuinely small market) leaves the plan
alone rather than stripping it down to nothing for no gain.
"""
from __future__ import annotations

from typing import Any, Optional

# Below this, Meta's own UI calls the audience "very narrow". Not a hard Meta limit —
# it accepts far smaller — but the point where delivery gets uneven enough that a
# small daily budget stops finding people at a stable price. Chosen to sit just above
# the 38k–45k case this exists for, and deliberately a floor rather than a target:
# nothing here widens an audience that is already deliverable.
MIN_DELIVERABLE_AUDIENCE = 100_000

# What Jane says about it. Plain language, and specific about what was given up.
_DROPPED_INTERESTS = (
    "{where} together only reach about {size} people with the interest filter on — "
    "too tight for Meta to spend {budget} evenly — so I've dropped the interests and "
    "kept the areas."
)
_WIDENED_GEO = (
    "Even without the interest filter {where} only reach about {size} people, so I've "
    "widened this to all of {city}."
)


def reach_bounds(estimate: Optional[dict]) -> tuple[Optional[int], Optional[int]]:
    """The audience-size bounds out of a delivery_estimate, both shapes Meta returns."""
    if not estimate:
        return None, None
    data = estimate.get("data", estimate)
    if isinstance(data, list):
        data = data[0] if data else {}
    low = data.get("estimate_mau_lower_bound") or data.get("estimate_dau_lower_bound")
    high = data.get("estimate_mau_upper_bound") or data.get("estimate_dau_upper_bound")
    return (int(low) if low is not None else None,
            int(high) if high is not None else None)


def audience_size(estimate: Optional[dict]) -> Optional[int]:
    """One number for the audience, or None when Meta did not say.

    The LOWER bound, not the upper: planning against the optimistic end of Meta's own
    range is how an audience that reports "up to 90,000" and delivers to 20,000 passes
    a floor it should have failed.
    """
    low, high = reach_bounds(estimate)
    return low if low is not None else high


def _without_interests(audience_targeting: dict) -> dict:
    """The demographic targeting minus anything interest-shaped.

    Age and gender stay: they are usually the client's own words ("women 25-40"),
    where the interest list is Jane's inference from a category.
    """
    return {k: v for k, v in (audience_targeting or {}).items()
            if k not in ("flexible_spec", "interests", "behaviors")}


def _names(geo_targeting: dict) -> str:
    """The places in this targeting, as the client would name them."""
    geo = (geo_targeting or {}).get("geo_locations") or {}
    names = [entry.get("name", "") for field in ("cities", "regions", "places",
                                                 "neighborhoods", "subcities", "zips")
             for entry in (geo.get(field) or []) if isinstance(entry, dict) and entry.get("name")]
    if not names:
        return "these areas"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


async def widen_for_delivery(
    estimator,
    geo_targeting: dict,
    audience_targeting: dict,
    *,
    city: str = "",
    budget_label: str = "this budget",
    wider_geo_targeting: Optional[dict] = None,
    floor: int = MIN_DELIVERABLE_AUDIENCE,
) -> dict:
    """Measure the planned audience and widen it until Meta can deliver to it.

    `estimator` is anything with an async get_delivery_estimate(targeting) — the Meta
    adapter in production, a stub in tests. Best-effort throughout: an unreachable
    estimate leaves the plan exactly as planned, because refusing to launch over a
    number we could not read would be worse than launching the audience the client
    already approved.

    Returns {"audience_targeting", "geo_targeting", "estimate", "note", "widened"}.
    """
    async def measure(geo: dict, audience: dict) -> tuple[Optional[dict], Optional[int]]:
        est = await estimator.get_delivery_estimate({**geo, **audience})
        return est, audience_size(est)

    estimate, size = await measure(geo_targeting, audience_targeting)
    result = {
        "audience_targeting": audience_targeting,
        "geo_targeting": geo_targeting,
        "estimate": estimate,
        "note": "",
        "widened": False,
    }
    # Meta did not answer, or the audience is already big enough to deliver.
    if size is None or size >= floor:
        return result

    # Rung 2 — drop the interests, keep the areas.
    stripped = _without_interests(audience_targeting)
    if stripped != audience_targeting:
        wider_estimate, wider_size = await measure(geo_targeting, stripped)
        # Kept only when it actually helped: Meta sometimes returns a lower number for
        # a broader spec, and shipping a worse audience plus an explanation of how it
        # was improved would be the wrong kind of confident.
        if wider_size is not None and wider_size > size:
            result.update(audience_targeting=stripped, estimate=wider_estimate,
                          widened=True,
                          note=_DROPPED_INTERESTS.format(
                              where=_names(geo_targeting),
                              size=f"{size:,}", budget=budget_label))
            estimate, size = wider_estimate, wider_size
            if size >= floor:
                return result
            audience_targeting = stripped

    # Rung 3 — widen the areas themselves, when the caller gave us somewhere wider.
    if wider_geo_targeting and wider_geo_targeting != geo_targeting:
        widest_estimate, widest_size = await measure(wider_geo_targeting, audience_targeting)
        if widest_size is not None and widest_size > (size or 0):
            result.update(geo_targeting=wider_geo_targeting, audience_targeting=audience_targeting,
                          estimate=widest_estimate, widened=True,
                          note=_WIDENED_GEO.format(
                              where=_names(geo_targeting), size=f"{size:,}",
                              city=city or "the city"))
    return result
