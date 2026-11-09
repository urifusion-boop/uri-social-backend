"""
A clip that is still rendering on fal when its model's ceiling passes is not failed and not re-rendered (that billed
the customer twice). The job moves on, the clip is recorded as pending, and a watcher saves it when fal finishes and
tells the customer so they can merge. The job stays "generating" (so nothing can be merged early) until the last late
clip lands, and the customer's credits are only settled after that.
"""
import asyncio
import sys
import types
from datetime import datetime, timedelta

import pytest

from app.agents.social_media_manager.services import video_generation_billing as B
from app.agents.social_media_manager.services import video_generation_service as V


_real_sleep = asyncio.sleep        # kept before any test patches asyncio.sleep, to really yield to background tasks


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _tick(times=5):
    for _ in range(times):
        _run(_real_sleep(0))


# A tiny in-memory stand-in for the jobs collection: just the filters and updates the code uses.
class Jobs:
    def __init__(self, **doc):
        self.doc = {"job_id": "J1", "status": "generating", "user_id": "u1", "clips": [], **doc}

    def _matches(self, flt):
        for key, want in flt.items():
            if key == "job_id":
                if self.doc.get("job_id") != want:
                    return False
            elif key == "clips.scene_number":
                if not any(c.get("scene_number") == want for c in self.doc["clips"]):
                    return False
            elif key == "clips":
                if "$not" in want and any(c.get("pending") for c in self.doc["clips"]):
                    return False
            elif self.doc.get(key) != want:
                return False
        return True

    async def update_one(self, flt, update):
        if not self._matches(flt):
            return
        for key, val in update.get("$set", {}).items():
            if key.startswith("clips.$."):
                target = next(c for c in self.doc["clips"] if c.get("scene_number") == flt["clips.scene_number"])
                target[key[len("clips.$."):]] = val
            else:
                self.doc[key] = val
        for key, val in update.get("$push", {}).items():
            self.doc.setdefault(key, []).append(val)
        for key in update.get("$unset", {}):
            self.doc.pop(key, None)

    async def find_one(self, flt, projection=None):
        return self.doc if self._matches(flt) else None


class Completed:
    error = None


class InProgress:
    pass


class Handle:
    def __init__(self, statuses, result=None):
        self.statuses, self.result_value = list(statuses), result or {"video": {"url": "https://fal/late.mp4"}}
        self.request_id = "req-late"

    async def status(self, with_logs=False):
        nxt = self.statuses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt()

    async def get(self):
        return self.result_value


@pytest.fixture
def env(monkeypatch):
    jobs = Jobs()
    notes, settled = [], []

    async def instant(_s):
        return None

    class FakeNotifications:
        async def _log_notification(self, **kw):
            notes.append(kw)

    class FakeBilling:
        @staticmethod
        async def settle(job_id, **kw):
            settled.append(job_id)

    fake_mod = types.ModuleType("app.services.NotificationService")
    fake_mod.notification_service = FakeNotifications()
    monkeypatch.setitem(sys.modules, "app.services.NotificationService", fake_mod)
    monkeypatch.setitem(sys.modules, "app.agents.social_media_manager.services.video_generation_billing", FakeBilling)
    monkeypatch.setattr(sys.modules["app.agents.social_media_manager.services"], "video_generation_billing",
                        FakeBilling, raising=False)
    monkeypatch.setattr(V.asyncio, "sleep", instant)
    monkeypatch.setattr(V, "_RETRY_PAUSE_SECONDS", 0)
    monkeypatch.setattr(V, "_jobs_collection", lambda: jobs)

    async def fake_store(result, model, seconds):
        return "https://cdn/late.mp4", 0.4

    monkeypatch.setattr(V.VideoGenerationService, "_store_clip", staticmethod(fake_store))
    return types.SimpleNamespace(jobs=jobs, notes=notes, settled=settled)


def _late(handle):
    late = V.ClipStillRendering("still rendering", "req-late", handle, "bytedance/seedance-2.0/image-to-video")
    late.actual_seconds = 5
    return late


STORY = {"scenes": [
    {"scene_number": 1, "duration_seconds": 5, "video_prompt": "x", "frame_image_url": "u"},
    {"scene_number": 2, "duration_seconds": 5, "video_prompt": "y", "frame_image_url": "u"},
]}


def _run_job(env, monkeypatch, handle):
    async def fake_scene(scene, model, voice, job_id, notes=None):
        if scene["scene_number"] == 1:
            raise _late(handle)
        return "https://cdn/s2.mp4", 0.1

    monkeypatch.setattr(V.VideoGenerationService, "_generate_scene_fal", staticmethod(fake_scene))

    async def go():
        await V.VideoGenerationService.run_job("J1", STORY, [], "advanced_references")

    _run(go())


# ── the job moves on and stays open ──────────────────────────────────────────

