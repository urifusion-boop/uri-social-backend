"""Keeping a campaign running past the date it was set to end.

The point of extending rather than relaunching: the campaign id, the ad set, the
creative and everything Meta has learned about who responds all survive. A replacement
campaign throws that away, which is worst for exactly the campaigns worth continuing.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.agents.jane_ads.extend import (
    MAX_EXTEND_DAYS, ExtendError, new_end_time, quote,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── The quote ─────────────────────────────────────────────────────────────────

def test_the_quote_is_days_times_the_daily_spend():
    q = quote(daily_ngn=2500, days=7, markup=1.1)
    assert q["ad_spend_ngn"] == 17500
    assert q["total_due_ngn"] == 19250      # the markup this campaign was sold under


def test_the_campaigns_own_markup_is_used_not_todays_rate():
    """billing.py keeps each campaign on the basis its wallet was gated against, so a
    later fee change never re-bases a live campaign onto maths its owner never agreed
    to. An extension has to follow the same rule."""
    assert quote(2000, 5, markup=1.0)["total_due_ngn"] == 10000
    assert quote(2000, 5, markup=1.25)["total_due_ngn"] == 12500


def test_an_absurd_length_is_refused():
    with pytest.raises(ExtendError):
        quote(2500, 0, 1.1)
    with pytest.raises(ExtendError):
        quote(2500, MAX_EXTEND_DAYS + 1, 1.1)


def test_a_campaign_below_the_daily_minimum_cannot_be_extended_as_is():
    """Extending must not be a way around the floor a fresh plan has to clear."""
    with pytest.raises(ExtendError) as e:
        quote(daily_ngn=1500, days=7, markup=1.1)
    assert "minimum" in str(e.value)


# ── Where the new end date lands ──────────────────────────────────────────────

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def test_a_running_campaign_extends_from_its_current_end():
    future = (NOW + timedelta(days=3)).isoformat()
    assert new_end_time(future, 7, now=NOW) == NOW + timedelta(days=10)


def test_a_campaign_that_already_ended_extends_from_today():
    """Three more days on a campaign that ended last week must mean three days of
    delivery from now — not three days that already elapsed."""
    past = (NOW - timedelta(days=7)).isoformat()
    assert new_end_time(past, 3, now=NOW) == NOW + timedelta(days=3)


def test_an_unparseable_or_missing_end_date_falls_back_to_now():
    assert new_end_time(None, 5, now=NOW) == NOW + timedelta(days=5)
    assert new_end_time("not a date", 5, now=NOW) == NOW + timedelta(days=5)


# ── The endpoint ──────────────────────────────────────────────────────────────

class _Adapter:
    def __init__(self, daily=2500, end=None, fail_extend=False):
        self.daily = daily
        self.end = end or (NOW + timedelta(days=1)).isoformat()
        self.fail_extend = fail_extend
        self.extended_to = None

    async def fetch_adset_schedule(self, campaign_id):
        return {"adset_id": "as_1", "ad_id": "ad_1", "status": "PAUSED",
                "effective_status": "PAUSED", "daily_ngn": self.daily,
                "lifetime_ngn": 0, "start_time": None, "end_time": self.end}

    async def extend_adset(self, adset_id, ends_at):
        if self.fail_extend:
            from app.agents.jane_ads.adapters.meta import MetaAPIError
            raise MetaAPIError("nope")
        self.extended_to = ends_at
        return {"end_time": ends_at.isoformat()}


class _Wallet:
    def __init__(self, balance=100000):
        self.balance = balance
        self.charges = []

    async def get_balance(self, business_id):
        return self.balance

    async def charge_ad_spend(self, business_id, amount, campaign_id=""):
        self.charges.append(amount)
        self.balance -= amount


class _Coll:
    def __init__(self, doc):
        self.doc = doc
        self.updates = []

    async def find_one(self, *a, **k):
        return self.doc

    async def update_one(self, q, u, **k):
        self.updates.append(u)


class _Db:
    def __init__(self, doc):
        self.c = _Coll(doc)

    def __getitem__(self, name):
        return self.c


def _record(**over):
    base = {"campaign_id": "c1", "brand_id": "b1", "business_id": "biz1",
            "adset_id": "as_1", "ad_spend_markup": 1.1}
    base.update(over)
    return base


def _patch(monkeypatch, adapter, wallet):
    monkeypatch.setattr("app.agents.jane_ads.adapters.meta.MetaAdPlatformAdapter",
                        lambda *a, **k: adapter)
    monkeypatch.setattr("app.agents.jane_ads.wallet.WalletService", lambda *a, **k: wallet)


def test_extending_charges_only_after_meta_confirms(monkeypatch):
    """A launch debits AFTER the campaign exists so nobody pays for a campaign that was
    never created. Extending follows the same order for the same reason."""
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    adapter, wallet = _Adapter(), _Wallet()
    _patch(monkeypatch, adapter, wallet)
    out = _run(extend_campaign("c1", ExtendBody(days=7, confirm=True),
                               db=_Db(_record()), brand_ctx={"brand_id": "b1"}))
    assert adapter.extended_to is not None
    assert wallet.charges == [19250.0]
    assert out["extended_by_days"] == 7


def test_a_failed_extension_charges_nothing(monkeypatch):
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    adapter, wallet = _Adapter(fail_extend=True), _Wallet()
    _patch(monkeypatch, adapter, wallet)
    with pytest.raises(HTTPException) as e:
        _run(extend_campaign("c1", ExtendBody(days=7, confirm=True),
                             db=_Db(_record()), brand_ctx={"brand_id": "b1"}))
    assert e.value.status_code == 502
    assert wallet.charges == []


def test_an_empty_wallet_is_refused_before_anything_changes(monkeypatch):
    """The common failure should be a clean refusal, not a half-finished extension."""
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    adapter, wallet = _Adapter(), _Wallet(balance=100)
    _patch(monkeypatch, adapter, wallet)
    with pytest.raises(HTTPException) as e:
        _run(extend_campaign("c1", ExtendBody(days=7, confirm=True),
                             db=_Db(_record()), brand_ctx={"brand_id": "b1"}))
    assert e.value.status_code == 402
    assert adapter.extended_to is None
    assert wallet.charges == []


def test_extending_needs_an_explicit_confirmation(monkeypatch):
    """It restarts spending on a campaign that had stopped. Never implied."""
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    adapter, wallet = _Adapter(), _Wallet()
    _patch(monkeypatch, adapter, wallet)
    with pytest.raises(HTTPException) as e:
        _run(extend_campaign("c1", ExtendBody(days=7), db=_Db(_record()),
                             brand_ctx={"brand_id": "b1"}))
    assert e.value.status_code == 400
    assert adapter.extended_to is None


def test_another_brands_campaign_cannot_be_extended(monkeypatch):
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    _patch(monkeypatch, _Adapter(), _Wallet())
    with pytest.raises(HTTPException) as e:
        _run(extend_campaign("c1", ExtendBody(days=7, confirm=True),
                             db=_Db(_record()), brand_ctx={"brand_id": "someone_else"}))
    assert e.value.status_code == 404


def test_the_extension_is_recorded_on_the_campaign(monkeypatch):
    """What was charged and until when — otherwise the ledger shows a debit against a
    campaign whose budget never appears to have changed."""
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    adapter, wallet = _Adapter(), _Wallet()
    _patch(monkeypatch, adapter, wallet)
    db = _Db(_record())
    _run(extend_campaign("c1", ExtendBody(days=5, confirm=True),
                         db=db, brand_ctx={"brand_id": "b1"}))
    update = db.c.updates[0]
    assert update["$inc"]["charged_upfront_ngn"] == 13750.0
    assert update["$push"]["extensions"]["days"] == 5
