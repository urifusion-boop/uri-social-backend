"""
A video should not fail for a reason a second try would fix, and a request that may already be paid for on fal must
never be sent again. Live staging test 2026-11-09: Seedance finished at 311s and 346s on fal, but the app had given up
at 300s and re-rendered both scenes on a fallback model, so each scene was billed twice.
"""
import asyncio
import sys
import types

import pytest

from app.agents.social_media_manager.services import video_creative_service as vcs
from app.agents.social_media_manager.services import video_generation_service as V
from app.agents.social_media_manager.services import video_storyboard_service as vss


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(V.asyncio, "sleep", instant)
    monkeypatch.setattr(V, "_RETRY_PAUSE_SECONDS", 0)


# ── which failures are worth another try ─────────────────────────────────────

@pytest.mark.parametrize("exc", [
    RuntimeError("fal-ai/x failed: internal server error"),
    ConnectionError("connection reset by peer"),
    RuntimeError("503 service unavailable"),
])
def test_transient_failures_are_retried(exc):
    assert V.is_retryable(exc)


@pytest.mark.parametrize("exc", [
    TimeoutError("did not respond within 300s"),
    V.NoResubmit("lost contact"),
    ValueError("requires a storyboard frame image"),
    RuntimeError("content_policy_violation"),
    RuntimeError("The prompt contains sensitive content"),
    RuntimeError("422 validation error: invalid duration"),
])
def test_failures_a_retry_cannot_fix_or_that_may_already_be_paid_for_are_not(exc):
    assert not V.is_retryable(exc)


def test_retry_transient_succeeds_after_blips_and_gives_up_on_permanent_errors():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("blip")
        return "ok"

    assert _run(V._retry_transient(flaky, "x")) == "ok" and len(calls) == 3

    calls.clear()

    async def refused():
        calls.append(1)
        raise RuntimeError("content_policy_violation")

    with pytest.raises(RuntimeError):
        _run(V._retry_transient(refused, "x"))
    assert len(calls) == 1


# ── polling fal never turns a hiccup into a second paid request ──────────────

class Queued: position = 0
class InProgress: pass
class Completed: error = None


class _Handle:
    def __init__(self, statuses, get_failures=0):
        self.statuses, self.get_failures = list(statuses), get_failures
        self.status_calls = self.get_calls = self.cancelled = 0

    async def status(self, with_logs=False):
        self.status_calls += 1
        nxt = self.statuses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt()

    async def get(self):
        self.get_calls += 1
        if self.get_calls <= self.get_failures:
            raise ConnectionError("fetch blip")
        return {"video": {"url": "https://v/1.mp4"}}

    async def cancel(self):
        self.cancelled += 1


@pytest.fixture
def fal(monkeypatch):
    submitted = []
    box = types.SimpleNamespace(handle=None, submit_failures=0)

    async def submit_async(model, arguments=None):
        if len(submitted) < box.submit_failures:
            submitted.append("failed")
            raise ConnectionError("submit blip")
        submitted.append(model)
        return box.handle

    mod = types.ModuleType("fal_client")
    mod.submit_async = submit_async
    monkeypatch.setitem(sys.modules, "fal_client", mod)
    box.submitted = submitted
    return box


def test_a_failed_progress_check_does_not_end_the_request(fal):
    fal.handle = _Handle([InProgress, ConnectionError("net"), TimeoutError("poll timed out"), RuntimeError("502"), Completed])
    out = _run(V.VideoGenerationService._submit_and_poll("m", {}, 900, 1))
    assert out["video"]["url"] == "https://v/1.mp4"
    assert fal.submitted == ["m"]                          # submitted ONCE: nothing was re-sent


def test_a_failed_submit_is_retried_because_nothing_was_sent(fal):
    fal.submit_failures = 2
    fal.handle = _Handle([Completed])
    assert _run(V.VideoGenerationService._submit_and_poll("m", {}, 900, 1))["video"]
    assert fal.submitted == ["failed", "failed", "m"]


def test_a_blip_fetching_a_finished_clip_is_retried(fal):
    fal.handle = _Handle([Completed], get_failures=2)
    assert _run(V.VideoGenerationService._submit_and_poll("m", {}, 900, 1))["video"]
    assert fal.handle.get_calls == 3


def test_losing_contact_for_good_is_not_a_reason_to_send_it_again(fal, monkeypatch):
    monkeypatch.setattr(V, "_MAX_CONSECUTIVE_POLL_FAILURES", 3)
    fal.handle = _Handle([ConnectionError("a"), ConnectionError("b"), ConnectionError("c")])
    with pytest.raises(V.NoResubmit):
        _run(V.VideoGenerationService._submit_and_poll("m", {}, 900, 1))


