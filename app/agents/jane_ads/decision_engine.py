"""
Jane + Ads — the decision engine (Corrected Platform Decision Logic, May 2026).

GOAL first, behaviour next, business type is only a hint, decided PER CAMPAIGN,
always explained. A transparent decision tree (not a scoring model): pure,
deterministic, fully unit-testable — no platform calls, no I/O.

Layer order (PRD §3):
  1. GOAL           — the goal of this campaign, leads everything
  2. BEHAVIOUR      — search vs discover vs mixed; business type sets a default that
                      the user's stated behaviour or the goal can override
  3. BUDGET         — small → one platform; large → the affordable few
  4. CREATIVE       — hard gate: no video → no TikTok
  5. GEOGRAPHY      — a targeting setting within a platform, not a platform reason
  6. RECOMMEND+EXPLAIN — name the platform(s) AND explain why (required)
"""
from __future__ import annotations

from . import constants as C
from .models import (
    ABTestScope,
    CampaignObjective,
    CampaignRequest,
    CampaignPlan,
    CreativeContext,
    Goal,
    PlanAdvice,
    PlanDecision,
    PlanResult,
    Platform,
    PlatformPlan,
    PurchaseBehaviour,
)

# ── Business type → DEFAULT behaviour (a hint only, PRD §3) ────────────────────
_SEARCH_KEYWORDS = {
    "plumber", "plumbing", "dentist", "locksmith", "electrician", "lawyer", "legal",
    "hospital", "hotel", "clinic", "doctor", "mechanic", "repair", "repairs",
    "accountant", "pest", "towing", "hvac", "carpenter", "contractor", "surveyor",
    "cleaning", "cleaner", "movers",
}
_DISCOVER_KEYWORDS = {
    "fashion", "clothing", "boutique", "beauty", "makeup", "cosmetics", "skincare",
    "hair", "salon", "food", "restaurant", "cafe", "bakery", "events", "event",
    "decor", "photography", "photographer", "jewelry", "jewellery", "art", "craft",
    "cake", "perfume", "shoes",
}
_MIXED_KEYWORDS = {
    "real", "estate", "realtor", "property", "school", "schools", "education",
    "travel", "tour", "cars", "car", "auto", "automobile", "furniture",
}


def default_behaviour(request: CampaignRequest) -> PurchaseBehaviour:
    """The business-type default behaviour — a HINT, later overridable."""
    text = f"{request.category} {request.description}".lower()
    words = set(text.replace(",", " ").replace("/", " ").split())
    if words & _MIXED_KEYWORDS:
        return PurchaseBehaviour.MIXED
    if words & _SEARCH_KEYWORDS:
        return PurchaseBehaviour.SEARCH
    if words & _DISCOVER_KEYWORDS:
        return PurchaseBehaviour.DISCOVER
    return PurchaseBehaviour.DISCOVER   # local-commerce default


def resolve_behaviour(request: CampaignRequest) -> tuple[PurchaseBehaviour, list[str]]:
    """Resolve behaviour in the PRD's order: default → user override → goal-implication.
    Returns the behaviour and the trace lines explaining how it was reached."""
    trace: list[str] = []
    behaviour = default_behaviour(request)
    trace.append(f"Business-type default (a hint): {behaviour.value.upper()}.")

    if request.stated_behaviour is not None:
        behaviour = request.stated_behaviour
        trace.append(f"You told me customers actually {behaviour.value.upper()} — overriding the default.")

    # Goal-implications (applied last, per the pseudocode).
    if request.goal in (Goal.AWARENESS, Goal.FOLLOWERS) and request.is_new_thing:
        if behaviour != PurchaseBehaviour.DISCOVER:
            trace.append("Goal is awareness for a brand-new thing nobody searches yet → DISCOVER.")
        behaviour = PurchaseBehaviour.DISCOVER
    if request.goal == Goal.WALK_INS and request.has_existing_demand:
        if behaviour != PurchaseBehaviour.SEARCH:
            trace.append("Goal is walk-ins with people already looking → SEARCH.")
        behaviour = PurchaseBehaviour.SEARCH

    return behaviour, trace


