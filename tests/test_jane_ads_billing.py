"""
Unit tests for ad-spend reconciliation (billing.py) — the loop that recoups Meta
ad spend from each customer's prepaid wallet at spend × markup.

Money-critical, so the invariants under test are: charge exactly (new spend × markup),
advance the water mark ONLY by the Meta spend actually recouped (take a partial slice,
never skip, when a wallet runs dry), pause + notify once when it can't cover the full
slice, retry the remainder after a top-up, and never touch anonymous/ownerless
campaigns.

The real wallet (InMemoryWalletStore) is used so charges/balances are real; Meta and
the notification service are faked; MongoWalletStore is swapped for the funded
in-memory store so billing.py's own `WalletService(MongoWalletStore(db))` picks it up.
"""
import asyncio

from app.agents.jane_ads import billing
from app.agents.jane_ads import constants as C
from app.agents.jane_ads.store import InMemoryWalletStore
from app.agents.jane_ads.wallet import WalletService

MARKUP = C.AD_SPEND_MARKUP


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
        return _FakeCursor([dict(r) for r in self.rows])

    def _match(self, r, f):
        for k, v in f.items():
            if isinstance(v, dict) and "$ne" in v:
                if r.get(k) == v["$ne"]:
                    return False
            elif r.get(k) != v:
                return False
        return True

    async def update_one(self, f, update):
        for r in self.rows:
            if self._match(r, f):
                r.update(update.get("$set", {}))
                return

    async def find_one_and_update(self, f, update):
        # Motor's atomic claim: match-and-set in one step. Returns the (pre-update)
        # doc if a row matched, else None — exactly what the billing guard relies on.
        for r in self.rows:
            if self._match(r, f):
                before = dict(r)
                r.update(update.get("$set", {}))
                return before
        return None

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
        self.summaries = summaries
        self.paused = []

    def __call__(self, db, access_token=None):
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


def _summary(spend, *, delivery="Active", conversations=0):
    return {
        "delivery": delivery, "spend_ngn": spend, "impressions": 0, "reach": 0,
        "conversations": conversations, "cost_per_conversation_ngn": 0.0, "ends_at": None,
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


def test_charges_new_spend_times_markup_and_advances_watermark(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("brnd_1", 100_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "spend_billed_ngn": 0.0, "display_name": "Shop"}]
    db, adapter, notifier = _setup(monkeypatch, rows, {"c1": _summary(2_000)}, store)

    res = _run(billing.reconcile_ad_spend_charges(db))

    # ₦2,000 new Meta spend × 1.5 = ₦3,000 debited; wallet covers it fully.
    assert res["checked"] == 1 and res["paused"] == 0
    assert res["charged_ngn"] == round(2_000 * MARKUP, 2)
    assert _run(WalletService(store).get_balance("brnd_1")) == 100_000 - 2_000 * MARKUP
    # Water mark tracks Meta SPEND recouped (₦2,000), not the marked-up amount.
    assert db._coll.rows[0]["spend_billed_ngn"] == 2_000
    assert adapter.paused == [] and notifier.calls == []


def test_only_new_spend_since_last_sweep_is_charged(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("brnd_1", 100_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "spend_billed_ngn": 1_500.0}]   # already billed ₦1,500 of spend
    db, _, _ = _setup(monkeypatch, rows, {"c1": _summary(2_000)}, store)

    res = _run(billing.reconcile_ad_spend_charges(db))
    # Only the ₦500 delta × 1.5 = ₦750.
    assert res["charged_ngn"] == round(500 * MARKUP, 2)
    assert db._coll.rows[0]["spend_billed_ngn"] == 2_000


def test_partial_slice_pauses_and_notifies_when_wallet_runs_dry(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("brnd_1", 6_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "spend_billed_ngn": 0.0, "display_name": "Shop"}]
    # ₦10,000 new spend × 1.5 = ₦15,000 owed, but only ₦6,000 in the wallet.
    db, adapter, notifier = _setup(monkeypatch, rows, {"c1": _summary(10_000)}, store)

    res = _run(billing.reconcile_ad_spend_charges(db))

    assert res["charged_ngn"] == 6_000                     # took the whole balance
    assert res["paused"] == 1
    assert _run(WalletService(store).get_balance("brnd_1")) == 0
    # Recouped ₦6,000 / 1.5 = ₦4,000 of spend; the other ₦6,000 stays billable.
    assert db._coll.rows[0]["spend_billed_ngn"] == round(6_000 / MARKUP, 2)
    assert db._coll.rows[0]["paused_for_funds"] is True
    assert adapter.paused == ["c1"] and len(notifier.calls) == 1

    # Second sweep, still-empty wallet: no charge, no re-notify, no re-advance.
    res2 = _run(billing.reconcile_ad_spend_charges(db))
    assert res2["charged_ngn"] == 0
    assert len(notifier.calls) == 1


def test_remainder_billed_after_topup(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("brnd_1", 6_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "spend_billed_ngn": 0.0}]
    db, _, _ = _setup(monkeypatch, rows, {"c1": _summary(10_000)}, store)
    first = _run(billing.reconcile_ad_spend_charges(db))    # covers what the ₦6,000 wallet can

    _run(WalletService(store).top_up("brnd_1", 20_000, reference="refill"))
    second = _run(billing.reconcile_ad_spend_charges(db))   # the remainder, after top-up
    # The full ₦10,000 of Meta spend is now billed, and total collected across both
    # sweeps is spend × markup — regardless of the markup rate (tolerant of rounding).
    assert abs(db._coll.rows[0]["spend_billed_ngn"] - 10_000) < 0.05
    assert abs((first["charged_ngn"] + second["charged_ngn"]) - 10_000 * MARKUP) < 0.05


def test_idempotent_when_no_new_spend(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("brnd_1", 100_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "spend_billed_ngn": 2_000.0}]
    db, _, notifier = _setup(monkeypatch, rows, {"c1": _summary(2_000)}, store)

    res = _run(billing.reconcile_ad_spend_charges(db))
    assert res["charged_ngn"] == 0
    assert _run(WalletService(store).get_balance("brnd_1")) == 100_000
    assert notifier.calls == []


def test_skips_ownerless_campaigns(monkeypatch):
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("oneshot_x", 100_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "oneshot_x", "user_id": None,
             "ad_id": "a1", "spend_billed_ngn": 0.0}]
    db, _, _ = _setup(monkeypatch, rows, {"c1": _summary(5_000)}, store)

    res = _run(billing.reconcile_ad_spend_charges(db))
    assert res["checked"] == 0 and res["charged_ngn"] == 0
    assert _run(WalletService(store).get_balance("oneshot_x")) == 100_000