def test_a_still_rendering_clip_is_recorded_pending_and_the_job_stays_generating(env, monkeypatch):
    # The watcher would wait forever on a clip that never finishes, so stand it down: this test is about the job.
    async def idle(*a, **k):
        return None

    monkeypatch.setattr(V.VideoGenerationService, "_watch_late_clip", staticmethod(idle))
    _run_job(env, monkeypatch, Handle([]))
    doc = env.jobs.doc
    first, second = doc["clips"]
    assert first["pending"] is True and first["video_url"] is None and first["fal_request_id"] == "req-late"
    assert isinstance(first["pending_since"], datetime) and first["routed_model"].startswith("bytedance/seedance")
    assert second["video_url"] == "https://cdn/s2.mp4"                    # the next scene was not held up
    assert doc["dispatch_done"] is True and doc["status"] == "generating"  # open: nothing can be merged yet


# ── the watcher delivers it, tells the customer, and completes the job ───────

def test_a_late_clip_is_saved_the_job_completes_and_the_customer_is_told(env, monkeypatch):
    _run_job(env, monkeypatch, Handle([InProgress, InProgress, Completed]))
    _tick()                                                   # let the watcher task run to the end
    doc = env.jobs.doc
    first = doc["clips"][0]
    assert first["video_url"] == "https://cdn/late.mp4" and first["pending"] is False and first["late"] is True
    assert first["cost_usd"] == 0.4
    assert doc["status"] == "complete"
    assert env.settled == ["J1"]
    assert len(env.notes) == 1
    note = env.notes[0]
    assert note["user_id"] == "u1" and note["notification_type"] == "video_clip_ready"
    assert "Scene 1" in note["metadata"]["message"] and "merge" in note["metadata"]["message"]
    assert note["metadata"]["delivered"] is True


def test_a_late_clip_that_fails_on_fal_is_reported_refunded_and_the_job_still_completes(env, monkeypatch):
    class Completed:                  # fal reports a failed request as Completed, carrying an error
        error = "model exploded"
    _run_job(env, monkeypatch, Handle([InProgress, Completed]))
    _tick()
    doc = env.jobs.doc
    first = doc["clips"][0]
    assert first["pending"] is False and first["video_url"] is None and "model exploded" in first["error"]
    assert "credits for this scene are returned" in first["error"]
    assert doc["status"] == "complete" and env.settled == ["J1"]
    assert env.notes[0]["metadata"]["delivered"] is False
    assert env.notes[0]["subject"] == "A video clip could not be finished"


def test_the_job_is_not_completed_by_the_watcher_until_every_scene_was_dispatched(env, monkeypatch):
    """Whichever of the job loop and the watcher finishes last completes the job; the watcher alone must not."""
    env.jobs.doc["clips"] = [{"scene_number": 1, "pending": True, "pending_since": datetime.utcnow(), "video_url": None}]
    env.jobs.doc["dispatch_done"] = False                      # the loop is still dispatching later scenes
    late = _late(Handle([Completed]))
    _run(V.VideoGenerationService._watch_late_clip("J1", 1, late))
    assert env.jobs.doc["clips"][0]["video_url"] == "https://cdn/late.mp4"
    assert env.jobs.doc["status"] == "generating" and env.settled == []


# ── billing waits for the clip ───────────────────────────────────────────────

def test_a_pending_clip_holds_settlement_back_until_it_lands_or_is_too_old():
    now = datetime.utcnow()
    fresh = {"clips": [{"scene_number": 1, "pending": True, "pending_since": now - timedelta(minutes=10)}]}
    old = {"clips": [{"scene_number": 1, "pending": True, "pending_since": now - timedelta(minutes=80)}]}
    done = {"clips": [{"scene_number": 1, "pending": False, "video_url": "u"}]}
    assert B.has_live_pending_clip(fresh, now) is True
    assert B.has_live_pending_clip(old, now) is False          # waited long enough: refunded like any undelivered scene
    assert B.has_live_pending_clip(done, now) is False
    assert B.has_live_pending_clip({}, now) is False


def test_settle_does_nothing_while_a_clip_is_still_on_its_way():
    class Col:
        claimed = False

        async def find_one(self, flt):
            return {"job_id": "J1", "status": "charged", "credits_charged": 4, "user_id": "u1"}

        async def find_one_and_update(self, *a, **k):
            Col.claimed = True

    job = {"total_scenes": 2, "clips": [{"scene_number": 1, "pending": True, "pending_since": datetime.utcnow()},
                                        {"scene_number": 2, "video_url": "u"}]}
    out = _run(B.settle("J1", job=job, col=Col()))
    assert Col.claimed is False and out["status"] == "charged"


def test_the_notification_type_is_one_the_notification_model_accepts():
    """_log_notification validates the type; an unregistered one would raise and the customer would never be told."""
    from typing import get_args
    from app.domain.models.notification_models import NotificationType
    assert "video_clip_ready" in get_args(NotificationType)
