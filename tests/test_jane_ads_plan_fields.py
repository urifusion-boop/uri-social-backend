"""The review step: the plan as fields a client edits before their money moves."""
import asyncio

import pytest

from app.agents.jane_ads.models import (
    ABTestScope, AdCreative, CampaignPlan, CampaignRequest, CreativeContext,
    Goal, GeoMode, GeoPin, GeoPlan, Platform, PlatformPlan, PurchaseBehaviour,
)
from app.agents.jane_ads.plan_fields import apply_edits, describe


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _plan(**over):
    base = dict(
        business_id="b1", goal=Goal.MESSAGES, behaviour=PurchaseBehaviour.DISCOVER,
        platforms=[PlatformPlan(platform=Platform.META, budget_ngn=20000, days=5,
                                variants=1, test_scope=ABTestScope.NONE)],
        per_business_cap_ngn=50000, account_cap_ngn=100000,
        geo=GeoPlan(mode=GeoMode.OWN_RADIUS, city="Lagos",
                    pins=[GeoPin(name="Ikeja", lat=6.6, lng=3.35)]),
        audience_targeting={"age_min": 25, "age_max": 45, "genders": [2],
                            "flexible_spec": [{"interests": [{"id": "1", "name": "Fashion"}]}]},
        creative=AdCreative(headline="Fresh bread", primary_text="Order today."),
    )
    base.update(over)
    return CampaignPlan(**base)


def _req(**over):
    base = dict(business_id="b1", business_name="B", budget_ngn=20000,
                creative=CreativeContext(), geo="Lagos")
    base.update(over)
    return CampaignRequest(**base)


def test_describe_shows_what_jane_decided_in_readable_lines():
    fields = {f["key"]: f for f in describe(_plan(), _req())}
    assert fields["caption"]["value"] == "Order today."
    assert fields["locations"]["value"] == ["Ikeja"]
    assert fields["interests"]["value"] == ["Fashion"]
    assert fields["gender"]["value"] == "women"
    assert fields["age_min"]["value"] == 25
    assert fields["budget_ngn"]["value"] == 20000


def test_derived_lines_are_shown_but_not_editable():
    """Daily spend follows from budget and duration. Offering a pencil beside it would
    promise an edit we would have to silently ignore."""
    fields = {f["key"]: f for f in describe(_plan(), _req())}
    assert fields["daily_spend"]["editable"] is False
    assert fields["daily_spend"]["value"] == 4000
    assert fields["destination"]["editable"] is False


def test_editing_the_caption_is_what_launches():
    plan, req, applied, rejected = _run(apply_edits(
        _plan(), _req(), {"caption": "Fresh bread, delivered before 8am."}))
    assert applied == ["caption"]
    assert rejected == []
    assert plan.creative.primary_text == "Fresh bread, delivered before 8am."


def test_ad_copy_that_would_be_rejected_at_launch_is_rejected_here(monkeypatch):
    """The whole point of validating at edit time: a client must not be allowed to
    save copy that the policy scan will throw out after the wallet is debited."""
    from app.agents.jane_ads import policy

    class _Block:
        severity = policy.Severity.BLOCK
        guidance = "no miracle cures"

    monkeypatch.setattr(policy, "review_ad_creative",
                        lambda *a, **k: type("R", (), {"violations": [_Block()]})())
    plan, _, applied, rejected = _run(apply_edits(
        _plan(), _req(), {"caption": "cures everything"}))
    assert applied == []
    assert "no miracle cures" in rejected[0]
    assert plan.creative.primary_text == "Order today."


def test_a_location_meta_cannot_name_is_refused_not_quietly_pinned(monkeypatch):
    """geo now targets named locations only. Accepting an unnameable one here would
    show a plan line the launch then silently drops."""
    async def _unresolved(name, region, token="", timeout=8.0):
        return None

    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _unresolved)
    plan, _, applied, rejected = _run(apply_edits(
        _plan(), _req(), {"locations": ["Computer Village"]}))
    assert applied == []
    assert any("Computer Village" in r for r in rejected)
    assert [p.name for p in plan.geo.pins] == ["Ikeja"]