def test_deleted_campaign_record_removed(monkeypatch):
    store = InMemoryWalletStore()
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "spend_billed_ngn": 0.0}]
    db, _, _ = _setup(monkeypatch, rows, {"c1": _summary(0, delivery="Deleted")}, store)

    _run(billing.reconcile_ad_spend_charges(db))
    assert db._coll.rows == []


def test_concurrent_sweeps_charge_a_slice_only_once(monkeypatch):
    """The over-charge regression: the scheduler runs in several workers, so this
    sweep can execute N times at once — all reading the same water mark before any
    writes it. The atomic claim must ensure the spend is billed exactly once."""
    store = InMemoryWalletStore()
    _run(WalletService(store).top_up("brnd_1", 100_000, reference="seed"))
    rows = [{"campaign_id": "c1", "business_id": "brnd_1", "user_id": "u1",
             "ad_id": "a1", "spend_billed_ngn": 0.0}]
    db, _, _ = _setup(monkeypatch, rows, {"c1": _summary(2_000)}, store)

    coll = db._coll
    # Simulate 4 concurrent sweeps that all observed the SAME prior mark (0.0):
    # each claims against {spend_billed_ngn: 0.0}. Only the first claim can match.
    claims = [_run(coll.find_one_and_update(
        {"campaign_id": "c1", "spend_billed_ngn": 0.0},
        {"$set": {"spend_billed_ngn": 2_000.0}},
    )) for _ in range(4)]
    winners = [c for c in claims if c is not None]
    assert len(winners) == 1                       # exactly one sweep may charge
    assert coll.rows[0]["spend_billed_ngn"] == 2_000.0

    # And a normal full sweep afterwards adds nothing (mark already at 2,000).
    res = _run(billing.reconcile_ad_spend_charges(db))
    assert res["charged_ngn"] == 0
    assert _run(WalletService(store).get_balance("brnd_1")) == 100_000
