"""Editing the targeting of a campaign that is already live.

Covers only the Campaign Management PRD's "Audience or geography edit" row (§19) and
the execution rules it depends on: CM13 (an ack is not an effect), CM15 (an unknown
write is reconciled, not retried) and CM17 (outside edits are not overwritten).
"""
import asyncio

import pytest
from fastapi import HTTPException

from unittest.mock import patch

from app.agents.jane_ads.live_edit import (
    EDITABLE, TIKTOK_EDITABLE, build_targeting_edit, build_tiktok_targeting_edit,
    describe_live, describe_live_tiktok, targeting_fingerprint,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


BASE = {
    "age_min": 25, "age_max": 45, "genders": [2],
    "geo_locations": {"cities": [{"key": "1"}]},
    "flexible_spec": [{"interests": [{"id": "1", "name": "Fashion"}]}],
}


def test_only_targeting_fields_are_offered():
    """Budget, schedule, objective and bid strategy are separate action families in the
    PRD and behave differently after spend. They must not appear as editable here."""
    keys = {f["key"] for f in describe_live(BASE)}
    assert keys == set(EDITABLE) - {"locations"} | ({"locations"} & keys)
    assert "budget_ngn" not in keys
    assert "days" not in keys
    assert "caption" not in keys


def test_the_fingerprint_moves_only_when_targeting_does():
    assert targeting_fingerprint(BASE) == targeting_fingerprint(dict(BASE))
    changed = {**BASE, "age_min": 26}
    assert targeting_fingerprint(BASE) != targeting_fingerprint(changed)
    # Key order is not a change.
    reordered = {k: BASE[k] for k in reversed(list(BASE))}
    assert targeting_fingerprint(BASE) == targeting_fingerprint(reordered)


def test_an_unsupported_field_is_refused_with_a_reason():
    targeting, applied, rejected = _run(build_targeting_edit(
        BASE, {"budget_ngn": 5000}, "Lagos"))
    assert targeting is None
    assert applied == []
    assert "already launched" in rejected[0]


def test_settings_not_being_edited_survive_the_write():
    """Meta REPLACES the whole targeting object, so an edit that sends only the changed
    keys silently drops the geo, the age and everything else."""
    targeting, applied, _ = _run(build_targeting_edit(BASE, {"gender": "men"}, "Lagos"))
    assert applied == ["gender"]
    assert targeting["genders"] == [1]
    assert targeting["geo_locations"] == {"cities": [{"key": "1"}]}
    assert targeting["age_min"] == 25
    assert targeting["flexible_spec"] == BASE["flexible_spec"]


def test_going_broad_removes_the_key_rather_than_merging_the_old_value_back():
    """apply_edits deletes these to mean 'broad'. A naive merge would resurrect the
    value the client just cleared."""
    targeting, applied, _ = _run(build_targeting_edit(BASE, {"gender": "all"}, "Lagos"))
    assert applied == ["gender"]
    assert "genders" not in targeting


def test_an_invalid_age_is_refused_before_anything_is_written():
    targeting, applied, rejected = _run(build_targeting_edit(BASE, {"age_min": 12}, "Lagos"))
    assert targeting is None
    assert applied == []
    assert rejected


# ── The endpoint's execution rules ────────────────────────────────────────────

class _Adapter:
    def __init__(self, targeting, *, effective="PAUSED", write=None, drift=None):
        self.targeting = dict(targeting)
        self.effective = effective
        self._write = write
        self._drift = drift
        self.writes = []
        self.reads = 0

    async def fetch_adset_targeting(self, campaign_id):
        self.reads += 1
        # `drift` simulates someone editing in Ads Manager between read and write.
        if self._drift and self.reads > 1:
            return {"adset_id": "as_1", "effective_status": self.effective,
                    "targeting": self._drift}
        return {"adset_id": "as_1", "effective_status": self.effective,
                "targeting": self.targeting}

    async def update_adset_targeting(self, adset_id, targeting, validate_only=False):
        if validate_only:
            return {"validated": True}
        self.writes.append(targeting)
        if self._write:
            raise self._write
        self.targeting = dict(targeting)
        return {"applied": True, "targeting": self.targeting}


class _Coll:
    def __init__(self, doc):
        self.doc = doc
        self.inserted = []

    async def find_one(self, *a, **k):
        return self.doc

    async def insert_one(self, doc):
        self.inserted.append(doc)


class _Db:
    def __init__(self, doc):
        self.campaigns = _Coll(doc)
        self.edits = _Coll(None)

    def __getitem__(self, name):
        return self.edits if "live_targeting_edits" in name else self.campaigns


def _record():
    return {"campaign_id": "c1", "brand_id": "b1", "adset_id": "as_1", "geo_city": "Lagos"}


def _patch_adapter(monkeypatch, adapter):
    monkeypatch.setattr("app.agents.jane_ads.adapters.meta.MetaAdPlatformAdapter",
                        lambda *a, **k: adapter)


def test_a_change_made_elsewhere_is_not_overwritten(monkeypatch):
    """CM17. The client opened the screen, someone edited the ad set in Ads Manager,
    and our write would silently revert them. Refuse instead."""
    from app.agents.jane_ads.router import LiveTargetingBody, edit_live_targeting

    adapter = _Adapter(BASE)
    _patch_adapter(monkeypatch, adapter)
    with pytest.raises(HTTPException) as e:
        _run(edit_live_targeting("c1", LiveTargetingBody(edits={"gender": "men"},
                                                         baseline="not-the-current-hash"),
                                 db=_Db(_record()), brand_ctx={"brand_id": "b1"}))
    assert e.value.status_code == 409
    assert adapter.writes == []


def test_a_verified_write_reports_what_meta_actually_holds(monkeypatch):
    """CM13. The result is read back from Meta, never inferred from a 200."""
    from app.agents.jane_ads.router import LiveTargetingBody, edit_live_targeting

    adapter = _Adapter(BASE)
    _patch_adapter(monkeypatch, adapter)
    db = _Db(_record())
    out = _run(edit_live_targeting(
        "c1", LiveTargetingBody(edits={"gender": "men"}, baseline=targeting_fingerprint(BASE)),
        db=db, brand_ctx={"brand_id": "b1"}))
    assert out["applied"] == ["gender"]
    assert out["verified"] is True
    assert adapter.targeting["genders"] == [1]
    # And the change is recorded, with what it was before.
    assert db.edits.inserted[0]["fields"] == ["gender"]
    assert db.edits.inserted[0]["before"]["genders"] == [2]


def test_a_lost_response_is_reconciled_not_retried(monkeypatch):
    """CM15. A timeout can mean the write applied and the answer was lost. Re-read and
    say which it was — never fire the mutation again."""
    from app.agents.jane_ads.adapters.meta import MetaAPIError
    from app.agents.jane_ads.router import LiveTargetingBody, edit_live_targeting

    applied_after_all = {**BASE, "genders": [1]}
    adapter = _Adapter(BASE, write=MetaAPIError("timeout"), drift=applied_after_all)
    _patch_adapter(monkeypatch, adapter)
    with pytest.raises(HTTPException) as e:
        _run(edit_live_targeting(
            "c1", LiveTargetingBody(edits={"gender": "men"},
                                    baseline=targeting_fingerprint(BASE)),
            db=_Db(_record()), brand_ctx={"brand_id": "b1"}))
    assert len(adapter.writes) == 1          # written once, never retried
    assert "did apply" in e.value.detail


def test_a_delivering_campaign_is_warned_about_the_learning_reset(monkeypatch):
    """We cannot avoid the reset, so the client is told before choosing."""
    from app.agents.jane_ads.router import get_live_targeting

    adapter = _Adapter(BASE, effective="ACTIVE")
    _patch_adapter(monkeypatch, adapter)
    out = _run(get_live_targeting("c1", db=_Db(_record()), brand_ctx={"brand_id": "b1"}))
    assert out["delivering"] is True
    assert "restarts Meta's learning" in out["learning_warning"]


def test_a_paused_campaign_gets_no_learning_warning(monkeypatch):
    from app.agents.jane_ads.router import get_live_targeting

    _patch_adapter(monkeypatch, _Adapter(BASE, effective="PAUSED"))
    out = _run(get_live_targeting("c1", db=_Db(_record()), brand_ctx={"brand_id": "b1"}))
    assert out["delivering"] is False
    assert out["learning_warning"] == ""


def test_another_brands_campaign_is_not_editable(monkeypatch):
    from app.agents.jane_ads.router import LiveTargetingBody, edit_live_targeting

    _patch_adapter(monkeypatch, _Adapter(BASE))
    with pytest.raises(HTTPException) as e:
        _run(edit_live_targeting("c1", LiveTargetingBody(edits={"gender": "men"}),
                                 db=_Db(_record()), brand_ctx={"brand_id": "someone_else"}))
    assert e.value.status_code == 404


def test_live_locations_are_read_from_the_ad_sets_own_geo(monkeypatch):
    """Meta returns place NAMES inline on geo_locations. Showing "—" for a campaign
    that really targets Ikeja G.R.A would tell the client their ad runs nowhere.
    Live-caught against a real ad set."""
    from app.agents.jane_ads.live_edit import live_location_names

    targeting = {**BASE, "geo_locations": {
        "location_types": ["home"],
        "neighborhoods": [
            {"key": "2891285", "name": "Ikeja G.R.A", "region": "Lagos State"},
            {"key": "2891329", "name": "Lekki Peninsula", "region": "Lagos State"},
        ],
        "cities": [{"key": "1", "name": "Lagos"}],
    }}
    assert live_location_names(targeting) == ["Ikeja G.R.A", "Lekki Peninsula", "Lagos"]
    shown = {f["key"]: f["value"] for f in describe_live(targeting)}
    assert shown["locations"] == ["Ikeja G.R.A", "Lekki Peninsula", "Lagos"]


def test_editing_interests_keeps_behaviours_and_life_events(monkeypatch):
    """Live-caught on a real ad set carrying 7 interests and 1 life_event. Meta rejects
    an id filed under the wrong key, so these cannot be folded in with the interests —
    and dropping them silently narrows an audience the client never touched."""
    async def _resolve(client, base, token, keyword):
        return {"id": "new", "name": keyword}

    monkeypatch.setattr("app.agents.jane_ads.audience_targeting._resolve_interest", _resolve)
    current = {**BASE, "flexible_spec": [{
        "interests": [{"id": "1", "name": "Fashion"}],
        "life_events": [{"id": "6003", "name": "Newly engaged (1 year)"}],
    }]}
    targeting, applied, _ = _run(build_targeting_edit(current, {"interests": ["Shoes"]}, "Lagos"))
    assert applied == ["interests"]
    entry = targeting["flexible_spec"][0]
    assert [i["name"] for i in entry["interests"]] == ["Shoes"]
    assert entry["life_events"] == [{"id": "6003", "name": "Newly engaged (1 year)"}]


def test_country_wide_targeting_is_named_not_shown_as_nothing():
    """An ad set running across a whole country showed "—", which a client reads as
    "nowhere" when it means "everywhere". Live-reported on a campaign that had fallen
    back to countries:["NG"]."""
    from app.agents.jane_ads.live_edit import live_location_names

    assert live_location_names({"geo_locations": {"countries": ["NG"]}}) == ["Nigeria (nationwide)"]
    shown = {f["key"]: f["value"] for f in describe_live({"geo_locations": {"countries": ["NG"]}})}
    assert shown["locations"] == ["Nigeria (nationwide)"]


def test_named_places_win_over_the_country_fallback():
    """A country code sits alongside the named entries in Meta's payload; showing both
    would imply the ad runs nationwide AND in one neighbourhood."""
    from app.agents.jane_ads.live_edit import live_location_names

    assert live_location_names({"geo_locations": {
        "countries": ["NG"],
        "neighborhoods": [{"key": "1", "name": "Surulere"}],
    }}) == ["Surulere"]


# ── TikTok: the same feature, built natively ─────────────────────────────────

TIKTOK_TARGETING = {
    "gender": "GENDER_FEMALE", "age_groups": ["AGE_25_34", "AGE_35_44"],
    "location_ids": ["2001"],
}


def test_describe_live_tiktok_shows_only_the_real_tiktok_fields():
    async def _fake_names(ids, adv, token):
        return ["Ikeja"]

    with patch("app.agents.jane_ads.adapters.tiktok._tiktok_location_names", _fake_names):
        fields = {f["key"]: f for f in _run(describe_live_tiktok(TIKTOK_TARGETING, "adv123", "tok"))}
    assert set(fields.keys()) == set(TIKTOK_EDITABLE)
    assert "interests" not in fields
    assert "placement" not in fields
    assert fields["locations"]["value"] == ["Ikeja"]
    assert fields["gender"]["value"] == "women"
    assert fields["age_min"]["value"] == 25
    assert fields["age_max"]["value"] == 44


def test_build_tiktok_targeting_edit_refuses_meta_only_fields():
    changed, applied, rejected = _run(build_tiktok_targeting_edit(
        TIKTOK_TARGETING, {"interests": ["Fashion"]}, "adv123", "tok"))
    assert changed is None
    assert applied == []
    assert "already launched" in rejected[0]


def test_build_tiktok_targeting_edit_applies_gender_and_age():
    changed, applied, rejected = _run(build_tiktok_targeting_edit(
        TIKTOK_TARGETING, {"gender": "men", "age_min": 18, "age_max": 24}, "adv123", "tok"))
    assert rejected == []
    assert sorted(applied) == ["age_max", "age_min", "gender"]
    assert changed == {"gender": "GENDER_MALE", "age_groups": ["AGE_18_24"]}
    # Only what changed — TikTok's /adgroup/update/ is a partial update, unlike
    # Meta which needs the whole merged targeting object.
    assert "location_ids" not in changed


def test_build_tiktok_targeting_edit_resolves_locations(monkeypatch):
    async def _fake_resolve(names, advertiser_id, access_token):
        assert advertiser_id == "adv123"
        return [{"name": "Lekki Peninsula", "location_id": "3001"}], []

    monkeypatch.setattr(
        "app.agents.jane_ads.adapters.tiktok._resolve_tiktok_locations", _fake_resolve)
    changed, applied, rejected = _run(build_tiktok_targeting_edit(
        TIKTOK_TARGETING, {"locations": ["Lekki Peninsula"]}, "adv123", "tok"))
    assert rejected == []
    assert applied == ["locations"]
    assert changed == {"location_ids": ["3001"]}


def test_build_tiktok_targeting_edit_reports_unresolved_locations(monkeypatch):
    async def _fake_resolve(names, advertiser_id, access_token):
        return [], ["Nowhereville"]

    monkeypatch.setattr(
        "app.agents.jane_ads.adapters.tiktok._resolve_tiktok_locations", _fake_resolve)
    changed, applied, rejected = _run(build_tiktok_targeting_edit(
        TIKTOK_TARGETING, {"locations": ["Nowhereville"]}, "adv123", "tok"))
    assert changed is None
    assert applied == []
    assert any("Nowhereville" in r for r in rejected)
