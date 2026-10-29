"""
Every storyboard run costs one credit: taken before any writing starts, given back in full if no storyboard
comes out, never taken twice for the same click, and never charged when video billing is off.
"""
import asyncio

import pytest
from fastapi import BackgroundTasks

from app.agents.social_media_manager.routers import complete_social_manager as R
from app.agents.social_media_manager.services import video_creative_service as vcs
from app.agents.social_media_manager.services import video_generation_billing as B
from app.agents.social_media_manager.services import video_pricing
from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
from app.core.config import settings
from tests.test_video_generation_billing import FakeCol, FakeTrial, FakeWallet, on  # noqa: F401  (on = fixture)

CTX = {"user_id": "u1", "brand_id": "b1", "agency_id": None}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _start(wallet, trial=None, col=None, key=None):
    col = col if col is not None else FakeCol()
    return _run(B.start_storyboard("u1", key, credits=wallet, trials=trial or FakeTrial(), col=col)), col


def _settle(job_id, ok, wallet, trial=None, col=None):
    return _run(B.settle_storyboard(job_id, ok, credits=wallet, trials=trial or FakeTrial(), col=col))


def test_a_storyboard_costs_one_credit():
    assert video_pricing.STORYBOARD_CREDITS == 1


# ── charging ──────────────────────────────────────────────────────────────────

def test_off_charges_nothing_and_records_nothing(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "off", raising=False)
    wallet, col = FakeWallet(5), FakeCol()
    out = _run(B.start_storyboard("u1", "k", credits=wallet, trials=FakeTrial(), col=col))
    assert out["billing"] is None and out["existing"] is False and out["job_id"]
    assert wallet.deductions == [] and col.docs == [] and wallet.credits == 5


def test_on_takes_one_credit_before_any_work(on):
    wallet = FakeWallet(5)
    out, col = _start(wallet)
    assert wallet.credits == 4
    assert wallet.deductions == [("u1", out["job_id"], "video_generation", 1)]
    assert out["billing"] == {"mode": "on", "credits_charged": 1, "credits_refunded": 0, "status": "charged"}
    record = col.one(out["job_id"])
    assert record["kind"] == "storyboard" and record["credits_charged"] == 1 and record["status"] == "charged"


def test_a_trial_user_pays_from_the_trial_balance_only(on):
    wallet, trial = FakeWallet(50), FakeTrial(3)
    out, col = _start(wallet, trial)
    assert trial.credits == 2 and wallet.deductions == [] and wallet.credits == 50
    assert col.one(out["job_id"])["is_trial"] is True


def test_not_enough_credits_is_declined_in_plain_words(on):
    wallet = FakeWallet(0)
    col = FakeCol()
    with pytest.raises(B.BillingDeclined) as e:
        _start(wallet, col=col)
    assert e.value.message == "This storyboard needs 1 credit, but you have 0. Top up to continue."
    assert e.value.details["credits_required"] == 1 and e.value.details["upgrade_url"]
    assert wallet.credits == 0
    assert col.docs[0]["status"] == "declined" and "idempotency_key" not in col.docs[0]


def test_a_double_click_is_charged_once(on):
    wallet, col = FakeWallet(5), FakeCol()
    first, _ = _start(wallet, col=col, key="click-1")
    again, _ = _start(wallet, col=col, key="click-1")
    assert again["existing"] is True and again["job_id"] == first["job_id"]
    assert wallet.credits == 4 and len(wallet.deductions) == 1


def test_a_storyboard_key_cannot_collide_with_a_video_key(on):
    wallet, col = FakeWallet(10), FakeCol()
    sb, _ = _start(wallet, col=col, key="same")
    video = _run(B.start("u1", {"scenes": [{"duration_seconds": 5}] * 3}, "quick_video", "Sarah", "same",
                         credits=wallet, trials=FakeTrial(), col=col))
    assert video["existing"] is False and video["job_id"] != sb["job_id"]
    assert len(wallet.deductions) == 2


def test_a_new_click_is_a_new_charge(on):
    wallet, col = FakeWallet(5), FakeCol()
    _start(wallet, col=col, key="a")
    _start(wallet, col=col, key="b")
    assert wallet.credits == 3


def test_shadow_records_the_price_and_charges_nothing(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "shadow", raising=False)
    monkeypatch.setattr(B, "_index_ready", False)
    wallet = FakeWallet(5)
    out, col = _start(wallet)
    assert wallet.deductions == [] and wallet.credits == 5
    assert col.one(out["job_id"])["would_charge"] == 1 and out["billing"]["mode"] == "shadow"


def test_an_account_with_no_wallet_is_unmetered_and_nothing_is_refunded_to_it(on):
    wallet = FakeWallet(None)
    out, col = _start(wallet)
    assert col.one(out["job_id"])["credits_charged"] == 0
    _settle(out["job_id"], False, wallet, col=col)
    assert wallet.refunds == []


# ── settling ──────────────────────────────────────────────────────────────────

