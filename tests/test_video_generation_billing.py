"""
Charging for video generation: charge once before work starts, give back what was not delivered, never twice.

Uses small in-memory stand-ins for the wallets and the billing collection, so every path (not enough
credits, a trial user, a double click, a crashed job, a refund that fails) runs without a database.
"""
import asyncio
from types import SimpleNamespace

import pytest
from pymongo.errors import DuplicateKeyError

from app.agents.social_media_manager.services import video_generation_billing as B
from app.core.config import settings
from app.domain.models.billing_models import CreditTransaction

SCENES3 = {"scenes": [{"duration_seconds": 5}] * 3}     # a 15-second video


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCol:
    """Just enough of a Mongo collection: unique (user_id, idempotency_key), find/insert/update."""
    def __init__(self):
        self.docs = []

    async def create_index(self, *a, **kw):
        return None

    async def insert_one(self, doc):
        key = doc.get("idempotency_key")
        if key and any(d.get("user_id") == doc["user_id"] and d.get("idempotency_key") == key for d in self.docs):
            raise DuplicateKeyError("duplicate")
        self.docs.append(dict(doc))

    def _match(self, d, flt):
        return all(d.get(k) == v for k, v in flt.items())

    async def find_one(self, flt, *a, **kw):
        # What the database had at the moment of the query is what the caller gets, even though other work
        # runs while the answer is on its way back: that gap is what makes concurrent callers see stale data.
        snapshot = next((dict(d) for d in self.docs if self._match(d, flt)), None)
        await asyncio.sleep(0)
        return snapshot

    def _apply(self, d, update):
        d.update(update.get("$set", {}))
        for k in update.get("$unset", {}):
            d.pop(k, None)

    async def update_one(self, flt, update, **kw):
        for d in self.docs:
            if self._match(d, flt):
                self._apply(d, update)
                return

    async def find_one_and_update(self, flt, update, **kw):
        for d in self.docs:
            if self._match(d, flt):
                before = dict(d)
                self._apply(d, update)
                return before
        return None

    def one(self, job_id):
        return next(d for d in self.docs if d["job_id"] == job_id)


class FakeWallet:
    def __init__(self, credits=None, fail_refund=False):
        self.credits = credits           # None = a legacy account with no wallet
        self.fail_refund = fail_refund
        self.deductions, self.refunds = [], []

    async def get_user_wallet(self, user_id):
        return None if self.credits is None else SimpleNamespace(credits_remaining=self.credits)

    async def deduct_credit(self, user_id, campaign_id, reason="x", retry_count=0, amount=1):
        self.deductions.append((user_id, campaign_id, reason, amount))
        if self.credits is None:
            return True                   # the real method lets a wallet-less account through untouched
        if self.credits < amount:
            return False
        self.credits -= amount
        return True

    async def refund_credit(self, user_id, campaign_id, reason="refund", amount=1):
        if self.fail_refund:
            raise RuntimeError("database said no")
        self.refunds.append((user_id, campaign_id, reason, amount))
        self.credits = (self.credits or 0) + amount
        return True


class FakeTrial:
    def __init__(self, credits=None):
        self.credits = credits           # None = not on a trial
        self.deductions, self.refunds = [], []

    async def has_active_trial(self, user_id):
        return self.credits is not None

    async def get_trial_status(self, user_id):
        return SimpleNamespace(credits_remaining=self.credits)

    async def deduct_trial_credit(self, user_id, campaign_id, reason="x", amount=1):
        self.deductions.append((user_id, campaign_id, reason, amount))
        if self.credits < amount:
            return False
        self.credits -= amount
        return True

    async def refund_trial_credit(self, user_id, campaign_id, reason="refund", amount=1):
        self.refunds.append((user_id, campaign_id, reason, amount))
        self.credits += amount
        return True


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "on", raising=False)
    monkeypatch.setattr(B, "_index_ready", False)


