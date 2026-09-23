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


# ── Placement and the daily floor ─────────────────────────────────────────────

def test_placement_defaults_to_automatic_when_meta_was_left_to_choose():
    fields = {f["key"]: f for f in describe(_plan(), _req())}
    assert fields["placement"]["value"] == "automatic"
    assert "instagram_only" in fields["placement"]["options"]


def test_instagram_only_pins_the_ad_to_instagram():
    """Automatic delivery also spends on Audience Network. A client who wants only
    Instagram must be able to say so and have it actually hold."""
    plan, _, applied, rejected = _run(apply_edits(_plan(), _req(), {"placement": "instagram_only"}))
    assert applied == ["placement"]
    assert rejected == []
    assert plan.audience_targeting["publisher_platforms"] == ["instagram"]


def test_going_back_to_automatic_clears_the_restriction():
    """Unset, not an empty list — Meta reads an empty publisher_platforms as invalid
    rather than as 'anywhere'."""
    plan = _plan(audience_targeting={"publisher_platforms": ["instagram"]})
    plan, _, applied, _ = _run(apply_edits(plan, _req(), {"placement": "automatic"}))
    assert applied == ["placement"]
    assert "publisher_platforms" not in plan.audience_targeting


def test_facebook_and_instagram_keeps_both_and_drops_audience_network():
    plan, _, _, _ = _run(apply_edits(_plan(), _req(), {"placement": "facebook_and_instagram"}))
    assert sorted(plan.audience_targeting["publisher_platforms"]) == ["facebook", "instagram"]


def test_an_unknown_placement_is_refused():
    _, _, applied, rejected = _run(apply_edits(_plan(), _req(), {"placement": "tiktok"}))
    assert applied == []
    assert rejected


def test_a_duration_that_drops_daily_spend_under_metas_floor_is_refused():
    """₦20,000 over 40 days is ₦500/day. Meta refuses the ad set outright (subcode
    1885272), so this has to be caught while the client can still change it."""
    _, _, applied, rejected = _run(apply_edits(_plan(), _req(), {"days": 40}))
    assert applied == []
    assert "minimum" in rejected[0]
    assert "12 days or fewer" in rejected[0]


def test_lowering_the_budget_alone_can_break_the_floor_too():
    """The floor is about the pair, not either number — a budget that is fine over
    2 days is not fine over the 5 already stored."""
    _, _, applied, rejected = _run(apply_edits(_plan(), _req(), {"budget_ngn": 3000}))
    assert applied == []
    assert "a day" in rejected[0]


def test_shortening_the_run_is_a_valid_way_to_clear_the_floor():
    """Validating budget and duration separately would refuse this, even though the
    pair is exactly what Meta wants."""
    plan, req, applied, rejected = _run(apply_edits(
        _plan(), _req(), {"budget_ngn": 4000, "days": 2}))
    assert rejected == []
    assert sorted(applied) == ["budget_ngn", "days"]
    assert req.budget_ngn == 4000
    assert plan.platforms[0].days == 2


def _tiktok_plan(**over):
    over.setdefault("platforms", [PlatformPlan(
        platform=Platform.TIKTOK, budget_ngn=100000, days=5, variants=1,
        test_scope=ABTestScope.NONE)])
    return _plan(**over)


# ── TikTok: targeting fields don't apply, must not pretend to ───────────────
# Live-caught 2026-09-23: adapters/tiktok.py never reads plan.geo or
# plan.audience_targeting at all — every TikTok campaign targets all adults in
# Nigeria regardless of what got saved here. These fields used to render anyway,
# Meta-labelled, letting a client "successfully" edit something with zero effect
# on the actual launch.

def test_describe_hides_meta_only_targeting_fields_for_a_tiktok_plan():
    fields = {f["key"]: f for f in describe(_tiktok_plan(), _req())}
    for key in ("locations", "interests", "gender", "placement", "age_min", "age_max"):
        assert key not in fields
    assert fields["tiktok_audience_note"]["value"] == "All adults in Nigeria"
    assert fields["tiktok_audience_note"]["editable"] is False
    # Still real, still shown: TikTok's ad_text/budget/schedule are genuinely used.
    assert "caption" in fields
    assert "budget_ngn" in fields


def test_describe_still_shows_all_meta_targeting_fields_for_a_meta_plan():
    # Regression: the TikTok branch must not have narrowed Meta's own fields.
    fields = {f["key"]: f for f in describe(_plan(), _req())}
    for key in ("locations", "interests", "gender", "placement", "age_min", "age_max"):
        assert key in fields
    assert "tiktok_audience_note" not in fields


