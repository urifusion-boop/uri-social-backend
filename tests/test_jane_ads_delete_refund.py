"""Deleting a campaign gives back what it was charged but didn't spend."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agents.jane_ads import launch_governance as gov
from app.agents.jane_ads import router
from app.agents.jane_ads.adapters import meta
from app.agents.jane_ads.billing import unspent_refund_ngn
from app.agents.jane_ads.store import MongoWalletStore
from app.agents.jane_ads.wallet import WalletService
from app.core.config import settings

from tests.test_jane_ads_growth import mongomock_motor, run  # noqa: F401

CTX = {"brand_id": "b1", "user_id": "u1"}
CAMPAIGN = {"campaign_id": "c1", "brand_id": "b1", "business_id": "b1",
            "charged_upfront_ngn": 6000.0, "ad_spend_markup": 1.111111, "budget_ngn": 5400.0}


def _db(record=CAMPAIGN, balance=1000.0):
    db = mongomock_motor.AsyncMongoMockClient()["refund"]
    run(db["jane_ads_meta_campaigns"].insert_one(dict(record)))
    run(WalletService(MongoWalletStore(db)).top_up("b1", balance, enforce_minimum=False))
    return db


def _balance(db):
    return run(WalletService(MongoWalletStore(db)).get_balance("b1"))


@pytest.fixture
def platform(monkeypatch):
    state = SimpleNamespace(spend=0.0, read_fails=False, deleted=[])

    class Adapter:
        def __init__(self, *_, **__):
            pass

        async def fetch_per_ad_spend(self, _):
            if state.read_fails:
                raise RuntimeError("meta down")
            return [SimpleNamespace(spend_ngn=state.spend)]

        async def delete_campaign(self, campaign_id):
            state.deleted.append(campaign_id)
            return True

    async def allowed(*_, **__):
        return None

    async def nothing(*_, **__):
        return None

    async def no_google(*_, **__):
        return None

    from app.agents.jane_ads import google_flow
    monkeypatch.setattr(meta, "MetaAdPlatformAdapter", Adapter)
    monkeypatch.setattr(gov, "require_launch_role", allowed)
    monkeypatch.setattr(gov, "audit", nothing)
    monkeypatch.setattr(google_flow, "find_record", no_google)
    monkeypatch.setattr(settings, "META_AD_ACCOUNT_ID", "act")
    monkeypatch.setattr(settings, "META_ADS_ACCESS_TOKEN", "tok")
    return state


def test_a_campaign_that_never_ran_gets_everything_back(platform):
    db = _db()
    out = run(router.delete_meta_campaign("c1", db, CTX))
    assert out == {"deleted": True, "refunded_ngn": 6000.0} and _balance(db) == 7000.0


def test_spend_and_its_fee_are_kept_and_the_rest_comes_back(platform):
    platform.spend = 2700.0
    db = _db()
    assert run(router.delete_meta_campaign("c1", db, CTX))["refunded_ngn"] == 3000.0
    assert _balance(db) == 4000.0


def test_a_fully_spent_campaign_refunds_nothing(platform):
    platform.spend = 5400.0
    db = _db()
    assert run(router.delete_meta_campaign("c1", db, CTX))["refunded_ngn"] == 0.0
    assert _balance(db) == 1000.0


def test_a_failed_spend_read_stops_the_delete_and_moves_no_money(platform):
    platform.read_fails = True
    db = _db()
    with pytest.raises(HTTPException) as e:
        run(router.delete_meta_campaign("c1", db, CTX))
    assert e.value.status_code == 502 and not platform.deleted
    assert run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": "c1"})) and _balance(db) == 1000.0


def test_a_campaign_billed_as_it_spends_holds_nothing_to_refund(platform):
    platform.read_fails = True
    db = _db({"campaign_id": "c1", "brand_id": "b1", "business_id": "b1", "budget_ngn": 5400.0})
    assert run(router.delete_meta_campaign("c1", db, CTX))["refunded_ngn"] == 0.0
    assert _balance(db) == 1000.0


def test_another_brand_cannot_delete_or_collect_the_refund(platform):
    db = _db()
    with pytest.raises(HTTPException):
        run(router.delete_meta_campaign("c1", db, {"brand_id": "b2", "user_id": "u2"}))
    assert not platform.deleted and _balance(db) == 1000.0


def test_a_second_delete_cannot_refund_twice(platform):
    db = _db()
    assert run(router._remove_and_refund(db, "jane_ads_meta_campaigns", "c1", 0.0, CTX)) == 6000.0
    assert run(router._remove_and_refund(db, "jane_ads_meta_campaigns", "c1", 0.0, CTX)) == 0.0
    assert _balance(db) == 7000.0


def test_the_wallet_refund_is_once_per_campaign_and_marked_as_a_refund():
    db = _db()
    wallet = WalletService(MongoWalletStore(db))
    first = run(wallet.refund_ad_spend("b1", 500.0, "c1"))
    assert run(wallet.refund_ad_spend("b1", 500.0, "c1")).transaction_id == first.transaction_id
    assert first.type.value == "refund" and _balance(db) == 1500.0


def test_an_old_tiktok_campaign_is_not_refunded_from_a_partial_spend_report():
    class Adapter:
        async def fetch_per_ad_spend(self, _):
            raise AssertionError("the report can't cover this campaign")

    old = {"campaign_id": "c1", "charged_upfront_ngn": 100.0,
           "created_at": datetime.now(timezone.utc) - timedelta(days=45)}
    assert run(router._spend_before_delete(Adapter(), old, window_days=30)) is None


def test_refund_maths():
    assert unspent_refund_ngn({"charged_upfront_ngn": 35000.0}, 10000.0) == 25000.0
    assert unspent_refund_ngn({}, 0.0) == 0.0
