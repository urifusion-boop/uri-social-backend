"""
Unit tests for the Jane Campaign Summary builder (summary.py) — the pure reasoning
assembly. The live Meta reach call happens in the adapter; here we pass a mocked
delivery_estimate (or none) and assert the structured {value, reason} output + the
labeled estimate derivation.
"""
from app.agents.jane_ads.models import (
    ABTestScope, CampaignObjective, CampaignPlan, CampaignRequest, CreativeContext,
    GeoMode, GeoPin, GeoPlan, Goal, OfferType, Platform, PlatformPlan, PurchaseBehaviour,
)
from app.agents.jane_ads.summary import ASSUMED_LEAD_RATE, build_campaign_summary


def _plan(**kw) -> CampaignPlan:
    base = dict(
        business_id="b1", goal=Goal.MESSAGES, behaviour=PurchaseBehaviour.DISCOVER,
        platforms=[PlatformPlan(platform=Platform.META, budget_ngn=20_000, days=7,
                                variants=1, test_scope=ABTestScope.NONE,
                                objective=CampaignObjective.CONVERSATIONS)],
        per_business_cap_ngn=20_000, account_cap_ngn=20_000, estimated_conversations=40,
        geo=GeoPlan(mode=GeoMode.OWN_RADIUS, city="Lagos",
                    pins=[GeoPin(name="Surulere", reason="dense commercial area")],
                    explanation="Surulere has the foot traffic your shop needs."),
    )
    base.update(kw)
    return CampaignPlan(**base)


def _req(**kw) -> CampaignRequest:
    base = dict(business_id="b1", business_name="Mama Kitchen", category="restaurant",
                goal=Goal.MESSAGES, offer_type=OfferType.PRODUCT, budget_ngn=20_000,
                creative=CreativeContext())
    base.update(kw)
    return CampaignRequest(**base)


def test_summary_has_all_reasoned_sections():
    s = build_campaign_summary(_plan(), _req())
    for section in (s.objective, s.audience, s.platforms, s.budget_allocation, s.duration, s.optimization):
        assert section.value and section.reason  # every choice carries a why


def test_objective_reflects_offer_type():
    s = build_campaign_summary(_plan(), _req(offer_type=OfferType.PROMOTION))
    assert "promotion" in s.objective.value.lower()


def test_audience_uses_geo_pins_and_explanation():
    s = build_campaign_summary(_plan(), _req())
    assert "Surulere" in s.audience.value
    assert "foot traffic" in s.audience.reason


def test_audience_defaults_to_nigeria_without_pins():
    s = build_campaign_summary(_plan(geo=None), _req())
    assert s.audience.value == "Nigeria"


def test_audience_names_who_it_targets_not_only_where():
    # Live-reported: a client picked an audience plan and the summary still read
    # "Nigeria — No specific area given", as if the app had ignored the choice.
    s = build_campaign_summary(_plan(), _req(),
                               audience_text="small business owners going digital")
    assert "small business owners going digital" in s.audience.value
    assert "Surulere" in s.audience.value


def test_audience_without_pins_still_names_who_rather_than_claiming_none_given():
    s = build_campaign_summary(_plan(geo=None), _req(), audience_text="gym owners in Lekki")
    assert "gym owners in Lekki" in s.audience.value
    assert "No specific area given" not in s.audience.reason


def test_audience_reason_names_the_interests_actually_sent_to_meta():
    # The card must describe the ad that really ships — these interests are exactly
    # what lands in the ad set's flexible_spec.
    plan = _plan(audience_targeting={"flexible_spec": [{"interests": [
        {"id": "1", "name": "Small business"}, {"id": "2", "name": "Digital marketing"},
    ]}]})
    s = build_campaign_summary(plan, _req(), audience_text="small business owners")
    assert "Small business" in s.audience.reason
    assert "Digital marketing" in s.audience.reason


def test_duration_matches_plan_days():
    s = build_campaign_summary(_plan(), _req())
    assert s.duration.value == "7 days"


def test_audience_size_from_meta_pool_but_leads_from_budget():
    # Meta's pool is huge (8M) — leads must NOT scale off it; they come from the budget
    # (plan.estimated_conversations = 40), so a ₦20k campaign never claims millions of leads.
    est = {"data": [{"estimate_mau_lower_bound": 7_800_000, "estimate_mau_upper_bound": 9_100_000}]}
    s = build_campaign_summary(_plan(), _req(), price_per_result_ngn=500, delivery_estimate=est)
    assert s.estimates.audience_size_low == 7_800_000 and s.estimates.audience_size_high == 9_100_000
    assert s.estimates.estimated_leads == 40                       # from budget, not the 8M pool
    assert s.estimates.estimated_clicks == max(40, round(40 / ASSUMED_LEAD_RATE))
    assert s.estimates.cost_per_result_ngn == 500


def test_estimates_fall_back_to_price_without_conversion_count():
    s = build_campaign_summary(_plan(estimated_conversations=None), _req(),
                               price_per_result_ngn=400, delivery_estimate=None)
    assert s.estimates.audience_size_low is None
    assert s.estimates.estimated_leads == round(20_000 / 400)
    assert s.estimates.cost_per_result_ngn == 400


def test_estimates_note_always_present():
    s = build_campaign_summary(_plan(), _req())
    assert "estimate" in s.estimates.note.lower()