def _start(wallet, trial=None, col=None, key=None, storyboard=SCENES3, outcome="quick_video", voice="Sarah"):
    col = col if col is not None else FakeCol()
    return _run(B.start("u1", storyboard, outcome, voice, key,
                        credits=wallet, trials=trial or FakeTrial(), col=col)), col


def _job(delivered, total=3, fallbacks=0):
    clips = [{"video_url": f"https://v/{i}.mp4", "cost_usd": 0.1, "fallback_used": i < fallbacks} for i in range(delivered)]
    clips += [{"video_url": None, "cost_usd": None} for _ in range(total - delivered)]
    return {"total_scenes": total, "clips": clips}


def _settle(job_id, job, wallet, trial=None, col=None):
    return _run(B.settle(job_id, job=job, credits=wallet, trials=trial or FakeTrial(), col=col))


# ── off and shadow: nothing is charged ──────────────────────────────────────────

def test_with_billing_off_nothing_is_charged_or_recorded(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "off", raising=False)
    wallet = FakeWallet(10)
    started, col = _start(wallet)
    assert started["billing"] is None and started["existing"] is False
    assert wallet.deductions == [] and col.docs == []
    assert _run(B.quote("u1", SCENES3, "quick_video", credits=wallet, trials=FakeTrial())) == {"billing": "off"}


def test_an_unknown_mode_value_means_off(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "ON please", raising=False)
    assert B.mode() == "off"


def test_shadow_mode_records_the_price_but_charges_nobody(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "shadow", raising=False)
    monkeypatch.setattr(B, "_index_ready", False)
    wallet = FakeWallet(0)               # could not afford it, and that must not matter in shadow mode
    started, col = _start(wallet)
    assert wallet.deductions == []
    record = col.one(started["job_id"])
    assert record["status"] == "shadow" and record["would_charge"] == 3
    # and settling it records what would have come back, without touching the wallet
    _settle(started["job_id"], _job(delivered=1), wallet, col=col)
    assert wallet.refunds == []
    assert col.one(started["job_id"])["status"] == "shadow_settled"
    assert col.one(started["job_id"])["would_refund"] == 2


# ── charging ────────────────────────────────────────────────────────────────────

def test_the_quoted_credits_are_taken_once_before_the_job_starts(on):
    wallet = FakeWallet(10)
    started, col = _start(wallet)
    assert wallet.credits == 7
    assert wallet.deductions == [("u1", started["job_id"], "video_generation", 3)]
    record = col.one(started["job_id"])
    assert (record["status"], record["credits_charged"], record["video_type"], record["seconds"]) == ("charged", 3, "quick_video", 15)
    assert started["billing"] == {"mode": "on", "credits_charged": 3, "credits_refunded": 0, "status": "charged"}


def test_not_enough_credits_declines_with_the_numbers_and_starts_nothing(on):
    wallet = FakeWallet(2)
    with pytest.raises(B.BillingDeclined) as err:
        _start(wallet)
    assert err.value.details == {"credits_required": 3, "credits_remaining": 2, "upgrade_url": "/pricing"}
    assert "3 credits" in err.value.message and wallet.credits == 2


def test_a_declined_attempt_can_be_retried_with_the_same_key_after_topping_up(on):
    wallet, col = FakeWallet(1), FakeCol()
    with pytest.raises(B.BillingDeclined):
        _start(wallet, col=col, key="click-1")
    wallet.credits = 10
    started, _ = _start(wallet, col=col, key="click-1")
    assert started["existing"] is False and wallet.credits == 7


def test_a_trial_user_pays_from_the_trial_balance_only(on):
    wallet, trial = FakeWallet(100), FakeTrial(5)
    started, col = _start(wallet, trial)
    assert trial.credits == 2 and wallet.credits == 100 and wallet.deductions == []
    assert col.one(started["job_id"])["is_trial"] is True


def test_a_trial_user_without_enough_trial_credits_is_declined_not_moved_to_the_paid_wallet(on):
    wallet, trial = FakeWallet(100), FakeTrial(1)
    with pytest.raises(B.BillingDeclined):
        _start(wallet, trial)
    assert wallet.credits == 100 and wallet.deductions == []


