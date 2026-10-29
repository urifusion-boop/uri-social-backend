"""
The video endpoints with charging wired in: charge before rendering, refuse cleanly when the customer cannot
pay, never charge a repeated request twice, and always settle - even when the job crashes.
"""
import asyncio
import json

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.agents.social_media_manager.routers import complete_social_manager as R
from app.agents.social_media_manager.services import video_generation_billing as billing
from app.agents.social_media_manager.services import video_generation_service as vgs
from app.core.config import settings

TOKEN = {"claims": {"userId": "u1"}}
STORYBOARD = {"scenes": [{"scene_number": i, "duration_seconds": 5} for i in range(1, 4)]}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _request(**kw):
    return R.VideoFromStoryboardRequest(storyboard=kw.pop("storyboard", STORYBOARD), **kw)


def _data(response):
    return response["responseData"] if isinstance(response, dict) else response


@pytest.fixture
def jobs(monkeypatch):
    """Records what the endpoint asked the job service to do, without running anything."""
    seen = {"created": [], "settled": []}

    async def fake_create_job(storyboard, outcome, avatar_voice="Sarah", job_id=None, user_id="", billing=None):
        seen["created"].append({"job_id": job_id, "user_id": user_id, "billing": billing, "outcome": outcome})
        return job_id or "generated"

    async def fake_settle(job_id, job=None, **kw):
        seen["settled"].append((job_id, job))

    monkeypatch.setattr(vgs.VideoGenerationService, "create_job", staticmethod(fake_create_job))
    monkeypatch.setattr(billing, "settle", fake_settle)
    return seen


def _call(request=None, key=None):
    tasks = BackgroundTasks()
    out = _run(R.generate_video_from_storyboard(request or _request(), tasks, TOKEN, key))
    return out, tasks


def test_with_billing_off_the_endpoint_behaves_as_it_always_did(monkeypatch, jobs):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "off", raising=False)
    out, tasks = _call()
    data = _data(out)
    assert data["status"] == "queued" and data["total_scenes"] == 3 and "billing" not in data
    assert len(jobs["created"]) == 1 and jobs["created"][0]["user_id"] == "u1" and jobs["created"][0]["billing"] is None
    assert [t.func for t in tasks.tasks] == [vgs.VideoGenerationService.run_job_and_settle]


def test_a_charged_job_is_created_with_the_id_the_charge_was_recorded_under(monkeypatch, jobs):
    async def fake_start(user_id, storyboard, outcome, avatar_voice, key=None, **kw):
        return {"job_id": "JOB1", "existing": False,
                "billing": {"mode": "on", "credits_charged": 3, "credits_refunded": 0, "status": "charged"}}

    monkeypatch.setattr(billing, "start", fake_start)
    out, tasks = _call(key="click-1")
    data = _data(out)
    assert data["job_id"] == "JOB1" and data["billing"]["credits_charged"] == 3
    assert jobs["created"][0]["job_id"] == "JOB1" and jobs["created"][0]["billing"]["credits_charged"] == 3
    assert tasks.tasks[0].args[0] == "JOB1"


def test_a_customer_who_cannot_pay_gets_a_402_with_the_numbers_and_no_job(monkeypatch, jobs):
    async def fake_start(*a, **kw):
        raise billing.BillingDeclined("This video needs 3 credits, but you have 1. Top up to continue.",
                                      {"credits_required": 3, "credits_remaining": 1, "upgrade_url": "/pricing"})

    monkeypatch.setattr(billing, "start", fake_start)
    out, tasks = _call()
    assert out.status_code == 402
    body = json.loads(out.body)
    assert body["responseData"] == {"credits_required": 3, "credits_remaining": 1, "upgrade_url": "/pricing"}
    assert "top up" in body["responseMessage"].lower()
    assert jobs["created"] == [] and tasks.tasks == []


def test_a_video_that_cannot_be_priced_is_a_400_not_a_free_video(monkeypatch, jobs):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "on", raising=False)
    too_long = {"scenes": [{"duration_seconds": 5}] * 13}
    with pytest.raises(HTTPException) as err:
        _call(_request(storyboard=too_long))
    assert err.value.status_code == 400 and jobs["created"] == []


def test_a_repeated_request_returns_the_first_job_and_starts_nothing_new(monkeypatch, jobs):
    async def fake_start(*a, **kw):
        return {"job_id": "JOB1", "existing": True, "billing": {"mode": "on", "credits_charged": 3}}

    async def fake_get_job(job_id):
        return {"job_id": job_id, "status": "generating", "total_scenes": 3}

    monkeypatch.setattr(billing, "start", fake_start)
    monkeypatch.setattr(vgs, "get_job", fake_get_job)
    out, tasks = _call(key="click-1")
    assert _data(out)["job_id"] == "JOB1" and _data(out)["status"] == "generating"
    assert jobs["created"] == [] and tasks.tasks == []


