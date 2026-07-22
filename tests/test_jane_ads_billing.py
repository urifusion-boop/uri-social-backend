"""
Unit tests for conversation-charge reconciliation (billing.py) — the loop that
recoups Meta ad spend from each customer's prepaid wallet.

Money-critical, so the invariants under test are: charge exactly the unbilled
conversations, advance the water mark ONLY by what was actually charged (never skip
an unbilled conversation when a wallet runs dry), pause + notify once when it does,
and never touch anonymous/ownerless campaigns.

The real wallet (InMemoryWalletStore) is used so charges/balances are real; Meta and
the notification service are faked; MongoWalletStore is swapped for the funded
in-memory store so billing.py's own `WalletService(MongoWalletStore(db))` picks it up.
"""
import asyncio

import pytest

from app.agents.jane_ads import billing
from app.agents.jane_ads.store import InMemoryWalletStore
from app.agents.jane_ads.wallet import WalletService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, length=None):
        return list(self._rows)


class _FakeCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, _filter=None, _proj=None):
        # billing reads a snapshot up front; return copies so its own updates during
        # the sweep don't mutate what it's iterating.
        return _FakeCursor([dict(r) for r in self.rows])

    def _match(self, r, f):
        return all(r.get(k) == v for k, v in f.items())

    async def update_one(self, f, update):
        for r in self.rows:
            if self._match(r, f):
                r.update(update.get("$set", {}))
                return

    async def delete_one(self, f):
        self.rows = [r for r in self.rows if not self._match(r, f)]


class _FakeDB:
    def __init__(self, rows):
        self._coll = _FakeCollection(rows)

    def __getitem__(self, name):
        assert name == billing.COLLECTION
        return self._coll


class _FakeAdapter:
    def __init__(self, summaries):
        self.summaries = summaries          # campaign_id -> summary dict
        self.paused = []                    # campaign_ids set inactive

    def __call__(self, db, access_token=None):  # stand in for the class constructor
        return self

    async def fetch_campaign_summary(self, campaign_id):
        return self.summaries[campaign_id]

    async def set_delivery(self, campaign_id, active):
        if not active:
            self.paused.append(campaign_id)
        return {"status": "PAUSED" if not active else "ACTIVE"}


class _FakeNotifier:
    def __init__(self):
        self.calls = []

    async def _log_notification(self, **kw):
        self.calls.append(kw)


def _summary(conversations, *, delivery="Active", spend=0.0, cost_per=100.0):
    return {
        "delivery": delivery, "spend_ngn": spend, "impressions": 0, "reach": 0,
        "conversations": conversations, "cost_per_conversation_ngn": cost_per,
        "ends_at": None,
    }


def _setup(monkeypatch, rows, summaries, funded_store):
    adapter = _FakeAdapter(summaries)
    notifier = _FakeNotifier()
    monkeypatch.setattr("app.agents.jane_ads.adapters.meta.MetaAdPlatformAdapter", adapter)
    monkeypatch.setattr("app.agents.jane_ads.store.MongoWalletStore", lambda db: funded_store)
    monkeypatch.setattr("app.services.NotificationService.notification_service", notifier)
    from app.core.config import settings
    monkeypatch.setattr(settings, "META_AD_ACCOUNT_ID", "act_test", raising=False)
    monkeypatch.setattr(settings, "META_ADS_ACCESS_TOKEN", "tok_test", raising=False)
    return _FakeDB(rows), adapter, notifier


def test_charges_each_new_conversation_and_advances_watermark(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("brnd_1", 100_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "conversations_billed": 0, "display_name": "Shop"}]
    db, adapter, notifier = _setup(monkeypatch, rows, {"c1": _summary(3)}, store)

    res = _run(billing.reconcile_conversation_charges(db))

    assert res == {"checked": 1, "charged": 3, "paused": 0}
    # cost_per=100 → 100×1.5=150 < ₦400 floor, so each charge is the ₦400 floor.
    assert _run(WalletService(store).get_balance("brnd_1")) == 100_000 - 3 * 400
    assert db._coll.rows[0]["conversations_billed"] == 3
    assert adapter.paused == []
    assert notifier.calls == []


def test_pauses_and_notifies_once_when_wallet_runs_dry(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("brnd_1", 5_000, reference="seed"))  # covers 12 @ ₦400
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "conversations_billed": 0, "display_name": "Shop"}]
    db, adapter, notifier = _setup(monkeypatch, rows, {"c1": _summary(20)}, store)

    res = _run(billing.reconcile_conversation_charges(db))

    charged = res["charged"]
    assert charged == 12               # 5000 / 400
    assert res["paused"] == 1
    # Water mark advances ONLY by what was charged — the other 8 stay billable.
    assert db._coll.rows[0]["conversations_billed"] == 12
    assert db._coll.rows[0]["paused_for_funds"] is True
    assert adapter.paused == ["c1"]
    assert len(notifier.calls) == 1

    # A second sweep with the same (still-empty) wallet must NOT re-notify or advance.
    res2 = _run(billing.reconcile_conversation_charges(db))
    assert res2["charged"] == 0
    assert len(notifier.calls) == 1
    assert db._coll.rows[0]["conversations_billed"] == 12


def test_unbilled_remainder_is_retried_after_topup(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("brnd_1", 5_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "conversations_billed": 0}]
    db, adapter, notifier = _setup(monkeypatch, rows, {"c1": _summary(20)}, store)
    _run(billing.reconcile_conversation_charges(db))          # charges 12, 8 left

    _run(WalletService(store).top_up("brnd_1", 5_000, reference="refill"))
    res = _run(billing.reconcile_conversation_charges(db))    # the remaining 8
    assert res["charged"] == 8
    assert db._coll.rows[0]["conversations_billed"] == 20


def test_idempotent_when_nothing_new(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("brnd_1", 100_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "conversations_billed": 3}]
    db, _, notifier = _setup(monkeypatch, rows, {"c1": _summary(3)}, store)

    res = _run(billing.reconcile_conversation_charges(db))
    assert res["charged"] == 0
    assert _run(WalletService(store).get_balance("brnd_1")) == 100_000
    assert notifier.calls == []


def test_skips_ownerless_or_anonymous_campaigns(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("oneshot_x", 100_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "oneshot_x", "user_id": None,
             "ad_id": "a1", "conversations_billed": 0}]
    db, _, _ = _setup(monkeypatch, rows, {"c1": _summary(5)}, store)

    res = _run(billing.reconcile_conversation_charges(db))
    assert res["checked"] == 0 and res["charged"] == 0
    assert _run(WalletService(store).get_balance("oneshot_x")) == 100_000


def test_deleted_campaign_record_removed(monkeypatch):
    store = InMemoryWalletStore()
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "conversations_billed": 0}]
    db, _, _ = _setup(monkeypatch, rows, {"c1": _summary(0, delivery="Deleted")}, store)

    _run(billing.reconcile_conversation_charges(db))
    assert db._coll.rows == []