# ── Behaviour → lean platforms ────────────────────────────────────────────────
_LEAN: dict[PurchaseBehaviour, list[Platform]] = {
    PurchaseBehaviour.SEARCH:   [Platform.GOOGLE],
    PurchaseBehaviour.DISCOVER: [Platform.META, Platform.TIKTOK],
    PurchaseBehaviour.MIXED:    [Platform.META, Platform.GOOGLE],
}


def budget_tier_for(total_budget_ngn: float) -> str:
    """A plain-language size label for the total campaign budget (PRD §3.3's
    `budgetTier`) — reuses the SAME boundaries that already decide A/B test scope
    (constants.AB_LIGHT_TEST_NGN/AB_FULL_TEST_NGN) so the label a user sees always
    matches the variant/test-scope decision underneath it, instead of drifting from
    a second, independently-tuned set of thresholds."""
    if total_budget_ngn >= C.AB_FULL_TEST_NGN:
        return "growth"
    if total_budget_ngn >= C.AB_LIGHT_TEST_NGN:
        return "standard"
    return "starter"


def _days_for(total_budget: float) -> int:
    if total_budget >= C.AB_FULL_TEST_NGN:
        return C.MAX_CAMPAIGN_DAYS
    if total_budget <= C.USEFUL_MIN_NGN["meta"]:
        return C.MIN_CAMPAIGN_DAYS
    return C.DEFAULT_CAMPAIGN_DAYS


def _variant_plan(platform_budget: float, useful_min: float) -> tuple[int, ABTestScope]:
    """Never split below the useful minimum — a starved variant can't learn (PRD C2)."""
    if platform_budget / 2 < useful_min:
        return 1, ABTestScope.NONE
    if platform_budget < C.AB_FULL_TEST_NGN:
        return 2, ABTestScope.AUDIENCE
    return 2, ABTestScope.AUDIENCE_AND_CREATIVE


def choose_platform(
    request: CampaignRequest,
    funded_amount_ngn: float,
    total_funded_wallets_ngn: float,
) -> PlanResult:
    """Run the corrected decision tree for ONE campaign."""
    trace: list[str] = []
    budget = request.budget_ngn

    # 1. GOAL
    trace.append(f"Goal of this campaign: {request.goal.value.upper()} — this leads the decision.")

    # 2. BEHAVIOUR (default → override → goal-implication)
    behaviour, btrace = resolve_behaviour(request)
    trace.extend(btrace)
    trace.append(f"Resolved behaviour: customers {behaviour.value.upper()} this → lean "
                 f"{[p.value for p in _LEAN[behaviour]]}.")

    lean = list(_LEAN[behaviour])

    # 4. CREATIVE hard gate (applied before affordability so it can't be chosen)
    if Platform.TIKTOK in lean and not request.creative.has_video:
        lean.remove(Platform.TIKTOK)
        trace.append("Creative gate: no native video → TikTok removed.")

    # 3. BUDGET gate
    affordable = [p for p in lean if budget >= C.USEFUL_MIN_NGN[p.value]]
    aff_txt = ", ".join(p.value.upper() for p in affordable) or "none"
    trace.append(f"Budget check — ₦{budget:,.0f} clears useful minimum for: {aff_txt}.")

    if not affordable:
        cheapest = min((C.USEFUL_MIN_NGN[p.value] for p in lean),
                       default=C.USEFUL_MIN_NGN["meta"])
        trace.append(f"No platform floor cleared → advise pooling or top up to ₦{cheapest:,.0f}.")
        return PlanResult(
            decision=PlanDecision.ADVISE,
            advice=PlanAdvice(
                reason=(f"₦{budget:,.0f} is below the useful minimum for every fitting "
                        f"platform. Pool with similar businesses, or top up to at least "
                        f"₦{cheapest:,.0f}."),
                suggested_min_ngn=cheapest,
                can_pool=(behaviour in (PurchaseBehaviour.DISCOVER, PurchaseBehaviour.MIXED)),
                trace=trace,
            ),
        )

    combined_min = sum(C.USEFUL_MIN_NGN[p.value] for p in affordable)
    if len(affordable) > 1 and budget < combined_min:
        platforms = [affordable[0]]   # lean is priority-ordered → first affordable is best fit
        trace.append(f"Small budget — ₦{budget:,.0f} < ₦{combined_min:,.0f} to fund all, "
                     f"so concentrate on the best fit: {platforms[0].value.upper()}.")
    else:
        platforms = affordable
        if len(platforms) > 1:
            trace.append(f"Budget funds several — running {', '.join(p.value.upper() for p in platforms)}.")
        else:
            trace.append(f"Running {platforms[0].value.upper()}.")

    # 5. GEOGRAPHY — a targeting setting WITHIN the platform, never a platform reason
    if request.geo:
        trace.append(f"Geography: {request.geo} — set as targeting within the platform, "
                     f"not a reason to switch platforms.")

    per_platform_budget = budget / len(platforms)
    days = _days_for(budget)
    platform_plans: list[PlatformPlan] = []
    for p in platforms:
        variants, scope = _variant_plan(per_platform_budget, C.USEFUL_MIN_NGN[p.value])
        platform_plans.append(PlatformPlan(
            platform=p, budget_ngn=round(per_platform_budget, 2), days=days,
            variants=variants, test_scope=scope,
            objective=CampaignObjective.CONVERSATIONS,
        ))

    trace.append(f"Caps — per-business ₦{funded_amount_ngn:,.0f}, "
                 f"per-account ₦{total_funded_wallets_ngn:,.0f}. URI never fronts more.")

    plan = CampaignPlan(
        business_id=request.business_id,
        goal=request.goal,
        behaviour=behaviour,
        platforms=platform_plans,
        per_business_cap_ngn=funded_amount_ngn,
        account_cap_ngn=total_funded_wallets_ngn,
        budget_tier=budget_tier_for(budget),
        explanation=_explain(request, behaviour, platform_plans),
        trace=trace,
    )
    return PlanResult(decision=PlanDecision.PLAN, plan=plan)


