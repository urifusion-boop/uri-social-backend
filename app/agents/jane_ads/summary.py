"""
Jane + Ads — the "Jane Campaign Summary" (Tier C).

Turns the plan the decision engine already computed into a structured, explainable
summary: every choice (audience, platforms, budget split, duration, optimization) paired
with a one-line WHY, plus reach/click/lead estimates. Jane is supposed to feel like a
media buyer sitting beside you — this is where she shows her reasoning instead of just
handing over a black-box plan.

Nothing here re-derives a decision: the values come straight from `plan`/`req`, and the
reach numbers come from Meta's real `delivery_estimate` when available. Every projected
number is explicitly an ESTIMATE — assumptions are labeled, never presented as a promise.
`build_campaign_summary` is pure (the live Meta call happens in the adapter and is passed
in), so the reasoning assembly is fully unit-tested.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .models import CampaignPlan, CampaignRequest, Platform

# Clearly-labeled planning assumption for turning budget-driven clicks → leads. Rough
# industry figure for Click-to-WhatsApp traffic, NOT a measured rate for this account —
# surfaced only ever as an "estimate", never a guarantee.
ASSUMED_LEAD_RATE = 0.35     # ~35% of taps actually send the WhatsApp message

_PLATFORM_LABELS = {
    Platform.META: "Meta (Instagram & Facebook)",
    Platform.GOOGLE: "Google",
    Platform.TIKTOK: "TikTok",
}


class ReasonedValue(BaseModel):
    """A decision plus the one-line reason Jane made it."""
    value: str
    reason: str


class CampaignEstimates(BaseModel):
    """Projected results — ALWAYS estimates. `audience_size_*` is Meta's real count of how
    many people your targeting COULD reach (the addressable pool, not how many the budget
    will actually reach). Clicks/leads are derived from the BUDGET (what you spend bounds
    the result), never from the pool size."""
    audience_size_low: Optional[int] = None
    audience_size_high: Optional[int] = None
    estimated_clicks: Optional[int] = None
    estimated_leads: Optional[int] = None
    cost_per_result_ngn: Optional[float] = None
    # What to call estimated_clicks on the card. The client used to hard-code
    # "Est. WhatsApp clicks", which was wrong for every other destination.
    clicks_label: str = "Est. link clicks"
    note: str = "These are estimates based on your budget and audience — not guarantees."


class CampaignSummary(BaseModel):
    objective: ReasonedValue
    audience: ReasonedValue
    platforms: ReasonedValue
    budget_allocation: ReasonedValue
    duration: ReasonedValue
    optimization: ReasonedValue
    estimates: CampaignEstimates = Field(default_factory=CampaignEstimates)


def _naira(n: float) -> str:
    return f"₦{n:,.0f}"


def _reach_bounds(delivery_estimate: Optional[dict]) -> tuple[Optional[int], Optional[int]]:
    """Pull the audience-size bounds out of Meta's delivery_estimate response, tolerant
    of both the estimate_mau_* and estimate_dau_* shapes Meta returns."""
    if not delivery_estimate:
        return None, None
    data = delivery_estimate.get("data", delivery_estimate)
    if isinstance(data, list):
        data = data[0] if data else {}
    low = data.get("estimate_mau_lower_bound") or data.get("estimate_dau_lower_bound")
    high = data.get("estimate_mau_upper_bound") or data.get("estimate_dau_upper_bound")
    return (int(low) if low is not None else None,
            int(high) if high is not None else None)


def build_campaign_summary(
    plan: CampaignPlan,
    req: CampaignRequest,
    price_per_result_ngn: Optional[float] = None,
    delivery_estimate: Optional[dict] = None,
    audience_text: str = "",
) -> CampaignSummary:
    """Assemble the structured, explained summary from the already-decided plan. Pure —
    the live Meta reach numbers are fetched by the caller and passed in as
    `delivery_estimate` (None → reach is simply omitted, the rest still renders)."""
    total_budget = sum(p.budget_ngn for p in plan.platforms)
    days = max((p.days for p in plan.platforms), default=1)
    offer = (req.offer_type.value if req.offer_type else "") or plan.goal.value

    # Objective — what's being promoted + the campaign goal driving everything.
    objective = ReasonedValue(
        value=f"Promote your {offer}",
        reason=f"Your goal is {plan.goal.value} — everything below is chosen to serve that.",
    )

    # Audience — WHO this runs at, and WHERE. This row used to be built from the geo
    # pins alone, so a client who had just picked an audience plan (or typed their own)
    # still read "Nigeria — no specific area given", as if the app had ignored them.
    # Live-reported. The interests are named too, because they're what Meta actually
    # receives: the card must describe the ad that really ships.
    pins = plan.geo.pins if plan.geo else []
    where = ", ".join(p.name for p in pins)
    who = (audience_text or "").strip()
    interests = [
        i.get("name", "") for spec in (plan.audience_targeting.get("flexible_spec") or [])
        for i in (spec.get("interests") or []) if i.get("name")
    ]
    if who or where:
        geo_reason = (plan.geo.explanation if plan.geo and plan.geo.explanation
                      else "These areas match where your customers actually are.")
        audience = ReasonedValue(
            value=" — ".join(bit for bit in (who, where or "Nigeria") if bit),
            reason=(f"Targeting interests: {', '.join(interests)}. {geo_reason}" if interests
                    else geo_reason if where
                    else "Targeting these people nationwide — tell me a city to focus the spend."),
        )
    else:
        audience = ReasonedValue(
            value="Nigeria",
            reason="No specific area given, so we target broadly — tell me a city to focus the spend.",
        )

    # Platforms — the resolved behaviour is why we landed here.
    plat_label = ", ".join(_PLATFORM_LABELS.get(p.platform, p.platform.value) for p in plan.platforms)
    platforms = ReasonedValue(
        value=plat_label,
        reason=(f"Your customers discover businesses like yours by scrolling, so Meta's feed "
                f"placements fit best." if plan.behaviour.value == "discover"
                else f"Chosen for a '{plan.behaviour.value}' buying pattern."),
    )

    # Budget allocation — the per-platform split, already computed.
    if len(plan.platforms) == 1:
        p = plan.platforms[0]
        budget_allocation = ReasonedValue(
            value=f"{_naira(p.budget_ngn)} over {p.days} days on {_PLATFORM_LABELS.get(p.platform, p.platform.value)}",
            reason=f"Your full {_naira(total_budget)} goes to the one platform that fits, for the strongest signal.",
        )
    else:
        parts = [f"{_naira(p.budget_ngn)} on {_PLATFORM_LABELS.get(p.platform, p.platform.value)}" for p in plan.platforms]
        budget_allocation = ReasonedValue(
            value="; ".join(parts),
            reason=f"Your {_naira(total_budget)} is split across platforms by where it should work hardest.",
        )

    # Duration — days, with the Meta daily-floor reasoning that set it.
    daily = total_budget / days if days else total_budget
    duration = ReasonedValue(
        value=f"{days} days",
        reason=(f"Spreads {_naira(total_budget)} to about {_naira(daily)}/day — enough to clear Meta's "
                f"minimum daily budget so the ad actually delivers, without stretching too thin."),
    )

    # Optimization — what Meta is told to get you, named after where the tap actually
    # lands. Was hard-coded to WhatsApp, which read "WhatsApp link clicks / most likely
    # to message you" on a campaign pointing at a website.
    from .destination import clicks_label, coerce_type, optimization_for
    _dest = coerce_type(getattr(plan, "destination_type", "") or "")
    _opt_value, _opt_reason = optimization_for(_dest)
    optimization = ReasonedValue(value=_opt_value, reason=_opt_reason)

    # Estimates. Audience size = Meta's addressable pool for the targeting (real data,
    # honestly labeled as "could reach", not "will reach"). Clicks/leads are BUDGET-driven:
    # leads = budget ÷ this business's real per-result price (same figure the plan already
    # uses for estimated_conversations), clicks back-derived from the lead rate.
    audience_low, audience_high = _reach_bounds(delivery_estimate)
    estimates = CampaignEstimates(audience_size_low=audience_low, audience_size_high=audience_high)
    leads = plan.estimated_conversations
    if not leads and price_per_result_ngn:
        leads = max(1, round(total_budget / price_per_result_ngn))
    if leads:
        estimates.estimated_leads = leads
        estimates.estimated_clicks = max(leads, round(leads / ASSUMED_LEAD_RATE))
        estimates.cost_per_result_ngn = round(
            price_per_result_ngn if price_per_result_ngn else total_budget / leads, 2)

    estimates.clicks_label = clicks_label(_dest)

    return CampaignSummary(
        objective=objective, audience=audience, platforms=platforms,
        budget_allocation=budget_allocation, duration=duration,
        optimization=optimization, estimates=estimates,
    )
