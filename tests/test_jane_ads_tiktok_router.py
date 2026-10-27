"""
Router-level coverage for the TikTok ads endpoints ported onto aws/prod.

The rule for this port: Meta behaves exactly as before. So half of these tests are
negative checks — a Meta campaign, or one that exists nowhere, still takes the untouched
Meta path (404 / the Meta adapter), and only a campaign in the TikTok collection is
routed to TikTok.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.agents.jane_ads import router as jane_router
from app.agents.jane_ads.models import CampaignPlan, Goal, Platform, PlatformPlan, PurchaseBehaviour
from app.agents.jane_ads.objectives import CHOICES
from app.core.config import settings

BRAND = {"brand_id": "brand-1", "user_id": "user-1"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Coll:
    def __init__(self, docs=None):
        self.docs = {d["campaign_id"]: d for d in (docs or [])}
        self.updates = []

    async def find_one(self, query):
        return self.docs.get(query.get("campaign_id"))

    async def update_one(self, query, update, **kw):
        self.updates.append((query, update))

    async def insert_one(self, doc):
        self.updates.append(("insert", doc))


class _Db:
    def __init__(self, meta=None, tiktok=None):
        self.colls = {"jane_ads_meta_campaigns": _Coll(meta), "jane_ads_tiktok_campaigns": _Coll(tiktok)}

    def __getitem__(self, name):
        return self.colls.setdefault(name, _Coll())


@pytest.fixture(autouse=True)
def _tiktok_configured(monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_ADS_ADVERTISER_ID", "adv123", raising=False)
    monkeypatch.setattr(settings, "TIKTOK_ADS_ACCESS_TOKEN", "tok", raising=False)


TIKTOK_CAMPAIGN = {"campaign_id": "tt1", "brand_id": "brand-1", "business_id": "b1", "ad_spend_markup": 1.1112}
META_CAMPAIGN = {"campaign_id": "m1", "brand_id": "brand-1", "business_id": "b1"}


# ── Objectives: TikTok wording on request, Meta's list otherwise ────────────────

def _choices(out):
    return [{k: o[k] for k in ("value", "label", "blurb", "caveat")} for o in out["objectives"]]


def test_objectives_default_is_metas_list_unchanged():
    assert _choices(_run(jane_router.list_objectives())) == CHOICES


@pytest.mark.parametrize("platform", ["google", "meta", "TIKTOK", "garbage"])
def test_any_other_platform_value_gets_metas_list(platform):
    assert _choices(_run(jane_router.list_objectives(platform=platform))) == CHOICES


def test_tiktok_objectives_keep_values_and_labels_but_change_the_wording():
    out = _run(jane_router.list_objectives(platform="tiktok"))["objectives"]

    assert [(o["value"], o["label"]) for o in out] == [(c["value"], c["label"]) for c in CHOICES]
    assert [o["blurb"] for o in out] != [c["blurb"] for c in CHOICES]
    assert not any("WhatsApp orders" in o["blurb"] for o in out)


def test_a_tiktok_client_is_told_nothing_about_other_platforms_in_the_objective_list():
    # Jane also echoes the picked blurb into the chat ("Engagement it is — …"), so any
    # Meta/WhatsApp comparison here reaches the client twice.
    out = _run(jane_router.list_objectives(platform="tiktok"))["objectives"]
    for o in out:
        text = f'{o["label"]} {o["blurb"]} {o["caveat"]}'.lower()
        for word in ("meta", "instagram", "facebook", "whatsapp", "pixel"):
            assert word not in text, (o["value"], word)
        assert len(o["blurb"]) <= 70, o["blurb"]


# ── Which campaigns count as TikTok ─────────────────────────────────────────────

def test_only_this_brands_tiktok_campaign_is_routed_to_tiktok():
    db = _Db(tiktok=[TIKTOK_CAMPAIGN])
    assert _run(jane_router._tiktok_live_campaign(db, "tt1", "brand-1")) == TIKTOK_CAMPAIGN
    assert _run(jane_router._tiktok_live_campaign(db, "tt1", "someone-else")) is None
    assert _run(jane_router._tiktok_live_campaign(db, "nope", "brand-1")) is None


def test_a_challenge_rebuild_stays_on_tiktok_and_meta_stays_with_jane():
    def plan(platform):
        return CampaignPlan(
            business_id="b1", goal=Goal.MESSAGES, behaviour=PurchaseBehaviour.DISCOVER,
            platforms=[PlatformPlan(platform=platform, budget_ngn=31_000, days=1, variants=1, test_scope="none")],
            per_business_cap_ngn=31_000, account_cap_ngn=31_000,
        )

    assert jane_router._preferred_platform_for_challenge(plan(Platform.TIKTOK)) == "tiktok"
    assert jane_router._preferred_platform_for_challenge(plan(Platform.META)) == ""


# ── Keep it running ─────────────────────────────────────────────────────────────

SCHEDULE = {"daily_ngn": 31_000.0, "end_time": "2026-10-27 12:55:00", "has_ended": False,
            "budget_ngn": 31_000.0, "adgroup_id": "222"}


def test_a_tiktok_campaign_is_quoted_against_tiktoks_own_floor():
    db = _Db(tiktok=[TIKTOK_CAMPAIGN])
    with patch("app.agents.jane_ads.adapters.tiktok.TikTokAdsAdapter.fetch_adgroup_schedule",
               AsyncMock(return_value=SCHEDULE)), \
         patch("app.agents.jane_ads.wallet.WalletService.get_balance", AsyncMock(return_value=500_000.0)):
        out = _run(jane_router.extend_quote("tt1", days=2, db=db, brand_ctx=BRAND))

    assert out["campaign_id"] == "tt1"
    assert out["affordable"] is True
    assert out["daily_ngn"] == 31_000.0


def test_a_tiktok_quote_under_tiktoks_floor_is_refused_in_tiktoks_name():
    db = _Db(tiktok=[TIKTOK_CAMPAIGN])
    thin = {**SCHEDULE, "daily_ngn": 5_000.0}
    with patch("app.agents.jane_ads.adapters.tiktok.TikTokAdsAdapter.fetch_adgroup_schedule",
               AsyncMock(return_value=thin)), \
         patch("app.agents.jane_ads.wallet.WalletService.get_balance", AsyncMock(return_value=500_000.0)):
        with pytest.raises(HTTPException) as e:
            _run(jane_router.extend_quote("tt1", days=2, db=db, brand_ctx=BRAND))

    assert e.value.status_code == 400
    assert "TikTok" in e.value.detail and "Meta" not in e.value.detail


def test_extending_a_tiktok_campaign_needs_confirmation():
    db = _Db(tiktok=[TIKTOK_CAMPAIGN])
    with pytest.raises(HTTPException) as e:
        _run(jane_router.extend_campaign("tt1", jane_router.ExtendBody(days=2, confirm=False), db=db, brand_ctx=BRAND))
    assert e.value.status_code == 400


def test_an_unknown_campaign_still_gets_the_untouched_meta_404():
    db = _Db()
    with pytest.raises(HTTPException) as e:
        _run(jane_router.extend_quote("nope", days=2, db=db, brand_ctx=BRAND))
    assert e.value.status_code == 404

    with pytest.raises(HTTPException) as e:
        _run(jane_router.extend_campaign("nope", jane_router.ExtendBody(days=2, confirm=True), db=db, brand_ctx=BRAND))
    assert e.value.status_code == 404


def test_a_meta_campaign_never_reaches_the_tiktok_adapter():
    db = _Db(meta=[META_CAMPAIGN])
    tiktok_called = AsyncMock()
    with patch("app.agents.jane_ads.adapters.tiktok.TikTokAdsAdapter.fetch_adgroup_schedule", tiktok_called), \
         patch("app.agents.jane_ads.adapters.meta.MetaAdPlatformAdapter.fetch_adset_schedule",
               AsyncMock(return_value={**SCHEDULE, "adset_id": "as1"})):
        try:
            _run(jane_router.extend_quote("m1", days=2, db=db, brand_ctx=BRAND))
        except Exception:
            pass  # what Meta's own path does with this fake schedule is not under test

    tiktok_called.assert_not_called()


# ── Live targeting ──────────────────────────────────────────────────────────────

def test_live_targeting_for_a_tiktok_campaign_reads_tiktok_not_meta():
    db = _Db(tiktok=[TIKTOK_CAMPAIGN])
    live = {"adgroup_id": "222", "operation_status": "ENABLE", "targeting": {"gender": "GENDER_UNLIMITED"}}
    with patch("app.agents.jane_ads.adapters.tiktok.TikTokAdsAdapter.fetch_adgroup_targeting",
               AsyncMock(return_value=live)), \
         patch("app.agents.jane_ads.live_edit.describe_live_tiktok", AsyncMock(return_value=[{"key": "gender"}])):
        out = _run(jane_router.get_live_targeting("tt1", db=db, brand_ctx=BRAND))

    assert out["adset_id"] == "222"
    assert out["delivering"] is True
    assert "TikTok" in out["learning_warning"]


def test_live_targeting_for_an_unknown_campaign_is_still_a_404():
    with pytest.raises(HTTPException) as e:
        _run(jane_router.get_live_targeting("nope", db=_Db(), brand_ctx=BRAND))
    assert e.value.status_code == 404


def test_editing_a_tiktok_campaign_with_no_edits_is_a_400():
    db = _Db(tiktok=[TIKTOK_CAMPAIGN])
    with pytest.raises(HTTPException) as e:
        _run(jane_router.edit_live_targeting("tt1", jane_router.LiveTargetingBody(edits={}), db=db, brand_ctx=BRAND))
    assert e.value.status_code == 400


# ── Status toggle remembers what it did ─────────────────────────────────────────

def test_activating_a_tiktok_campaign_persists_its_state():
    db = _Db(tiktok=[TIKTOK_CAMPAIGN])
    with patch("app.agents.jane_ads.adapters.tiktok.TikTokAdsAdapter.set_delivery",
               AsyncMock(return_value={"status": "ok"})):
        _run(jane_router.set_meta_campaign_status(
            "tt1", jane_router.CampaignStatusBody(active=True), db=db, brand_ctx=BRAND))

    (query, update), = db["jane_ads_tiktok_campaigns"].updates
    assert query == {"campaign_id": "tt1"}
    assert update == {"$set": {"status": "active"}}