def test_a_nameable_location_replaces_the_pins(monkeypatch):
    async def _resolved(name, region, token="", timeout=8.0):
        return {"type": "city", "key": "1", "name": "Surulere", "region": "Lagos State"}

    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _resolved)
    plan, _, applied, rejected = _run(apply_edits(
        _plan(), _req(), {"locations": ["Surulere"]}))
    assert applied == ["locations"]
    assert [p.name for p in plan.geo.pins] == ["Surulere"]


def test_a_rejected_edit_does_not_throw_away_a_good_one(monkeypatch):
    """Losing work the client typed is its own bug — one bad field must not discard
    the rest of the form."""
    async def _unresolved(name, region, token="", timeout=8.0):
        return None

    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _unresolved)
    plan, _, applied, rejected = _run(apply_edits(
        _plan(), _req(), {"locations": ["Nowhere"], "caption": "Kept this."}))
    assert applied == ["caption"]
    assert rejected
    assert plan.creative.primary_text == "Kept this."


def test_interests_are_checked_against_metas_own_catalogue(monkeypatch):
    async def _resolve(client, base, token, keyword):
        return {"id": "99", "name": "Bread"} if keyword == "Bread" else None

    monkeypatch.setattr("app.agents.jane_ads.audience_targeting._resolve_interest", _resolve)
    plan, _, applied, rejected = _run(apply_edits(
        _plan(), _req(), {"interests": ["Bread", "Invented Thing"]}))
    assert applied == ["interests"]
    assert any("Invented Thing" in r for r in rejected)
    assert plan.audience_targeting["flexible_spec"] == [{"interests": [{"id": "99", "name": "Bread"}]}]


def test_interests_stay_in_one_flexible_spec_entry(monkeypatch):
    """Meta ORs within an entry and ANDs across entries. Two entries would mean
    'people interested in BOTH', a near-empty audience."""
    async def _resolve(client, base, token, keyword):
        return {"id": keyword, "name": keyword}

    monkeypatch.setattr("app.agents.jane_ads.audience_targeting._resolve_interest", _resolve)
    plan, _, _, _ = _run(apply_edits(_plan(), _req(), {"interests": ["A", "B", "C"]}))
    assert len(plan.audience_targeting["flexible_spec"]) == 1
    assert len(plan.audience_targeting["flexible_spec"][0]["interests"]) == 3


@pytest.mark.parametrize("choice,expected", [("all", None), ("men", [1]), ("women", [2])])
def test_gender_maps_to_metas_codes(choice, expected):
    plan, _, applied, _ = _run(apply_edits(_plan(), _req(), {"gender": choice}))
    assert applied == ["gender"]
    assert plan.audience_targeting.get("genders") == expected


def test_an_age_range_outside_metas_bounds_is_refused():
    """Meta rejects these outright at ad-set create, so catching it here is the
    difference between a sentence under the field and a failed launch."""
    _, _, applied, rejected = _run(apply_edits(_plan(), _req(), {"age_min": 12}))
    assert applied == []
    assert "between 18 and 65" in rejected[0]


def test_an_inverted_age_range_is_refused():
    _, _, applied, rejected = _run(apply_edits(
        _plan(), _req(), {"age_min": 50, "age_max": 30}))
    assert applied == []
    assert rejected


def test_a_budget_over_the_brands_cap_is_refused():
    _, _, applied, rejected = _run(apply_edits(_plan(), _req(), {"budget_ngn": 90000}))
    assert applied == []
    assert "cap" in rejected[0]


def test_budget_moves_on_both_the_request_and_the_platform_plan():
    """They are read by different code paths at launch. Updating one and not the
    other is how a card ends up promising a number the ad set does not carry."""
    plan, req, applied, _ = _run(apply_edits(_plan(), _req(), {"budget_ngn": 30000}))
    assert applied == ["budget_ngn"]
    assert req.budget_ngn == 30000
    assert plan.platforms[0].budget_ngn == 30000


def test_changing_duration_reflows_the_daily_spend():
    plan, req, _, _ = _run(apply_edits(_plan(), _req(), {"days": 10}))
    fields = {f["key"]: f for f in describe(plan, req)}
    assert fields["days"]["value"] == 10
    assert fields["daily_spend"]["value"] == 2000


def test_clearing_every_interest_goes_broad_rather_than_erroring():
    plan, _, applied, rejected = _run(apply_edits(_plan(), _req(), {"interests": []}))
    assert applied == ["interests"]
    assert rejected == []
    assert "flexible_spec" not in plan.audience_targeting