def test_daily_spend_help_names_tiktoks_own_floor_not_metas():
    fields = {f["key"]: f for f in describe(_tiktok_plan(), _req())}
    assert "TikTok" in fields["daily_spend"]["help"]
    assert "31,000" in fields["daily_spend"]["help"]
    assert "Meta" not in fields["daily_spend"]["help"]


def test_apply_edits_rejects_targeting_edits_on_a_tiktok_plan_without_calling_meta():
    # No monkeypatch on the Meta lookups here — if this reached _validated_interests
    # it would try a real network call and the test would hang/error, which is
    # itself proof the gate works.
    plan, req, applied, rejected = _run(apply_edits(
        _tiktok_plan(), _req(), {"interests": ["Fashion"], "gender": "women"}))
    assert applied == []
    assert len(rejected) == 1
    assert "TikTok" in rejected[0]
    assert plan.audience_targeting == _tiktok_plan().audience_targeting


def test_apply_edits_uses_tiktoks_daily_floor_not_metas():
    # ₦100,000 / 10 days = ₦10,000/day — clears Meta's ₦1,610 floor easily, but sits
    # well under TikTok's real ₦31,000 floor. Must be refused for a TikTok plan.
    plan, req, applied, rejected = _run(apply_edits(
        _tiktok_plan(), _req(), {"days": 10}))
    assert applied == []
    assert "TikTok" in rejected[0]
    assert "31,000" in rejected[0]


def test_apply_edits_accepts_a_budget_days_pair_that_clears_tiktoks_real_floor():
    # ₦45,000 / 1 day = ₦45,000/day — clears TikTok's ₦31,000 floor and stays under
    # the fixture's ₦50,000 brand cap. Uses a req whose budget matches (the default
    # _req() carries the unrelated ₦20,000 base fixture value).
    plan, req, applied, rejected = _run(apply_edits(
        _tiktok_plan(), _req(budget_ngn=45000), {"days": 1}))
    assert rejected == []
    assert applied == ["days"]
    assert plan.platforms[0].days == 1


def test_duration_is_not_pinned_to_the_default():
    """'It mustn't always be 7 days' — any duration that clears the daily floor and
    the 1-90 bound is accepted, not just Jane's default."""
    for days in (2, 3, 9, 12):
        plan, _, applied, rejected = _run(apply_edits(_plan(), _req(), {"days": days}))
        assert rejected == [], f"{days} days rejected: {rejected}"
        assert plan.platforms[0].days == days


# ── The endpoints themselves ──────────────────────────────────────────────────

class _FakeCollection:
    def __init__(self, doc):
        self.doc = doc
        self.updates = []

    async def find_one(self, *a, **k):
        return self.doc

    async def update_one(self, query, update, **k):
        self.updates.append(update)
        self.doc.update(update.get("$set", {}))
        return None


class _FakeDb:
    def __init__(self, doc):
        self.plans = _FakeCollection(doc)

    def __getitem__(self, name):
        return self.plans


def _pending_doc():
    return {
        "plan_id": "plan_x", "brand_id": "brand_1", "status": "pending",
        "thread_id": "t1",
        "plan": _plan().model_dump(mode="json"),
        "req": _req().model_dump(mode="json"),
    }


def test_get_fields_endpoint_returns_the_editable_lines():
    from app.agents.jane_ads.router import meta_plan_fields

    db = _FakeDb(_pending_doc())
    out = _run(meta_plan_fields("plan_x", db=db, brand_ctx={"brand_id": "brand_1"}))
    keys = [f["key"] for f in out["fields"]]
    assert out["plan_id"] == "plan_x"
    assert {"caption", "locations", "interests", "gender", "placement", "budget_ngn"} <= set(keys)


def test_get_fields_refuses_another_brands_plan():
    """These carry a business's targeting and copy — a wrong brand_id must 404, not
    leak someone else's campaign."""
    from fastapi import HTTPException

    from app.agents.jane_ads.router import meta_plan_fields

    db = _FakeDb(_pending_doc())
    with pytest.raises(HTTPException) as e:
        _run(meta_plan_fields("plan_x", db=db, brand_ctx={"brand_id": "someone_else"}))
    assert e.value.status_code == 404


def test_patch_endpoint_persists_the_edit_onto_the_plan_the_launch_reloads(monkeypatch):
    """The launch endpoint re-reads this document. If the edit does not land in
    doc['plan'], the client's change is cosmetic and the old ad launches."""
    from app.agents.jane_ads.router import PlanFieldsBody, meta_plan_edit_fields

    db = _FakeDb(_pending_doc())
    out = _run(meta_plan_edit_fields(
        "plan_x", PlanFieldsBody(edits={"caption": "Edited by the client."}),
        db=db, brand_ctx={"brand_id": "brand_1"},
    ))
    assert out["applied"] == ["caption"]
    assert db.plans.doc["plan"]["creative"]["primary_text"] == "Edited by the client."
    assert db.plans.doc["edited_by_client"] is True