# Backwards-friendly alias — the campaign IS the unit of decision.
plan_campaign = choose_platform


def apply_platform_override(plan: CampaignPlan, chosen: list[Platform]) -> CampaignPlan:
    """Rebuild a plan's platform split around the user's explicit choice, overriding
    Jane's recommendation (PRD §1.8 — every override is logged separately by the
    instrumentation layer; this just makes the simulation actually reflect it).
    Re-splits the same total budget (per_business_cap_ngn) evenly across `chosen`."""
    chosen = list(dict.fromkeys(chosen))  # dedupe, preserve order
    if not chosen:
        raise ValueError("override must choose at least one platform")

    total_budget = plan.per_business_cap_ngn
    per_platform_budget = total_budget / len(chosen)
    days = _days_for(total_budget)

    platform_plans: list[PlatformPlan] = []
    for p in chosen:
        variants, scope = _variant_plan(per_platform_budget, C.USEFUL_MIN_NGN[p.value])
        platform_plans.append(PlatformPlan(
            platform=p, budget_ngn=round(per_platform_budget, 2), days=days,
            variants=variants, test_scope=scope,
            objective=CampaignObjective.CONVERSATIONS,
        ))
    return plan.model_copy(update={"platforms": platform_plans})


def _explain(
    request: CampaignRequest,
    behaviour: PurchaseBehaviour,
    plans: list[PlatformPlan],
) -> str:
    """The required explanation (PRD §6 template): platform + how they buy + budget +
    creative + geography, in plain language like a marketer."""
    names = {Platform.META: "Instagram + Facebook", Platform.GOOGLE: "Google Search",
             Platform.TIKTOK: "TikTok"}
    where = " and ".join(names[p.platform] for p in plans)
    buy = {
        PurchaseBehaviour.SEARCH: "your customers search for this rather than stumble on it",
        PurchaseBehaviour.DISCOVER: "your customers discover this by scrolling, not by searching",
        PurchaseBehaviour.MIXED: "your customers both search and discover",
    }[behaviour]
    budget_bit = ("your budget is small so I'm focusing on one platform"
                  if len(plans) == 1 else "your budget funds more than one platform")
    creative_bit = ("you have video" if request.creative.has_video
                    else "you have photos" if request.creative.kind.value == "image"
                    else "no creative is needed for search")
    geo_bit = f", and you're targeting {request.geo}" if request.geo else ""
    return (f"I chose {where} because {buy}, {budget_bit}, {creative_bit}{geo_bit}.")