def test_a_clip_that_finished_but_cannot_be_fetched_is_not_sent_again(fal):
    fal.handle = _Handle([Completed], get_failures=99)
    with pytest.raises(V.NoResubmit):
        _run(V.VideoGenerationService._submit_and_poll("m", {}, 900, 1))


def test_a_timeout_asks_fal_to_cancel_the_request(fal):
    fal.handle = _Handle([InProgress])
    with pytest.raises(TimeoutError):
        _run(V.VideoGenerationService._submit_and_poll("m", {}, -1, 1))
    assert fal.handle.cancelled == 1


# ── run_job: retry the same model, never pay twice ───────────────────────────

class _Col:
    def __init__(self):
        self.clips = []

    async def update_one(self, flt, update):
        if "$push" in update:
            self.clips.append(update["$push"]["clips"])


def _job(monkeypatch, outcome, behaviour):
    col, used = _Col(), []

    async def fake_scene(scene, model, voice, job_id, notes=None):
        used.append(model)
        result = behaviour(model, used.count(model))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(V, "_jobs_collection", lambda: col)
    monkeypatch.setattr(V.VideoGenerationService, "_generate_scene_fal", staticmethod(fake_scene))
    story = {"scenes": [{"scene_number": 1, "duration_seconds": 5, "video_prompt": "x", "frame_image_url": "u"}]}
    _run(V.VideoGenerationService.run_job("J1", story, [], outcome))
    return col.clips[0], used


def test_a_one_off_failure_is_fixed_by_a_second_try_on_the_same_model_with_no_fallback(monkeypatch):
    clip, used = _job(monkeypatch, "high_quality",
                      lambda model, n: RuntimeError("internal error") if n == 1 else ("https://cdn/c.mp4", 0.4))
    assert used == [V._OMNI_FLASH, V._OMNI_FLASH]
    assert clip["video_url"] == "https://cdn/c.mp4" and clip["fallback_used"] is False and clip["warning"] is None


def test_a_refused_prompt_is_not_retried_on_the_same_model_but_does_fall_back(monkeypatch):
    clip, used = _job(monkeypatch, "quick_video",
                      lambda model, n: RuntimeError("content_policy_violation") if model == V._H3_TURBO
                      else ("https://cdn/c.mp4", 0.1))
    assert used == [V._H3_TURBO, V._PIXVERSE]
    assert clip["fallback_used"] is True


def test_a_request_that_may_have_finished_is_never_re_rendered_elsewhere(monkeypatch):
    clip, used = _job(monkeypatch, "quick_video", lambda model, n: V.NoResubmit("lost contact"))
    assert used == [V._H3_TURBO]                           # not retried, and the fallback was not used
    assert clip["video_url"] is None and "not made twice" in clip["error"] and clip["fallback_used"] is False


# ── a scene's picture gets a second try, because without it there is no clip ─

def test_a_failed_describe_it_frame_is_tried_again(monkeypatch):
    attempts = []

    async def fake_draw(scene, refs, prev, brand, identity=None):
        attempts.append(scene["scene_number"])
        return None if len(attempts) == 1 else "https://cdn/f.png"

    pushed = []

    class Col:
        async def update_one(self, flt, update):
            pushed.append(update)

    monkeypatch.setattr(vcs.VideoCreativeService, "_generate_scene_frame", staticmethod(fake_draw))
    monkeypatch.setattr(vcs, "_frame_jobs_collection", lambda: Col())
    _run(vcs.VideoCreativeService.run_frame_job("J", [{"scene_number": 1, "video_prompt": "x"}], [], None))
    assert attempts == [1, 1]
    assert any("$push" in u and u["$push"]["frames"]["frame_image_url"] == "https://cdn/f.png" for u in pushed)


def test_a_failed_photo_storyboard_frame_is_tried_again(monkeypatch):
    attempts = []

    async def fake_frame(scene, brand_images, chained_frame_url=None):
        attempts.append(1)
        return None if len(attempts) == 1 else "https://cdn/f.png"

    class Col:
        async def update_one(self, flt, update):
            pass

    monkeypatch.setattr(vss.VideoStoryboardService, "_generate_scene_frame", staticmethod(fake_frame))
    monkeypatch.setattr(vss, "_frame_jobs_collection", lambda: Col())
    _run(vss.VideoStoryboardService.run_frame_job("J", [{"scene_number": 1}], []))
    assert len(attempts) == 2
