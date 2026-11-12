"""
When our fal.ai balance is used up, the Video page must say something calm and true, not dump
"fal-ai/…: User is locked. Reason: Exhausted balance… | fallback …" on the customer. The job also must not
burn retries and a pointless fallback model (same fal account) on every scene — it stops, the admin is alerted,
and the credits for the scenes that were not made go back (the billing settle handles that).
"""
import asyncio
import types

import pytest

from app.agents.social_media_manager.services import video_generation_service as V
from app.services import fal_errors

# What fal actually sends for a locked account: HTTP 403, this `detail` (fal_client puts it in str(exc)).
FAL_LOCKED = "User is locked. Reason: Exhausted balance. Top up your balance at fal.ai/dashboard/billing."


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(V.asyncio, "sleep", instant)
    monkeypatch.setattr(V, "_RETRY_PAUSE_SECONDS", 0)


# ── recognising it ───────────────────────────────────────────────────────────

class _HttpError(Exception):
    """Shaped like an httpx / fal_client HTTP error: the text of the response is on `.response`."""

    def __init__(self, message, body):
        super().__init__(message)
        self.response = types.SimpleNamespace(text=body)


@pytest.mark.parametrize("error", [
    RuntimeError(FAL_LOCKED),
    RuntimeError(f"fal-ai/kling-video/v2.5-turbo/standard/image-to-video failed: {FAL_LOCKED}"),
    FAL_LOCKED,
    _HttpError("Client error '403 Forbidden' for url 'https://queue.fal.run/x'", '{"detail": "%s"}' % FAL_LOCKED),
    RuntimeError("USER IS LOCKED"),
])
def test_fals_locked_account_error_is_recognised(error):
    assert fal_errors.is_out_of_credit(error)


@pytest.mark.parametrize("error", [
    RuntimeError("503 service unavailable"),
    RuntimeError("content_policy_violation: prompt refused"),
    RuntimeError("403 Forbidden: invalid API key"),
    RuntimeError("fal-ai/x failed: internal server error"),
    TimeoutError("timed out after 300s"),
])
def test_other_failures_are_not_mistaken_for_it(error):
    assert not fal_errors.is_out_of_credit(error)


def test_it_is_never_retried():
    assert not V.is_retryable(RuntimeError(FAL_LOCKED))


# ── the job ──────────────────────────────────────────────────────────────────

class _Col:
    def __init__(self):
        self.clips, self.sets = [], []

    async def update_one(self, flt, update):
        if "$push" in update:
            self.clips.append(update["$push"]["clips"])
        if "$set" in update:
            self.sets.append(update["$set"])


def _scenes(n):
    return {"scenes": [
        {"scene_number": i, "duration_seconds": 5, "video_prompt": "x", "frame_image_url": "u"}
        for i in range(1, n + 1)
    ]}


def _job(monkeypatch, scenes, behaviour):
    """Run a job; behaviour(scene_number, model) returns a clip tuple or raises."""
    col, calls, alerts = _Col(), [], []

    async def fake_scene(scene, model, voice, job_id, notes=None):
        calls.append((scene["scene_number"], model))
        return behaviour(scene["scene_number"], model)

    async def fake_alert(error):
        alerts.append(str(error))

    monkeypatch.setattr(V, "_jobs_collection", lambda: col)
    monkeypatch.setattr(V.VideoGenerationService, "_generate_scene_fal", staticmethod(fake_scene))
    monkeypatch.setattr(fal_errors, "alert_out_of_credit", fake_alert)
    _run(V.VideoGenerationService.run_job("J1", _scenes(scenes), [], V.DEFAULT_OUTCOME))
    return col, calls, alerts


def _locked(scene_number, model):
    raise RuntimeError(f"{model} failed: {FAL_LOCKED}")


def test_an_empty_fal_balance_stops_the_job_after_one_attempt_and_never_tries_the_fallback(monkeypatch):
    col, calls, alerts = _job(monkeypatch, 3, _locked)
    primary = V.OUTCOME_ROUTES[V.DEFAULT_OUTCOME]["primary"]
    assert calls == [(1, primary)]            # one try: no retry, no fallback model, scenes 2 and 3 not attempted
    assert len(alerts) == 1                   # the admin is told once, not once per scene


