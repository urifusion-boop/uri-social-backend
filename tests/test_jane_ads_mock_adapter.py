"""
Unit tests for the mock ad-platform adapter + the plan→launch→events seam.

Proves Shore's half runs end-to-end with no live platform: plan a campaign, launch it
on the mock, and receive deterministic conversation + spend events back.
"""
import asyncio

from app.agents.jane_ads.adapters.mock import MockAdPlatformAdapter
from app.agents.jane_ads.decision_engine import plan_campaign
from app.agents.jane_ads.models import CampaignRequest, SpendAuthorization


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _plan(budget=10_000.0, category="fashion"):
    res = plan_campaign(
        CampaignRequest(business_id="b1", category=category, budget_ngn=budget),
        funded_amount_ngn=budget,
        total_funded_wallets_ngn=budget,
    )
    assert res.plan is not None
    return res.plan


def test_launch_returns_ids_mapping_business_to_ad():
    adapter = MockAdPlatformAdapter()
    plan = _plan()
    auth = SpendAuthorization(business_id="b1", funded_amount_ngn=10_000, account_cap_ngn=10_000)
    res = _run(adapter.launch_campaign(plan, auth))
    assert res.launched
    assert "b1" in res.ad_ids
    assert res.campaign_id


def test_conversations_are_deterministic():
    # ₦10,000 budget, ₦500/conversation → exactly 20 conversations.
    adapter = MockAdPlatformAdapter(conversation_cost_ngn=500.0)
    plan = _plan(budget=10_000.0)
    auth = SpendAuthorization(business_id="b1", funded_amount_ngn=10_000, account_cap_ngn=10_000)
    launch = _run(adapter.launch_campaign(plan, auth))
    convos = _run(adapter.poll_conversations(launch.campaign_id))
    assert len(convos) == 20
    assert all(c.business_id == "b1" for c in convos)
    assert all(c.charge_ngn == 500.0 for c in convos)


def test_spend_never_exceeds_funded_cap():
    # Fund only ₦3,000 though the plan budget is higher → mock caps spend at the auth.
    adapter = MockAdPlatformAdapter(conversation_cost_ngn=500.0)
    plan = _plan(budget=10_000.0)
    auth = SpendAuthorization(business_id="b1", funded_amount_ngn=3_000, account_cap_ngn=3_000)
    launch = _run(adapter.launch_campaign(plan, auth))
    spend = _run(adapter.fetch_per_ad_spend(launch.campaign_id))
    assert spend[0].spend_ngn <= 3_000


def test_pause_stops_conversations_and_spend():
    adapter = MockAdPlatformAdapter(conversation_cost_ngn=500.0)
    plan = _plan(budget=10_000.0)
    auth = SpendAuthorization(business_id="b1", funded_amount_ngn=10_000, account_cap_ngn=10_000)
    launch = _run(adapter.launch_campaign(plan, auth))
    ad_id = launch.ad_ids["b1"]
    assert _run(adapter.pause_ad(launch.campaign_id, ad_id)) is True
    assert _run(adapter.poll_conversations(launch.campaign_id)) == []
    assert _run(adapter.fetch_per_ad_spend(launch.campaign_id))[0].spend_ngn == 0
