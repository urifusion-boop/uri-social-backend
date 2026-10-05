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
  2. Widen the areas to the whole city/state, keep the interests.
  3. Only then drop the interest filter.

Interests go LAST, and that order is the whole point. Reach is cheapest to buy by
dropping them, but lead quality is what they buy: comparing Jane's launched ad sets
against manually-run ones on the same account, the campaigns that brought chancers
and people with no interest in the offer were the ones with no interest filter. A
wider area full of the right people beats a tight area full of anybody.

Each rung is measured, not assumed: widening is only kept when Meta says it actually
helped, so a floor that cannot be reached (a genuinely small market) leaves the plan
alone rather than stripping it down to nothing for no gain.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Below this, Meta's own UI calls the audience "very narrow". Not a hard Meta limit —
# it accepts far smaller — but the point where delivery gets uneven enough that a
# small daily budget stops finding people at a stable price. Chosen to sit just above
# the 38k–45k case this exists for, and deliberately a floor rather than a target:
# nothing here widens an audience that is already deliverable.
MIN_DELIVERABLE_AUDIENCE = 100_000

# What Jane says about it. Plain language, and specific about what was given up.
_WIDENED_GEO = (
    "{where} only reach about {size} people — too tight for Meta to spend {budget} "
    "evenly — so I've widened this to all of {city} and kept who we're looking for "
    "the same."
)
_DROPPED_INTERESTS = (
    "Even across all of {city} this is still only about {size} people, so I've also "
    "had to drop the interest filter. Watch who actually messages: if they're the "
    "wrong people, narrowing the area again works better than narrowing the budget."
)


def restate_geography(text: str, dropped: list[str], city: str) -> str:
    """The same sentence, talking about where the ad will ACTUALLY run.

    Jane writes her plan before the audience is measured, so after a widening her
    own words still promised the pockets: "I'll focus on busy professionals in
    Victoria Island, Ikoyi and Lekki Phase 1..." immediately above a note saying the
    campaign had been widened to all of Lagos. The client reads the first sentence
    and believes it.

    The names are REPLACED rather than the sentence deleted: that sentence usually
    carries the audience reasoning too ("...who would benefit from a convenient
    pickup service"), and throwing it away to fix the geography would cost more than
    it saved.
    """
    if not text or not dropped or not city:
        return text
    # Longest first, so "Lekki Phase 1" is consumed before the "Lekki" inside it.
    names = sorted({n.strip() for n in dropped if n and n.strip()}, key=len, reverse=True)
    one = "|".join(re.escape(n) for n in names)
    # A run of them: "A, B and C", "A and B", "A, B, C".
    run = re.compile(rf"(?:{one})(?:\s*(?:,|,?\s*and)\s*(?:{one}))*", re.IGNORECASE)
    replacement = f"all of {city}"
    out, replaced = run.subn(replacement, text)
    if not replaced:
        return text
    # "all of Lagos, all of Lagos and all of Lagos" if the names were scattered.
    collapse = re.compile(rf"(?:{re.escape(replacement)})(?:\s*(?:,|,?\s*and)\s*{re.escape(replacement)})+",
                          re.IGNORECASE)
    return collapse.sub(replacement, out)


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

    # Rung 2 — widen the areas, keep who we are looking for.
    if wider_geo_targeting and wider_geo_targeting != geo_targeting:
        wider_estimate, wider_size = await measure(wider_geo_targeting, audience_targeting)
        # Kept only when it actually helped: Meta sometimes returns a lower number for
        # a broader spec, and shipping a worse audience plus an explanation of how it
        # was improved would be the wrong kind of confident.
        if wider_size is not None and wider_size > size:
            result.update(geo_targeting=wider_geo_targeting, estimate=wider_estimate,
                          widened=True,
                          note=_WIDENED_GEO.format(
                              where=_names(geo_targeting), size=f"{size:,}",
                              budget=budget_label, city=city or "the city"))
            estimate, size = wider_estimate, wider_size
            if size >= floor:
                return result
            geo_targeting = wider_geo_targeting

    # Rung 3 — the interest filter, last, because it is what keeps the WRONG people out.
    stripped = _without_interests(audience_targeting)
    if stripped != audience_targeting:
        widest_estimate, widest_size = await measure(geo_targeting, stripped)
        if widest_size is not None and widest_size > (size or 0):
            result.update(audience_targeting=stripped, geo_targeting=geo_targeting,
                          estimate=widest_estimate, widened=True,
                          note=_DROPPED_INTERESTS.format(
                              city=city or "that area", size=f"{size:,}"))
    return result