def test_every_scene_gets_the_calm_message_and_none_shows_fals_text(monkeypatch):
    col, _, _ = _job(monkeypatch, 3, _locked)
    assert [c["scene_number"] for c in col.clips] == [1, 2, 3]
    for clip in col.clips:
        assert clip["video_url"] is None
        assert clip["error"] == fal_errors.CLIP_MESSAGE and clip["error_code"] == fal_errors.ERROR_CODE
        assert "fal" not in clip["error"].lower().replace("fallback", "") and "balance" not in clip["error"].lower()


def test_a_job_with_nothing_made_fails_with_the_plain_message(monkeypatch):
    col, _, _ = _job(monkeypatch, 2, _locked)
    failed = next(s for s in col.sets if s.get("status") == "failed")
    assert failed["error"] == fal_errors.JOB_MESSAGE and failed["error_code"] == fal_errors.ERROR_CODE
    assert "Exhausted" not in failed["error"] and "fal.ai" not in failed["error"]


def test_scenes_made_before_the_balance_ran_out_are_kept_and_the_job_is_not_failed(monkeypatch):
    def behaviour(scene_number, model):
        if scene_number == 1:
            return "https://cdn/clip1.mp4", 0.1
        return _locked(scene_number, model)

    col, calls, _ = _job(monkeypatch, 3, behaviour)
    assert col.clips[0]["video_url"] == "https://cdn/clip1.mp4" and "error_code" not in col.clips[0]
    assert [c["error_code"] for c in col.clips[1:]] == [fal_errors.ERROR_CODE] * 2
    notice = next(s for s in col.sets if s.get("error_code"))
    assert notice["error"] == fal_errors.JOB_MESSAGE and "status" not in notice     # partial: not marked failed
    assert {"status": "complete"} in col.sets                                       # the normal completion still runs


def test_a_fallback_refused_for_the_same_reason_is_handled_the_same_way(monkeypatch):
    primary = V.OUTCOME_ROUTES[V.DEFAULT_OUTCOME]["primary"]

    def behaviour(scene_number, model):
        if model == primary:
            raise RuntimeError(f"{model} exploded")           # an ordinary failure first…
        return _locked(scene_number, model)                   # …then the fallback hits the empty balance

    col, calls, alerts = _job(monkeypatch, 2, behaviour)
    assert col.clips[-1]["error_code"] == fal_errors.ERROR_CODE and len(alerts) == 1
    assert all(model != primary for scene, model in calls[2:])  # scene 2 is not attempted at all


def test_an_ordinary_failure_still_reports_its_own_error_and_alerts_nobody(monkeypatch):
    def behaviour(scene_number, model):
        raise RuntimeError(f"{model} exploded")

    col, _, alerts = _job(monkeypatch, 1, behaviour)
    assert "exploded" in col.clips[0]["error"] and "error_code" not in col.clips[0]
    assert alerts == []
    assert not any(s.get("error_code") for s in col.sets)


# ── the admin alert ──────────────────────────────────────────────────────────

def test_the_admin_alert_is_raised_and_a_broken_alert_never_breaks_the_job(monkeypatch):
    from app.services import platform_alerts
    raised = []

    async def fake_raise(db, kind, title, detail, now=None):
        raised.append((kind, title, detail))

    monkeypatch.setattr(platform_alerts, "raise_alert", fake_raise)
    import app.database as database
    monkeypatch.setattr(database, "get_db", lambda: object())
    _run(fal_errors.alert_out_of_credit(RuntimeError(FAL_LOCKED)))
    kind, title, detail = raised[0]
    assert kind == "fal_insufficient_balance" and "out of credit" in title and "Exhausted balance" in detail

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(platform_alerts, "raise_alert", boom)
    _run(fal_errors.alert_out_of_credit(RuntimeError(FAL_LOCKED)))     # must not raise
