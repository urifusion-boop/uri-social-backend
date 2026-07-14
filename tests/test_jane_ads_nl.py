"""
Unit tests for the natural-language layer's deterministic mapping (nl.py).

The LLM parse itself is live/non-deterministic; here we test to_campaign_request —
the pure map from parsed fields → CampaignRequest, including defaults and the
"missing budget → can't proceed" rule.
"""
from app.agents.jane_ads.models import CreativeKind, Goal, PurchaseBehaviour
from app.agents.jane_ads.nl import ParsedCampaign, to_campaign_request


def test_full_message_maps_to_request():
    parsed = ParsedCampaign(
        business_name="Mama Kitchen", category="restaurant", goal="messages",
        budget_ngn=10_000, city="Surulere", stated_behaviour=None,
    )
    req = to_campaign_request(parsed)
    assert req is not None
    assert req.category == "restaurant"
    assert req.goal == Goal.MESSAGES
    assert req.budget_ngn == 10_000
    assert req.geo == "Surulere"


def test_missing_budget_cannot_proceed():
    parsed = ParsedCampaign(category="fashion", city="Lekki")   # no budget
    assert to_campaign_request(parsed) is None


def test_zero_budget_rejected():
    assert to_campaign_request(ParsedCampaign(budget_ngn=0)) is None


def test_goal_defaults_to_messages_when_absent():
    req = to_campaign_request(ParsedCampaign(budget_ngn=5_000))
    assert req.goal == Goal.MESSAGES


def test_stated_behaviour_maps_when_present():
    req = to_campaign_request(ParsedCampaign(budget_ngn=15_000, stated_behaviour="search"))
    assert req.stated_behaviour == PurchaseBehaviour.SEARCH


def test_video_flag_sets_creative_kind():
    req = to_campaign_request(ParsedCampaign(budget_ngn=60_000, has_video=True))
    assert req.creative.kind == CreativeKind.VIDEO and req.creative.has_video is True


def test_flags_thread_through():
    req = to_campaign_request(ParsedCampaign(
        budget_ngn=10_000, is_new_thing=True, has_existing_demand=True))
    assert req.is_new_thing is True and req.has_existing_demand is True