def test_patch_endpoint_does_not_persist_when_everything_was_rejected(monkeypatch):
    """A save that changed nothing must leave the stored plan untouched, rather than
    rewriting it with the same values and claiming an edit happened."""
    from app.agents.jane_ads.router import PlanFieldsBody, meta_plan_edit_fields

    db = _FakeDb(_pending_doc())
    out = _run(meta_plan_edit_fields(
        "plan_x", PlanFieldsBody(edits={"age_min": 9}),
        db=db, brand_ctx={"brand_id": "brand_1"},
    ))
    assert out["applied"] == []
    assert out["rejected"]
    assert db.plans.updates == []


def test_patch_refuses_a_plan_that_already_launched():
    """Editing a launched plan would silently do nothing — the campaign is already on
    Meta. Say so instead."""
    from fastapi import HTTPException

    from app.agents.jane_ads.router import PlanFieldsBody, meta_plan_edit_fields

    doc = _pending_doc()
    doc["status"] = "launched"
    db = _FakeDb(doc)
    with pytest.raises(HTTPException) as e:
        _run(meta_plan_edit_fields("plan_x", PlanFieldsBody(edits={"caption": "x"}),
                                   db=db, brand_ctx={"brand_id": "brand_1"}))
    assert e.value.status_code == 409


def test_saving_an_untouched_interest_does_not_destroy_it(monkeypatch):
    """Live-caught: a client changed one field and the save reported five interests
    they had never touched as untargetable, wiping them.

    Meta's display names carry their category — "Marketing (business and finance)" —
    and Meta's OWN search cannot find that string again. So anything already on the
    plan must be kept by its stored id rather than re-resolved."""
    async def _never_resolves(client, base, token, keyword):
        return None  # exactly what Meta does with its own display names

    monkeypatch.setattr("app.agents.jane_ads.audience_targeting._resolve_interest", _never_resolves)
    plan = _plan(audience_targeting={"flexible_spec": [{"interests": [
        {"id": "6003", "name": "Marketing (business and finance)"},
        {"id": "6004", "name": "Retail (industry)"},
    ]}]})
    out, _, applied, rejected = _run(apply_edits(
        plan, _req(),
        {"interests": ["Marketing (business and finance)", "Retail (industry)"]}))
    assert rejected == []
    assert applied == ["interests"]
    assert out.audience_targeting["flexible_spec"][0]["interests"] == [
        {"id": "6003", "name": "Marketing (business and finance)"},
        {"id": "6004", "name": "Retail (industry)"},
    ]


def test_a_genuinely_new_interest_is_still_checked_against_meta(monkeypatch):
    """Keeping known ones must not turn into trusting anything the client types."""
    async def _resolve(client, base, token, keyword):
        return {"id": "77", "name": "Bread"} if keyword == "Bread" else None

    monkeypatch.setattr("app.agents.jane_ads.audience_targeting._resolve_interest", _resolve)
    plan = _plan(audience_targeting={"flexible_spec": [{"interests": [
        {"id": "6003", "name": "Marketing (business and finance)"},
    ]}]})
    out, _, applied, rejected = _run(apply_edits(
        plan, _req(),
        {"interests": ["Marketing (business and finance)", "Bread", "Not A Real Thing"]}))
    assert any("Not A Real Thing" in r for r in rejected)
    names = [i["name"] for i in out.audience_targeting["flexible_spec"][0]["interests"]]
    assert names == ["Marketing (business and finance)", "Bread"]


def test_dropping_an_interest_from_the_list_removes_it(monkeypatch):
    """Keeping known interests must not mean they can never be deleted."""
    async def _never(client, base, token, keyword):
        return None

    monkeypatch.setattr("app.agents.jane_ads.audience_targeting._resolve_interest", _never)
    plan = _plan(audience_targeting={"flexible_spec": [{"interests": [
        {"id": "1", "name": "Keep me"}, {"id": "2", "name": "Drop me"},
    ]}]})
    out, _, applied, _ = _run(apply_edits(plan, _req(), {"interests": ["Keep me"]}))
    assert applied == ["interests"]
    assert [i["name"] for i in out.audience_targeting["flexible_spec"][0]["interests"]] == ["Keep me"]