def test_if_the_job_cannot_be_created_after_charging_the_credits_go_straight_back(monkeypatch, jobs):
    async def fake_start(*a, **kw):
        return {"job_id": "JOB1", "existing": False, "billing": {"mode": "on", "credits_charged": 3}}

    async def broken_create(*a, **kw):
        raise RuntimeError("database down")

    monkeypatch.setattr(billing, "start", fake_start)
    monkeypatch.setattr(vgs.VideoGenerationService, "create_job", staticmethod(broken_create))
    with pytest.raises(RuntimeError):
        _call()
    assert jobs["settled"] == [("JOB1", {"total_scenes": 3, "clips": []})]      # nothing delivered = full refund


# ── the runner ──────────────────────────────────────────────────────────────────

class _FakeJobs:
    def __init__(self):
        self.updates = []

    async def update_one(self, flt, update, **kw):
        self.updates.append((flt, update))


def test_the_job_is_settled_after_it_finishes(monkeypatch, jobs):
    async def fine(job_id, storyboard, brand_images, outcome, avatar_voice="Sarah"):
        return None

    monkeypatch.setattr(vgs.VideoGenerationService, "run_job", staticmethod(fine))
    _run(vgs.VideoGenerationService.run_job_and_settle("JOB1", STORYBOARD, [], "quick_video"))
    assert [j for j, _ in jobs["settled"]] == ["JOB1"]


def test_a_job_that_crashes_is_marked_failed_and_still_settled_so_the_credits_come_back(monkeypatch, jobs):
    async def crash(*a, **kw):
        raise ValueError("provider exploded")

    fake = _FakeJobs()
    monkeypatch.setattr(vgs.VideoGenerationService, "run_job", staticmethod(crash))
    monkeypatch.setattr(vgs, "_jobs_collection", lambda: fake)
    _run(vgs.VideoGenerationService.run_job_and_settle("JOB1", STORYBOARD, [], "quick_video"))
    assert [j for j, _ in jobs["settled"]] == ["JOB1"]
    assert fake.updates[0][1]["$set"]["status"] == "failed" and "provider exploded" in fake.updates[0][1]["$set"]["error"]


def test_a_billing_error_while_settling_never_breaks_the_runner(monkeypatch, jobs):
    async def fine(*a, **kw):
        return None

    async def broken_settle(job_id, job=None, **kw):
        raise RuntimeError("billing database down")

    monkeypatch.setattr(vgs.VideoGenerationService, "run_job", staticmethod(fine))
    monkeypatch.setattr(billing, "settle", broken_settle)
    _run(vgs.VideoGenerationService.run_job_and_settle("JOB1", STORYBOARD, [], "quick_video"))   # must not raise


# ── quote and job status ────────────────────────────────────────────────────────

def test_the_quote_endpoint_says_billing_is_off_until_it_is_turned_on(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "off", raising=False)
    out = _run(R.video_quote(_request(), TOKEN))
    assert _data(out) == {"billing": "off"}


def test_the_quote_endpoint_returns_the_servers_price(monkeypatch):
    async def fake_quote(user_id, storyboard, outcome, avatar_voice=""):
        from app.agents.social_media_manager.services import video_pricing
        assert user_id == "u1"
        return {"billing": "on", **video_pricing.quote_for(storyboard, outcome, avatar_voice), "sufficient": True}

    monkeypatch.setattr(billing, "quote", fake_quote)
    data = _data(_run(R.video_quote(_request(outcome="high_quality"), TOKEN)))
    assert data["credits_required"] == 4 and data["video_type"] == "high_quality"


def test_the_quote_for_a_video_that_cannot_be_priced_is_a_400(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_GENERATION_BILLING_MODE", "on", raising=False)
    with pytest.raises(HTTPException) as err:
        _run(R.video_quote(_request(storyboard={"scenes": []}), TOKEN))
    assert err.value.status_code == 400


def test_the_job_status_shows_what_was_charged_and_never_the_owner(monkeypatch):
    async def fake_get_job(job_id):
        return {"job_id": job_id, "status": "complete", "user_id": "u1", "billing": {"internal": True}, "clips": []}

    async def fake_for_job(job_id):
        return {"mode": "on", "status": "settled", "credits_charged": 3, "credits_refunded": 1}

    monkeypatch.setattr(vgs, "get_job", fake_get_job)
    monkeypatch.setattr(billing, "for_job", fake_for_job)
    data = _data(_run(R.get_video_job("JOB1", TOKEN)))
    assert "user_id" not in data
    assert data["billing"] == {"mode": "on", "status": "settled", "credits_charged": 3, "credits_refunded": 1}


def test_an_uncharged_job_shows_no_billing_block(monkeypatch):
    async def fake_get_job(job_id):
        return {"job_id": job_id, "status": "complete", "user_id": "u1", "billing": None, "clips": []}

    async def none(job_id):
        return None

    monkeypatch.setattr(vgs, "get_job", fake_get_job)
    monkeypatch.setattr(billing, "for_job", none)
    data = _data(_run(R.get_video_job("JOB1", TOKEN)))
    assert "billing" not in data and "user_id" not in data
