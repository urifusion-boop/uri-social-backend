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


def test_a_campaign_predating_the_new_minimum_can_still_be_extended():
    """Live-caught: a real ad set runs ₦1,800/day, launched before the ₦2,000 minimum
    existed. Refusing to continue it would punish exactly the long-running campaigns
    this feature is for — ₦2,000 governs what may be SET on a new plan, not whether an
    ad Meta is already delivering may carry on."""
    q = quote(daily_ngn=1800, days=3, markup=1.1)
    assert q["ad_spend_ngn"] == 5400


def test_a_campaign_meta_will_not_deliver_cannot_be_extended():
    """Below Meta's own floor there is nothing to continue — Meta refuses the ad set."""
    with pytest.raises(ExtendError) as e:
        quote(daily_ngn=1000, days=7, markup=1.1)
    assert "floor" in str(e.value)


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


def test_a_deleted_campaign_says_what_to_do_instead(monkeypatch):
    """Meta refuses to edit a deleted ad set at all (subcode 1487056) — only its name.
    Live-caught. There is nothing to continue, so the client is told to start a new
    campaign rather than shown a raw platform error."""
    from app.agents.jane_ads.adapters.meta import MetaAPIError
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    class _Deleted(_Adapter):
        async def extend_adset(self, adset_id, ends_at):
            raise MetaAPIError("adset extend: Deleted ad sets can't be edited (code=100, subcode=1487056)")

    adapter, wallet = _Deleted(), _Wallet()
    _patch(monkeypatch, adapter, wallet)
    with pytest.raises(HTTPException) as e:
        _run(extend_campaign("c1", ExtendBody(days=3, confirm=True),
                             db=_Db(_record()), brand_ctx={"brand_id": "b1"}))
    assert e.value.status_code == 409
    assert "deleted" in e.value.detail.lower()
    assert wallet.charges == []


# ── TikTok: the same feature, dispatched by platform ─────────────────────────
# Added 2026-09-25 — "Keep it running" needed a real TikTok adapter to extend
# against (fetch_adgroup_schedule/extend_adgroup) before this was buildable;
# see those methods' own docstrings for what's confirmed vs. still unverified.

class _TikTokAdapter:
    """TikTok's own version of _Adapter — same role, TikTok's method names
    (fetch_adgroup_schedule/extend_adgroup, a total budget instead of a daily
    one) and TikTokAdsAPIError instead of MetaAPIError."""

    def __init__(self, daily=31000, budget=217000, end=None, fail_extend=False):
        self.daily = daily
        self.budget = budget
        self.end = end or (NOW + timedelta(days=1)).isoformat()
        self.fail_extend = fail_extend
        self.extended_to = None
        self.extended_budget = None

    async def fetch_adgroup_schedule(self, campaign_id):
        return {"adgroup_id": "ag_1", "ad_id": "ad_1", "operation_status": "ENABLE",
                "budget_ngn": self.budget, "daily_ngn": self.daily,
                "start_time": None, "end_time": self.end, "has_ended": False}

    async def extend_adgroup(self, adgroup_id, new_budget_ngn, new_end_time):
        if self.fail_extend:
            from app.agents.jane_ads.adapters.tiktok import TikTokAdsAPIError
            raise TikTokAdsAPIError("nope")
        self.extended_to = new_end_time
        self.extended_budget = new_budget_ngn
        return {"budget_ngn": new_budget_ngn, "end_time": new_end_time.isoformat()}


class _MultiCollDb:
    """Distinguishes collection names, unlike _Db above (which returns the
    same collection whatever name is asked for — fine for a Meta-only record,
    but _live_campaign's dual-collection lookup needs the Meta collection to
    genuinely come back empty for a TikTok record, exactly like the real
    jane_ads_meta_campaigns/jane_ads_tiktok_campaigns split)."""

    def __init__(self, tiktok_doc):
        self.meta = _Coll(None)
        self.tiktok = _Coll(tiktok_doc)

    def __getitem__(self, name):
        return self.tiktok if name == "jane_ads_tiktok_campaigns" else self.meta


def _tiktok_record(**over):
    base = {"campaign_id": "c1", "brand_id": "b1", "business_id": "biz1",
            "adgroup_id": "ag_1", "ad_spend_markup": 1.1}
    base.update(over)
    return base