def test_an_older_account_with_no_wallet_is_unmetered_and_never_refunded(on):
    wallet = FakeWallet(None)
    started, col = _start(wallet)
    assert col.one(started["job_id"])["credits_charged"] == 0 and wallet.deductions == []
    _settle(started["job_id"], _job(delivered=0), wallet, col=col)
    assert wallet.refunds == [] and col.one(started["job_id"])["status"] == "settled"


def test_the_same_request_twice_returns_the_first_job_and_charges_once(on):
    wallet, col = FakeWallet(10), FakeCol()
    first, _ = _start(wallet, col=col, key="click-1")
    second, _ = _start(wallet, col=col, key="click-1")
    assert second["existing"] is True and second["job_id"] == first["job_id"]
    assert wallet.credits == 7 and len(wallet.deductions) == 1


def test_different_keys_are_different_videos(on):
    wallet, col = FakeWallet(10), FakeCol()
    _start(wallet, col=col, key="a")
    _start(wallet, col=col, key="b")
    assert wallet.credits == 4


def test_a_tampered_storyboard_is_charged_for_what_will_be_rendered(on):
    wallet = FakeWallet(100)
    started, col = _start(wallet, storyboard={"scenes": [{"duration_seconds": 1}, {"duration_seconds": 99999}]})
    assert col.one(started["job_id"])["seconds"] == 16


def test_talking_videos_are_priced_by_voice(on):
    nigerian, standard = FakeWallet(100), FakeWallet(100)
    _start(nigerian, outcome="talking_dialogue", voice="Femi (Nigerian)")
    _start(standard, outcome="talking_dialogue", voice="Sarah")
    assert nigerian.deductions[0][3] == 3 and standard.deductions[0][3] == 9


# ── quote ───────────────────────────────────────────────────────────────────────

def test_the_quote_shows_price_balance_and_whether_it_is_affordable(on):
    q = _run(B.quote("u1", SCENES3, "quick_video", credits=FakeWallet(10), trials=FakeTrial()))
    assert (q["credits_required"], q["balance"], q["balance_after"], q["sufficient"]) == (3, 10, 7, True)
    assert q["billing"] == "on" and not any("naira" in k for k in q)  # credits only: no naira value shown
    short = _run(B.quote("u1", SCENES3, "quick_video", credits=FakeWallet(2), trials=FakeTrial()))
    assert short["sufficient"] is False and short["balance_after"] == 0


def test_the_quote_for_an_unmetered_account_is_always_affordable(on):
    q = _run(B.quote("u1", SCENES3, "quick_video", credits=FakeWallet(None), trials=FakeTrial()))
    assert q["sufficient"] is True and q["balance"] is None


# ── settling ────────────────────────────────────────────────────────────────────

def test_a_fully_delivered_video_keeps_the_charge(on):
    wallet = FakeWallet(10)
    started, col = _start(wallet)
    public = _settle(started["job_id"], _job(delivered=3), wallet, col=col)
    assert wallet.refunds == [] and public["credits_charged"] == 3 and public["credits_refunded"] == 0
    assert col.one(started["job_id"])["status"] == "settled"


def test_scenes_that_did_not_come_out_are_refunded_in_proportion(on):
    wallet = FakeWallet(10)
    started, col = _start(wallet)
    public = _settle(started["job_id"], _job(delivered=2), wallet, col=col)
    assert wallet.refunds == [("u1", started["job_id"], "refund", 1)]
    assert wallet.credits == 8 and public["credits_refunded"] == 1


def test_a_job_that_delivered_nothing_is_refunded_in_full(on):
    wallet = FakeWallet(10)
    started, col = _start(wallet)
    _settle(started["job_id"], _job(delivered=0), wallet, col=col)
    assert wallet.credits == 10


