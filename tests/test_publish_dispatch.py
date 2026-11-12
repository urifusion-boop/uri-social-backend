"""
Publishing through the per-account gate (services/publish_dispatch.py), as used by the immediate
Approve path and the scheduled-publish cron.

What must hold: one post at a time per account with a gap between; a busy, spaced-out or rate-limited
account DEFERS the draft (back to its scheduled status with `next_attempt_at`) instead of failing it;
a platform rate limit pauses the account and defers; a daily/app cap is NOT deferred or retried but
keeps its clear message; and none of this ever counts as a failure or sends anyone to "Reconnect".
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.social_media_manager.services import publish_confirmation as pc
from app.agents.social_media_manager.services import publish_dispatch as pd
from app.agents.social_media_manager.services import rate_limit as rl
from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService
from tests.test_account_gate import FakeGateCollection, _match as gate_match
from tests.test_publish_confirmation import _Coll, _DB


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


TIKTOK_CONN = {"id": "tt_1", "user_id": "u1", "platform": "tiktok", "connected_via": "tiktok_direct_oauth",
               "connection_status": "active"}
KEY = "tiktok:tt_1"


class GateDb:
    def __init__(self):
        self.gate = FakeGateCollection()

    def __getitem__(self, name):
        assert name == rl.GATE_COLLECTION
        return self.gate


def _limited(code="rate_limit_exceeded", text="TikTok direct publish failed: slow down"):
    info = rl.classify_tiktok(code)
    return {"success": False, "error": text, "rate_limit": info}


# ── dispatch ──────────────────────────────────────────────────────────────────

def test_an_account_we_cannot_identify_is_not_gated():
    publish = AsyncMock(return_value={"success": True, "post_id": "p"})
    out = _run(pd.dispatch(GateDb(), "tiktok", {"connected_via": "tiktok_direct_oauth"}, publish))
    assert out.result["success"] and not out.deferred and publish.await_count == 1


def test_platforms_we_do_not_gate_run_straight_through():
    publish = AsyncMock(return_value={"success": True})
    db = GateDb()
    out = _run(pd.dispatch(db, "linkedin", {"id": "li_1"}, publish))
    assert out.result["success"] and not db.gate.docs


def test_a_free_account_publishes_and_is_released_with_a_gap():
    db = GateDb()
    publish = AsyncMock(return_value={"success": True, "post_id": "p"})
    out = _run(pd.dispatch(db, "tiktok", TIKTOK_CONN, publish))
    assert out.result["post_id"] == "p" and not out.deferred
    gate = db.gate.docs[KEY]
    assert "lease_owner" not in gate and gate["next_allowed_at"] > rl.utcnow()
    assert gate["backoff_level"] == 0


def test_a_busy_account_defers_without_publishing():
    db = GateDb()
    _run(rl.acquire(db, KEY, "another-post", lease_s=300))
    publish = AsyncMock()
    out = _run(pd.dispatch(db, "tiktok", TIKTOK_CONN, publish, wait_max_s=0.0))
    assert out.deferred and out.result is None and out.resume_at > rl.utcnow()
    assert "one post at a time" in out.message and "UTC" not in out.message
    publish.assert_not_called()


def test_a_post_waits_a_moment_for_the_gap_and_then_goes(monkeypatch):
    monkeypatch.setenv("TIKTOK_MIN_GAP_S", "0.4")
    db = GateDb()
    publish = AsyncMock(return_value={"success": True})
    _run(pd.dispatch(db, "tiktok", TIKTOK_CONN, publish))
    out = _run(pd.dispatch(db, "tiktok", TIKTOK_CONN, publish, wait_max_s=5.0))
    assert not out.deferred and publish.await_count == 2


def test_a_paused_account_defers_until_it_resumes():
    db = GateDb()
    until = rl.utcnow() + timedelta(minutes=10)
    _run(rl.pause(db, KEY, until=until, code="17", reason="limit"))
    publish = AsyncMock()
    out = _run(pd.dispatch(db, "tiktok", TIKTOK_CONN, publish, wait_max_s=0.0))
    assert out.deferred and out.resume_at == until and out.info.code == "17"
    publish.assert_not_called()


def test_a_platform_rate_limit_pauses_the_account_and_defers_the_post():
    db = GateDb()
    publish = AsyncMock(return_value=_limited())
    out = _run(pd.dispatch(db, "tiktok", TIKTOK_CONN, publish))
    assert out.deferred and out.result["success"] is False and out.resume_at > rl.utcnow()
    gate = db.gate.docs[KEY]
    assert gate["paused_until"] > rl.utcnow() and "lease_owner" not in gate and gate["backoff_level"] == 1


def test_a_limit_reported_only_as_text_is_recognised_too():
    """Outstand and wrapped Meta errors arrive as strings, with no typed info attached."""
    db = GateDb()
    publish = AsyncMock(return_value={"success": False, "error": "429 Too Many Requests for url https://api.outstand.so"})
    out = _run(pd.dispatch(db, "tiktok", {"outstand_account_id": "7ylKW", "connected_via": "outstand"}, publish))
    assert out.deferred and "outstand:7ylKW" in db.gate.docs


def test_a_cap_is_not_deferred_and_does_not_pause_the_account():
    db = GateDb()
    publish = AsyncMock(return_value=_limited("spam_risk_too_many_posts"))
    out = _run(pd.dispatch(db, "tiktok", TIKTOK_CONN, publish))
    assert not out.deferred and out.info.kind == rl.LimitKind.DAILY_CAP and out.result["success"] is False
    assert "paused_until" not in db.gate.docs[KEY]


def test_an_ordinary_failure_passes_through_untouched():
    db = GateDb()
    publish = AsyncMock(return_value={"success": False, "error": "Media container error: Invalid image"})
    out = _run(pd.dispatch(db, "instagram", {"ig_user_id": "1784"}, publish))
    assert not out.deferred and out.info is None and "paused_until" not in db.gate.docs["instagram:1784"]


def test_an_exception_still_releases_the_account():
    db = GateDb()
    publish = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        _run(pd.dispatch(db, "tiktok", TIKTOK_CONN, publish))
    assert "lease_owner" not in db.gate.docs[KEY]


def test_a_success_resets_the_accounts_backoff():
    db = GateDb()
    db.gate.docs[KEY] = {"_id": KEY, "backoff_level": 2}
    _run(pd.dispatch(db, "tiktok", TIKTOK_CONN, AsyncMock(return_value={"success": True})))
    assert db.gate.docs[KEY]["backoff_level"] == 0


# ── What a deferral does to the draft ─────────────────────────────────────────

NOW = datetime(2026, 11, 3, 12, 0)
RESUME = datetime(2026, 11, 3, 12, 31, tzinfo=timezone.utc)


def test_deferral_puts_the_draft_back_to_scheduled_with_a_time_and_no_failure_count(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    info = rl.classify_tiktok("rate_limit_exceeded")
    f = pd.deferral_fields({"id": "d1"}, RESUME, info, "Queued", now=NOW)
    assert f["status"] == "scheduled" and f["scheduled_date"] == NOW
    assert f["next_attempt_at"] == datetime(2026, 11, 3, 12, 31) and f["next_attempt_at"].tzinfo is None
    assert f["publish_wait"]["reason"] == "rate_limited" and f["publish_wait"]["message"] == "Queued"
    assert "publish_retry_count" not in f and "error_message" not in f


def test_deferral_in_staging_uses_the_staging_status_so_prod_cron_cannot_claim_it(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    assert pd.deferral_fields({}, RESUME, None, "q", now=NOW)["status"] == "staging_scheduled"


def test_deferral_keeps_an_earlier_scheduled_time_but_never_a_future_one():
    earlier = NOW - timedelta(hours=1)
    assert pd.deferral_fields({"scheduled_date": earlier}, RESUME, None, "q", now=NOW)["scheduled_date"] == earlier
    later = NOW + timedelta(hours=1)
    assert pd.deferral_fields({"scheduled_date": later}, RESUME, None, "q", now=NOW)["scheduled_date"] == NOW


def test_a_busy_account_is_labelled_busy_not_rate_limited():
    busy = rl.RateLimitInfo(kind=rl.LimitKind.TEMPORARY, platform="tiktok", code="busy", resume_at=RESUME)
    assert pd.deferral_fields({}, RESUME, busy, "q", now=NOW)["publish_wait"]["reason"] == "account_busy"


def test_only_a_cap_marks_a_draft_as_not_to_be_retried():
    assert pd.cap_fields(rl.classify_tiktok("spam_risk_too_many_posts")) == {"publish_error_kind": "daily_cap", "publish_retry_count": 3}
    assert pd.cap_fields(rl.classify_tiktok("reached_active_user_cap"))["publish_error_kind"] == "app_cap"
    assert pd.cap_fields(rl.classify_tiktok("rate_limit_exceeded")) == {} and pd.cap_fields(None) == {}


def test_the_queued_answer_for_approve():
    d = pd.Dispatch(deferred=True, resume_at=RESUME, message="Queued: soon")
    r = pd.queued_result(d)
    assert r["queued"] is True and r["publish_status"] == "queued" and r["success"] is False
    assert r["resume_at"] == "2026-11-03T12:31:00+00:00" and r["error"] == r["message"] == "Queued: soon"


# ── The scheduler's "not waiting" filter ──────────────────────────────────────

def test_not_waiting_matches_drafts_with_no_or_a_passed_next_attempt_only():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    cond = pd.not_waiting(now)
    assert gate_match({"id": 1}, cond)
    assert gate_match({"id": 2, "next_attempt_at": None}, cond)
    assert gate_match({"id": 3, "next_attempt_at": now - timedelta(minutes=1)}, cond)
    assert gate_match({"id": 4, "next_attempt_at": now}, cond)
    assert not gate_match({"id": 5, "next_attempt_at": now + timedelta(minutes=1)}, cond)


# ── Rate limits are never "reconnect your account" ────────────────────────────

@pytest.fixture
def notes(monkeypatch):
    sent = []

    async def fake_notify(db, draft, published, message=""):
        sent.append((published, message))

    monkeypatch.setattr(pc, "_notify", fake_notify)
    return sent


@pytest.mark.parametrize("raw,category", [
    ("TikTok API error (rate_limit_exceeded): slow down", "rate_limit"),
    ("429 Too Many Requests for url https://api.outstand.so", "rate_limit"),
    ("TikTok API error (spam_risk_too_many_posts): cap", "daily_cap"),
    ("TikTok API error (reached_active_user_cap): cap", "daily_cap"),
])
def test_explain_classifies_limits_without_asking_for_a_reconnect(raw, category):
    info = pc.explain("tiktok", raw)
    assert info["category"] == category and info["reconnect"] is False and "reconnect" not in info["message"].lower()


def test_meta_limits_are_recognised_by_explain_too():
    info = pc.explain("instagram", "Media container error: (#4) Application request limit reached")
    assert info["category"] == "rate_limit" and info["reconnect"] is False


def test_many_rate_limits_in_a_row_never_flag_the_connection(notes):
    db = _DB()
    db["social_connections"] = _Coll([{"outstand_account_id": "acc1", "platform": "tiktok", "user_id": "u1"}])
    draft = {"id": "d1", "user_id": "u1", "platform": "tiktok", "brand_id": None}
    for _ in range(5):                      # the unknown-error threshold is 3
        _run(pc.failed_on_submit(db, draft, {"outstand_account_id": "acc1"}, "429 Too Many Requests"))
    conn = db["social_connections"].docs[0]
    assert "needs_reconnect" not in conn and "consecutive_publish_failures" not in conn


def test_an_uncatalogued_error_still_counts_toward_reconnect_as_before(notes):
    info = pc.explain("tiktok", "Something nobody has seen before")
    assert info["category"] == "unknown"


# ── The Approve path, end to end ──────────────────────────────────────────────

class ApproveDb(_DB):
    def __init__(self):
        super().__init__()
        self["content_drafts"] = _Coll([{"id": "d1", "user_id": "u1", "platform": "tiktok", "status": "ready_to_publish",
                                         "video_url": "https://v/x.mp4", "content": "caption"}])
        self["social_connections"] = _Coll([dict(TIKTOK_CONN)])
        self[rl.GATE_COLLECTION] = FakeGateCollection()


@pytest.fixture
def approve(monkeypatch, notes):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PUBLISH_IMMEDIATE_WAIT_MAX_S", "0")
    db = ApproveDb()

    def go(publish_result=None, scheduled_datetime=None):
        publish = AsyncMock(return_value=publish_result or {"success": True, "post_id": "pid"})
        with patch.object(ApprovalWorkflowService, "_publish_to_platform", publish), \
             patch("app.services.NotificationService.notification_service.notify_content_posted", AsyncMock()):
            results = _run(ApprovalWorkflowService._trigger_immediate_publishing(db, "u1", ["d1"], scheduled_datetime))
        return results["d1"], db["content_drafts"].docs[0], publish

    return db, go


def test_approve_publishes_a_free_account(approve):
    db, go = approve
    result, draft, publish = go()
    assert result["success"] and draft["status"] == "published" and publish.await_count == 1


def test_approve_on_a_paused_account_answers_queued_and_defers_the_draft(approve):
    db, go = approve
    _run(rl.pause(db, KEY, until=rl.utcnow() + timedelta(minutes=10), code="rate_limit_exceeded"))
    result, draft, publish = go()
    assert result["queued"] is True and result["publish_status"] == "queued" and result["resume_at"]
    assert draft["status"] == "scheduled" and draft["next_attempt_at"] > datetime.utcnow()
    assert draft["publish_wait"]["reason"] == "rate_limited"
    publish.assert_not_called()


def test_approve_on_a_busy_account_answers_queued(approve):
    db, go = approve
    _run(rl.acquire(db, KEY, "another-post", lease_s=300))
    result, draft, publish = go()
    assert result["queued"] and draft["status"] == "scheduled" and draft["publish_wait"]["reason"] == "account_busy"
    publish.assert_not_called()


def test_a_platform_rate_limit_during_approve_queues_instead_of_failing(approve):
    db, go = approve
    result, draft, publish = go(_limited())
    assert result["queued"] and draft["status"] == "scheduled" and draft["next_attempt_at"]
    assert "error_message" not in draft and "publish_retry_count" not in draft
    assert db[rl.GATE_COLLECTION].docs[KEY]["paused_until"] > rl.utcnow()


def test_a_cap_during_approve_fails_clearly_and_is_tagged_not_to_be_retried(approve):
    db, go = approve
    result, draft, publish = go(_limited("spam_risk_too_many_posts", "TikTok direct publish failed: capped"))
    assert not result.get("queued") and result["success"] is False
    assert draft["status"] == "publish_failed" and draft["publish_error_kind"] == "daily_cap"
    assert draft["publish_retry_count"] == 3 and "daily posting limit" in draft["error_message"]
    assert "reconnect" not in draft["error_message"].lower()
    assert "paused_until" not in db[rl.GATE_COLLECTION].docs[KEY]


def test_scheduling_with_outstand_is_a_submission_and_is_not_gated(approve):
    db, go = approve
    _run(rl.pause(db, KEY, until=rl.utcnow() + timedelta(minutes=10)))
    result, draft, publish = go(scheduled_datetime=datetime.utcnow() + timedelta(hours=2))
    assert publish.await_count == 1 and not result.get("queued")


def test_a_deferred_draft_that_later_goes_out_forgets_that_it_was_waiting(approve):
    db, go = approve
    db["content_drafts"].docs[0].update({"next_attempt_at": datetime.utcnow() - timedelta(minutes=1),
                                         "publish_wait": {"reason": "rate_limited"}})
    result, draft, publish = go()
    assert draft["status"] == "published" and "next_attempt_at" not in draft and "publish_wait" not in draft