def _patch_tiktok(monkeypatch, adapter, wallet):
    monkeypatch.setattr("app.agents.jane_ads.adapters.tiktok.TikTokAdsAdapter",
                        lambda *a, **k: adapter)
    monkeypatch.setattr("app.agents.jane_ads.wallet.WalletService", lambda *a, **k: wallet)
    monkeypatch.setattr("app.core.config.settings.TIKTOK_ADS_ADVERTISER_ID", "adv123")
    monkeypatch.setattr("app.core.config.settings.TIKTOK_ADS_ACCESS_TOKEN", "tok")


def test_tiktok_extending_charges_only_after_tiktok_confirms(monkeypatch):
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    adapter, wallet = _TikTokAdapter(), _Wallet(balance=500000)
    _patch_tiktok(monkeypatch, adapter, wallet)
    out = _run(extend_campaign("c1", ExtendBody(days=7, confirm=True),
                               db=_MultiCollDb(_tiktok_record()), brand_ctx={"brand_id": "b1"}))
    assert adapter.extended_to is not None
    # 31,000/day * 7 days * 1.1 markup = 238,700
    assert wallet.charges == [238700.0]
    assert out["extended_by_days"] == 7
    # Both budget AND end date move together — the whole point of
    # extend_adgroup existing separately from Meta's end-date-only extend.
    assert adapter.extended_budget == 217000 + 31000 * 7


def test_tiktok_extend_uses_tiktoks_own_floor_not_metas(monkeypatch):
    """31,000/day clears TikTok's real floor easily but the quote/floor check must
    use TikTok's ₦31,000, not Meta's ₦1,610 — a campaign spending, say, ₦5,000/day
    would pass Meta's floor and fail TikTok's for real."""
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    adapter, wallet = _TikTokAdapter(daily=5000), _Wallet()
    _patch_tiktok(monkeypatch, adapter, wallet)
    with pytest.raises(HTTPException) as e:
        _run(extend_campaign("c1", ExtendBody(days=7, confirm=True),
                             db=_MultiCollDb(_tiktok_record()), brand_ctx={"brand_id": "b1"}))
    assert e.value.status_code == 400
    assert "TikTok" in e.value.detail
    assert "31,000" in e.value.detail
    assert wallet.charges == []


def test_tiktok_failed_extension_charges_nothing(monkeypatch):
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    adapter, wallet = _TikTokAdapter(fail_extend=True), _Wallet(balance=500000)
    _patch_tiktok(monkeypatch, adapter, wallet)
    with pytest.raises(HTTPException) as e:
        _run(extend_campaign("c1", ExtendBody(days=7, confirm=True),
                             db=_MultiCollDb(_tiktok_record()), brand_ctx={"brand_id": "b1"}))
    assert e.value.status_code == 502
    assert wallet.charges == []


def test_tiktok_extension_recorded_on_the_tiktok_collection(monkeypatch):
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    adapter, wallet = _TikTokAdapter(), _Wallet(balance=500000)
    _patch_tiktok(monkeypatch, adapter, wallet)
    db = _MultiCollDb(_tiktok_record())
    _run(extend_campaign("c1", ExtendBody(days=5, confirm=True),
                         db=db, brand_ctx={"brand_id": "b1"}))
    # Written to jane_ads_tiktok_campaigns, not jane_ads_meta_campaigns.
    assert db.tiktok.updates
    assert not db.meta.updates
    update = db.tiktok.updates[0]
    assert update["$push"]["extensions"]["days"] == 5


def test_tiktok_extend_refused_without_configured_credentials(monkeypatch):
    from app.agents.jane_ads.router import ExtendBody, extend_campaign

    adapter, wallet = _TikTokAdapter(), _Wallet(balance=500000)
    monkeypatch.setattr("app.agents.jane_ads.adapters.tiktok.TikTokAdsAdapter",
                        lambda *a, **k: adapter)
    monkeypatch.setattr("app.agents.jane_ads.wallet.WalletService", lambda *a, **k: wallet)
    monkeypatch.setattr("app.core.config.settings.TIKTOK_ADS_ADVERTISER_ID", "")
    with pytest.raises(HTTPException) as e:
        _run(extend_campaign("c1", ExtendBody(days=7, confirm=True),
                             db=_MultiCollDb(_tiktok_record()), brand_ctx={"brand_id": "b1"}))
    assert e.value.status_code == 400
    assert "not configured" in e.value.detail
