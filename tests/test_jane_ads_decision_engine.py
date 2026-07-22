"""
Unit tests for the Jane + Ads decision engine (Corrected Platform Decision Logic).

GOAL first, behaviour drives, business type is a hint, decided per campaign, always
explained. Pure logic — no network, no server, no DB.
"""
from app.agents.jane_ads import constants as C
from app.agents.jane_ads.decision_engine import default_behaviour, choose_platform, budget_tier_for, _days_for
from app.agents.jane_ads.models import (
    CampaignRequest,
    CreativeContext,
    CreativeKind,
    Goal,
    PlanDecision,
    Platform,
    PurchaseBehaviour,
    ABTestScope,
)


def _req(**kw) -> CampaignRequest:
    base = dict(business_id="b1", budget_ngn=10_000.0, goal=Goal.MESSAGES)
    base.update(kw)
    return CampaignRequest(**base)


def _plan(**kw):
    b = kw.pop("budget_ngn", 10_000.0)
    return choose_platform(_req(budget_ngn=b, **kw), b, b)


# ── Default behaviour (business type is only a hint) ──────────────────────────

def test_default_behaviour_fashion_is_discover():
    assert default_behaviour(_req(category="fashion")) == PurchaseBehaviour.DISCOVER


def test_default_behaviour_plumber_is_search():
    assert default_behaviour(_req(category="plumber")) == PurchaseBehaviour.SEARCH


def test_default_behaviour_real_estate_is_mixed():
    assert default_behaviour(_req(category="real estate")) == PurchaseBehaviour.MIXED


def test_default_behaviour_unknown_defaults_discover():
    assert default_behaviour(_req(category="")) == PurchaseBehaviour.DISCOVER


# ── USE CASE 1 — fashion boutique whose GOAL overrides the default ────────────

def test_fashion_boutique_stated_search_goes_to_google():
    # "people already know me, they just can't find me" → stated SEARCH overrides
    # the fashion→discover default → Google, not Meta.
    res = choose_platform(
        _req(category="fashion", budget_ngn=15_000,
             stated_behaviour=PurchaseBehaviour.SEARCH),
        15_000, 15_000,
    )
    assert [p.platform for p in res.plan.platforms] == [Platform.GOOGLE]
    assert res.plan.behaviour == PurchaseBehaviour.SEARCH


def test_fashion_boutique_default_still_meta_without_override():
    res = _plan(category="fashion")
    assert [p.platform for p in res.plan.platforms] == [Platform.META]


# ── USE CASE 2 — one clinic, two campaigns, two platforms ─────────────────────

def test_clinic_walkins_goes_to_google():
    res = choose_platform(
        _req(category="clinic", goal=Goal.WALK_INS, has_existing_demand=True, budget_ngn=10_000),
        10_000, 10_000,
    )
    assert [p.platform for p in res.plan.platforms] == [Platform.GOOGLE]


def test_clinic_new_service_awareness_goes_to_meta():
    # Same clinic, brand-new service nobody searches for → DISCOVER → Meta, even
    # though clinic defaults to search. This is the whole point of the correction.
    res = choose_platform(
        _req(category="clinic", goal=Goal.AWARENESS, is_new_thing=True, budget_ngn=10_000),
        10_000, 10_000,
    )
    assert Platform.META in {p.platform for p in res.plan.platforms}
    assert Platform.GOOGLE not in {p.platform for p in res.plan.platforms}
    assert res.plan.behaviour == PurchaseBehaviour.DISCOVER


def test_same_business_different_campaigns_different_platforms():
    walkins = choose_platform(
        _req(category="clinic", goal=Goal.WALK_INS, has_existing_demand=True, budget_ngn=10_000),
        10_000, 10_000).plan
    launch = choose_platform(
        _req(category="clinic", goal=Goal.AWARENESS, is_new_thing=True, budget_ngn=10_000),
        10_000, 10_000).plan
    assert walkins.platforms[0].platform != launch.platforms[0].platform


# ── Budget / creative gates ───────────────────────────────────────────────────

def test_discover_small_budget_meta_only():
    res = _plan(category="food", budget_ngn=5_000)
    assert [p.platform for p in res.plan.platforms] == [Platform.META]


def test_discover_large_budget_with_video_meta_and_tiktok():
    res = choose_platform(
        _req(category="skincare", budget_ngn=60_000,
             creative=CreativeContext(kind=CreativeKind.VIDEO, has_video=True)),
        60_000, 60_000,
    )
    assert {p.platform for p in res.plan.platforms} == {Platform.META, Platform.TIKTOK}