def test_a_storyboard_that_came_out_keeps_its_credit(on):
    wallet = FakeWallet(5)
    out, col = _start(wallet)
    final = _settle(out["job_id"], True, wallet, col=col)
    assert wallet.refunds == [] and wallet.credits == 4
    assert final["credits_charged"] == 1 and final["credits_refunded"] == 0
    assert col.one(out["job_id"])["status"] == "settled"


def test_a_storyboard_that_failed_gets_its_credit_back(on):
    wallet = FakeWallet(5)
    out, col = _start(wallet)
    final = _settle(out["job_id"], False, wallet, col=col)
    assert wallet.credits == 5 and wallet.refunds == [("u1", out["job_id"], "refund", 1)]
    assert final["credits_refunded"] == 1


def test_a_failed_trial_storyboard_goes_back_to_the_trial_balance(on):
    wallet, trial = FakeWallet(50), FakeTrial(3)
    out, col = _start(wallet, trial)
    _settle(out["job_id"], False, wallet, trial, col)
    assert trial.credits == 3 and len(trial.refunds) == 1 and wallet.refunds == []


def test_settling_twice_never_refunds_twice(on):
    wallet = FakeWallet(5)
    out, col = _start(wallet)
    _settle(out["job_id"], False, wallet, col=col)
    _settle(out["job_id"], False, wallet, col=col)
    assert len(wallet.refunds) == 1 and wallet.credits == 5


def test_a_refund_that_cannot_be_paid_is_left_marked_not_lost(on):
    wallet = FakeWallet(5, fail_refund=True)
    out, col = _start(wallet)
    _settle(out["job_id"], False, wallet, col=col)
    record = col.one(out["job_id"])
    assert record["status"] == "refund_failed" and record["refund_due"] == 1


def test_settling_a_run_that_was_never_charged_does_nothing(monkeypatch):
    wallet, col = FakeWallet(5), FakeCol()
    assert _settle("nobody", False, wallet, col=col) is None
    assert wallet.refunds == []


# ── the quote ─────────────────────────────────────────────────────────────────

def test_the_quote_shows_price_balance_and_whether_they_can_afford_it(on):
    out = _run(B.quote_storyboard("u1", credits=FakeWallet(5), trials=FakeTrial()))
    assert out["credits_required"] == 1 and out["balance"] == 5 and out["balance_after"] == 4 and out["sufficient"] is True
    broke = _run(B.quote_storyboard("u1", credits=FakeWallet(0), trials=FakeTrial()))
    assert broke["sufficient"] is False and broke["balance_after"] == 0


def test_the_quote_says_off_when_billing_is_off(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "off", raising=False)
    assert _run(B.quote_storyboard("u1", credits=FakeWallet(5), trials=FakeTrial())) == {"billing": "off"}


# ── the job that does the writing settles it ──────────────────────────────────

class FakeJobs:
    def __init__(self, log):
        self.log = log

    async def update_one(self, flt, update, **kw):
        self.log.append(("job", update["$set"]["status"]))


@pytest.fixture
def job_run(monkeypatch):
    log = []
    monkeypatch.setattr(vcs, "_creative_storyboard_jobs_collection", lambda: FakeJobs(log))

    async def fake_settle(job_id, succeeded, **kw):
        log.append(("settle", job_id, succeeded))

    monkeypatch.setattr(B, "settle_storyboard", fake_settle)
    return log


def _run_job(monkeypatch, result=None, boom=None):
    async def fake_generate(**kw):
        if boom:
            raise boom
        return result

    monkeypatch.setattr(vcs.VideoCreativeService, "generate_creative_storyboard", staticmethod(fake_generate))
    _run(vcs.VideoCreativeService.run_storyboard_job("j1", "a brief long enough", [], {}))


def test_a_finished_storyboard_is_settled_as_kept_and_before_the_job_is_marked_complete(monkeypatch, job_run):
    _run_job(monkeypatch, {"status": True, "storyboard": {"scenes": []}})
    assert job_run == [("settle", "j1", True), ("job", "complete")]


def test_a_failed_storyboard_is_settled_as_failed_before_the_job_says_failed(monkeypatch, job_run):
    _run_job(monkeypatch, {"status": False, "error": "model said no"})
    assert job_run == [("settle", "j1", False), ("job", "failed")]


def test_a_crash_while_writing_still_refunds_and_ends_the_job(monkeypatch, job_run):
    _run_job(monkeypatch, boom=RuntimeError("boom"))
    assert job_run == [("settle", "j1", False), ("job", "failed")]