def test_settling_twice_refunds_once(on):
    wallet = FakeWallet(10)
    started, col = _start(wallet)
    _settle(started["job_id"], _job(delivered=0), wallet, col=col)
    _settle(started["job_id"], _job(delivered=0), wallet, col=col)
    assert wallet.credits == 10 and len(wallet.refunds) == 1


def test_two_settlements_running_at_the_same_moment_refund_once(on):
    # Both read the job as "charged" before either has recorded anything; only one may win the right to refund.
    wallet = FakeWallet(10)
    started, col = _start(wallet)

    async def both():
        return await asyncio.gather(
            B.settle(started["job_id"], job=_job(delivered=0), credits=wallet, trials=FakeTrial(), col=col),
            B.settle(started["job_id"], job=_job(delivered=0), credits=wallet, trials=FakeTrial(), col=col),
        )

    _run(both())
    assert wallet.credits == 10 and len(wallet.refunds) == 1


def test_a_fallback_that_worked_is_not_a_second_charge_and_not_a_refund(on):
    wallet = FakeWallet(10)
    started, col = _start(wallet)
    _settle(started["job_id"], _job(delivered=3, fallbacks=2), wallet, col=col)
    assert wallet.credits == 7 and wallet.refunds == []
    assert col.one(started["job_id"])["fallbacks_used"] == 2


def test_the_provider_cost_of_the_job_is_recorded_for_reconciliation(on):
    wallet = FakeWallet(10)
    started, col = _start(wallet)
    _settle(started["job_id"], _job(delivered=3), wallet, col=col)
    assert col.one(started["job_id"])["provider_cost_usd"] == pytest.approx(0.3)


def test_a_trial_refund_goes_back_to_the_trial_balance(on):
    wallet, trial = FakeWallet(100), FakeTrial(5)
    started, col = _start(wallet, trial)
    _settle(started["job_id"], _job(delivered=0), wallet, trial, col=col)
    assert trial.credits == 5 and wallet.refunds == []


def test_a_refund_that_cannot_be_paid_is_left_marked_for_follow_up_not_lost(on):
    wallet = FakeWallet(10, fail_refund=True)
    started, col = _start(wallet)
    _settle(started["job_id"], _job(delivered=0), wallet, col=col)
    record = col.one(started["job_id"])
    assert record["status"] == "refund_failed" and record["refund_due"] == 3


def test_a_job_whose_charge_was_declined_is_never_settled(on):
    wallet, col = FakeWallet(1), FakeCol()
    with pytest.raises(B.BillingDeclined):
        _start(wallet, col=col)
    job_id = col.docs[0]["job_id"]
    _settle(job_id, _job(delivered=0), wallet, col=col)
    assert wallet.refunds == []


def test_a_job_that_was_never_billed_settles_to_nothing(on):
    assert _settle("no-such-job", _job(delivered=0), FakeWallet(10), col=FakeCol()) is None


# ── what the customer sees ──────────────────────────────────────────────────────

def test_the_customer_sees_what_was_charged_and_returned_only_when_it_was_really_charged(on):
    wallet = FakeWallet(10)
    started, col = _start(wallet)
    _settle(started["job_id"], _job(delivered=2), wallet, col=col)
    shown = _run(B.for_job(started["job_id"], col=col))
    assert shown["credits_charged"] == 3 and shown["credits_refunded"] == 1 and shown["status"] == "settled"
    assert "user_id" not in shown and "would_charge" not in shown


def test_shadow_charges_are_not_shown_to_the_customer(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "shadow", raising=False)
    monkeypatch.setattr(B, "_index_ready", False)
    started, col = _start(FakeWallet(10))
    assert _run(B.for_job(started["job_id"], col=col)) is None


def test_the_deduction_reason_is_one_the_transaction_log_accepts():
    # The wallet is decremented first and logged second; a reason the log rejects turns a good charge into a 500.
    CreditTransaction(user_id="u", type="deduction", amount=-3, balance_before=10, balance_after=7,
                      reason=B.DEDUCT_REASON)