def test_tiktok_hard_gated_without_video():
    res = _plan(category="skincare", budget_ngn=60_000)   # no video
    assert Platform.TIKTOK not in {p.platform for p in res.plan.platforms}


def test_below_floor_advises_pooling():
    res = _plan(category="fashion", budget_ngn=2_000)
    assert res.decision == PlanDecision.ADVISE
    assert res.advice.suggested_min_ngn == C.USEFUL_MIN_NGN["meta"]
    assert res.advice.can_pool is True


# ── A/B variant rule (PRD C2) ─────────────────────────────────────────────────

def test_no_split_below_floor():
    res = _plan(category="fashion", budget_ngn=5_000)
    assert res.plan.platforms[0].variants == 1
    assert res.plan.platforms[0].test_scope == ABTestScope.NONE


def test_light_test_at_mid_budget():
    res = _plan(category="fashion", budget_ngn=10_000)
    assert res.plan.platforms[0].variants == 2
    assert res.plan.platforms[0].test_scope == ABTestScope.AUDIENCE


def test_full_test_at_large_budget():
    res = _plan(category="fashion", budget_ngn=20_000)
    assert res.plan.platforms[0].test_scope == ABTestScope.AUDIENCE_AND_CREATIVE


# ── Meta daily-budget floor: duration is capped so total/days clears it ────────

def test_days_capped_so_small_budget_clears_meta_daily_floor():
    # ₦5,000 over the default 4 days = ₦1,250/day, under Meta's ₦1,610 floor →
    # Meta rejects the ad set (subcode 1885272). Duration must shorten to 3 days.
    assert _days_for(5_000) == 3
    assert 5_000 / _days_for(5_000) >= C.META_MIN_DAILY_NGN


def test_days_unchanged_when_budget_already_clears_floor():
    # ₦10,000 over 5 days = ₦2,000/day (clears the floor) — unchanged.
    assert _days_for(10_000) == C.DEFAULT_CAMPAIGN_DAYS
    # ₦20,000+ still gets the full 7-day run.
    assert _days_for(20_000) == C.MAX_CAMPAIGN_DAYS


def test_every_runnable_budget_produces_a_deliverable_daily_budget():
    # Any budget the engine will actually run (≥ the useful minimum) must yield a
    # daily budget at/above Meta's floor after the day-cap.
    for budget in (5_000, 6_000, 8_000, 10_000, 15_000, 20_000, 50_000):
        daily = budget / _days_for(budget)
        assert daily >= C.META_MIN_DAILY_NGN, f"₦{budget} → ₦{daily:.0f}/day under floor"


# ── Caps, explanation, trace ──────────────────────────────────────────────────

def test_caps_attached():
    res = choose_platform(_req(budget_ngn=10_000), 10_000, 250_000)
    assert res.plan.per_business_cap_ngn == 10_000
    assert res.plan.account_cap_ngn == 250_000


def test_explanation_required_and_names_platform():
    res = _plan(category="fashion", budget_ngn=10_000)
    assert res.plan.explanation
    assert "Instagram" in res.plan.explanation or "Facebook" in res.plan.explanation


def test_trace_is_populated():
    res = _plan(category="fashion", budget_ngn=10_000)
    assert len(res.plan.trace) >= 5


def test_budget_conserved_across_split():
    res = choose_platform(
        _req(category="skincare", budget_ngn=60_000,
             creative=CreativeContext(has_video=True)),
        60_000, 60_000,
    )
    assert abs(sum(p.budget_ngn for p in res.plan.platforms) - 60_000) < 1.0


# ── Budget tier (PRD §3.3) — same boundaries as A/B test scope ────────────────

def test_budget_tier_below_light_test_is_starter():
    assert budget_tier_for(C.AB_LIGHT_TEST_NGN - 1) == "starter"


def test_budget_tier_at_light_test_boundary_is_standard():
    assert budget_tier_for(C.AB_LIGHT_TEST_NGN) == "standard"


def test_budget_tier_below_full_test_is_standard():
    assert budget_tier_for(C.AB_FULL_TEST_NGN - 1) == "standard"


def test_budget_tier_at_full_test_boundary_is_growth():
    assert budget_tier_for(C.AB_FULL_TEST_NGN) == "growth"


def test_budget_tier_well_above_full_test_is_growth():
    assert budget_tier_for(C.AB_FULL_TEST_NGN * 5) == "growth"


def test_plan_carries_its_own_budget_tier():
    res = _plan(category="fashion", budget_ngn=C.AB_FULL_TEST_NGN)
    assert res.plan.budget_tier == "growth"