def test_a_settling_problem_never_hides_the_storyboard(monkeypatch, job_run):
    async def broken(job_id, succeeded, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(B, "settle_storyboard", broken)
    _run_job(monkeypatch, {"status": True, "storyboard": {"scenes": []}})
    assert job_run == [("job", "complete")]


# ── the endpoints ─────────────────────────────────────────────────────────────

@pytest.fixture
def endpoint(monkeypatch):
    seen = {"created": [], "ran": [], "settled": [], "started": []}

    async def fake_get(user_id, db, brand_id=None):
        return {"status": True, "responseData": {"brand_name": "Uri"}}

    async def fake_create(job_id=None):
        seen["created"].append(job_id)
        return job_id

    async def fake_run(*a):
        seen["ran"].append(a)

    async def fake_settle(job_id, succeeded, **kw):
        seen["settled"].append((job_id, succeeded))

    monkeypatch.setattr(BrandProfileService, "get", staticmethod(fake_get))
    monkeypatch.setattr(vcs.VideoCreativeService, "create_storyboard_job", staticmethod(fake_create))
    monkeypatch.setattr(vcs.VideoCreativeService, "run_storyboard_job", staticmethod(fake_run))
    monkeypatch.setattr(B, "settle_storyboard", fake_settle)
    seen["start"] = lambda result: monkeypatch.setattr(B, "start_storyboard", _recorder(seen, result))
    return seen


def _recorder(seen, result):
    async def fake_start(user_id, key=None, **kw):
        seen["started"].append((user_id, key))
        if isinstance(result, Exception):
            raise result
        return result
    return fake_start


def _call(key=None):
    tasks = BackgroundTasks()
    request = R.CreativeStoryboardRequest(brief="A fun ad for our lunch combo this week")
    return _run(R.generate_creative_storyboard(request, tasks, None, CTX, key)), tasks


def _data(out):
    return out["responseData"] if isinstance(out, dict) else out


CHARGED = {"mode": "on", "credits_charged": 1, "credits_refunded": 0, "status": "charged"}


def test_endpoint_charges_first_then_writes_under_the_charged_job_id(endpoint):
    endpoint["start"]({"job_id": "J", "billing": CHARGED, "existing": False})
    out, tasks = _call("click-1")
    assert endpoint["started"] == [("u1", "click-1")]
    assert endpoint["created"] == ["J"] and len(tasks.tasks) == 1
    assert _data(out) == {"job_id": "J", "status": "generating", "billing": CHARGED}


def test_endpoint_with_billing_off_answers_as_it_always_did(endpoint):
    endpoint["start"]({"job_id": "J", "billing": None, "existing": False})
    out, _ = _call()
    assert _data(out) == {"job_id": "J", "status": "generating"}


def test_endpoint_refuses_with_402_and_writes_nothing_when_they_cannot_pay(endpoint):
    endpoint["start"](B.BillingDeclined("This storyboard needs 1 credit, but you have 0. Top up to continue.",
                                        {"credits_required": 1, "credits_remaining": 0, "upgrade_url": "/pricing"}))
    out, tasks = _call()
    assert out.status_code == 402
    body = out.body.decode()
    assert "needs 1 credit" in body and "upgrade_url" in body
    assert endpoint["created"] == [] and tasks.tasks == []


def test_a_repeated_request_returns_the_first_job_and_starts_no_second_writer(monkeypatch, endpoint):
    async def first_job(job_id):
        return {"job_id": job_id, "status": "complete"}
    monkeypatch.setattr(vcs.VideoCreativeService, "get_storyboard_job", staticmethod(first_job))
    endpoint["start"]({"job_id": "J", "billing": CHARGED, "existing": True})
    out, tasks = _call("click-1")
    assert _data(out) == {"job_id": "J", "status": "complete", "billing": CHARGED}
    assert endpoint["created"] == [] and tasks.tasks == []


def test_if_the_job_cannot_be_created_the_credit_goes_straight_back(monkeypatch, endpoint):
    endpoint["start"]({"job_id": "J", "billing": CHARGED, "existing": False})

    async def boom(job_id=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(vcs.VideoCreativeService, "create_storyboard_job", staticmethod(boom))
    with pytest.raises(RuntimeError):
        _call()
    assert endpoint["settled"] == [("J", False)]


def test_polling_shows_what_was_charged_and_returned(monkeypatch):
    async def job(job_id):
        return {"job_id": job_id, "status": "failed", "storyboard": None, "error": "x"}

    async def for_job(job_id, **kw):
        return {"mode": "on", "status": "settled", "credits_charged": 1, "credits_refunded": 1}

    monkeypatch.setattr(vcs.VideoCreativeService, "get_storyboard_job", staticmethod(job))
    monkeypatch.setattr(B, "for_job", for_job)
    out = _run(R.get_creative_storyboard_job("J", {"claims": {"userId": "u1"}}))
    assert _data(out)["billing"]["credits_refunded"] == 1


def test_the_storyboard_quote_endpoint(monkeypatch):
    async def quoted(user_id, **kw):
        return {"billing": "on", "credits_required": 1, "balance": 4, "sufficient": True}
    monkeypatch.setattr(B, "quote_storyboard", quoted)
    out = _run(R.storyboard_quote({"claims": {"userId": "u1"}}))
    assert _data(out)["credits_required"] == 1