def test_the_save_returns_what_the_plan_card_above_needs_to_stop_lying():
    """Jane's plan card renders from the planning payload. Without refreshed numbers
    it keeps showing her original budget and duration after the client changed them —
    two contradictory answers to 'what is about to launch'. Live-reported."""
    from app.agents.jane_ads.router import PlanFieldsBody, meta_plan_edit_fields

    db = _FakeDb(_pending_doc())
    out = _run(meta_plan_edit_fields(
        "plan_x", PlanFieldsBody(edits={"budget_ngn": 9000, "days": 3}),
        db=db, brand_ctx={"brand_id": "brand_1"},
    ))
    assert out["plan_edited"] is True
    assert out["plan"]["platforms"][0]["budget_ngn"] == 9000
    assert out["plan"]["platforms"][0]["days"] == 3


def test_a_save_that_changed_nothing_does_not_claim_the_plan_was_edited():
    from app.agents.jane_ads.router import PlanFieldsBody, meta_plan_edit_fields

    db = _FakeDb(_pending_doc())
    out = _run(meta_plan_edit_fields(
        "plan_x", PlanFieldsBody(edits={"age_min": 9}),
        db=db, brand_ctx={"brand_id": "brand_1"},
    ))
    assert out["plan_edited"] is False


# ── Rebuilding Jane's reasoning from the edited plan ──────────────────────────

def test_saving_rebuilds_janes_reasoning_from_the_edited_plan(monkeypatch):
    """Her prose NAMES the budget, duration, pockets and interests. Patching the
    numbers alone left the card arguing for choices the client had overruled."""
    from app.agents.jane_ads import plan_fields
    from app.agents.jane_ads.router import PlanFieldsBody, meta_plan_edit_fields

    async def _no_meta(db, plan, req, audience_text=""):
        from app.agents.jane_ads.summary import build_campaign_summary
        return build_campaign_summary(plan, req, audience_text=audience_text).model_dump(mode="json")

    monkeypatch.setattr(plan_fields, "rebuild_summary", _no_meta)

    db = _FakeDb(_pending_doc())
    out = _run(meta_plan_edit_fields(
        "plan_x", PlanFieldsBody(edits={"budget_ngn": 9000, "days": 3}),
        db=db, brand_ctx={"brand_id": "brand_1"},
    ))
    assert out["summary"] is not None
    # The rebuilt prose must carry the CLIENT's numbers, not Jane's originals.
    blob = str(out["summary"])
    assert "9,000" in blob or "9000" in blob
    assert "3 days" in blob
    # And it must be what got stored, or the launch and the card disagree again.
    assert db.plans.doc["summary"] == out["summary"]


def test_a_failed_rebuild_keeps_the_previous_summary_rather_than_blanking_it(monkeypatch):
    """A stale summary is worse than a fresh one and far better than none — losing the
    whole reasoning block because Meta's estimate endpoint blipped is not acceptable."""
    from app.agents.jane_ads.router import PlanFieldsBody, meta_plan_edit_fields

    async def _fails(db, plan, req, audience_text=""):
        return None

    monkeypatch.setattr("app.agents.jane_ads.plan_fields.rebuild_summary", _fails)
    doc = _pending_doc()
    doc["summary"] = {"objective": {"value": "the original", "reason": "r"}}
    db = _FakeDb(doc)
    out = _run(meta_plan_edit_fields(
        "plan_x", PlanFieldsBody(edits={"caption": "new words"}),
        db=db, brand_ctx={"brand_id": "brand_1"},
    ))
    assert out["applied"] == ["caption"]
    assert out["summary"] is None
    assert db.plans.doc["summary"] == {"objective": {"value": "the original", "reason": "r"}}


def test_the_reach_estimate_is_refetched_not_carried_over(monkeypatch):
    """Changing locations, interests, age, gender or placement is exactly what moves
    reach. Reusing the old figure would attach Jane's audience size to the client's."""
    from app.agents.jane_ads import plan_fields

    calls = {"n": 0}

    class _Adapter:
        def __init__(self, *a, **k):
            pass

        async def get_delivery_estimate(self, targeting):
            calls["n"] += 1
            calls["targeting"] = targeting
            return {"users_lower_bound": 100, "users_upper_bound": 200}

    monkeypatch.setattr("app.agents.jane_ads.adapters.meta.MetaAdPlatformAdapter", _Adapter)

    async def _named(geo, region="", access_token=""):
        return {"geo_locations": {"cities": [{"key": "1"}]}}

    monkeypatch.setattr("app.agents.jane_ads.geo.meta_targeting_from_geo_named", _named)
    plan = _plan(audience_targeting={"genders": [1]})
    out = _run(plan_fields.rebuild_summary(None, plan, _req(), "shoppers"))
    assert calls["n"] == 1
    # The estimate must use the SAME merged shape the launch sends.
    assert calls["targeting"]["genders"] == [1]
    assert "cities" in calls["targeting"]["geo_locations"]
    assert out is not None
