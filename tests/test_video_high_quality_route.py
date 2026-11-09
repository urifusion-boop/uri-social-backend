"""
The "High Quality" video button renders on Google's Gemini Omni Flash (via fal.ai), with no fallback model.

fal's Veo 3 is retired and Veo 3.1 is being removed on 2026-11-17, so High Quality moved to Omni Flash. It has no
fallback for now: a scene that fails is reported as failed (the billing step refunds undelivered scenes) instead of
quietly re-rendering on a lesser model than the customer chose.
"""
import asyncio
import sys
import types

import pytest

from app.agents.social_media_manager.services import video_generation_service as V
from app.core.config import settings

OMNI = "google/gemini-omni-flash/v1.2/image-to-video"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_high_quality_uses_omni_flash_and_has_no_fallback():
    route = V.OUTCOME_ROUTES["high_quality"]
    assert route["primary"] == OMNI == V._OMNI_FLASH
    assert route["fallback"] is None
    assert V.MODEL_REGISTRY[OMNI]["label"] == "Gemini Omni Flash"
    assert V.MODEL_REGISTRY[OMNI]["cost_per_second"] == pytest.approx(0.0819)


def test_every_other_outcome_still_has_a_fallback_that_exists():
    for name, route in V.OUTCOME_ROUTES.items():
        if name == "high_quality":
            continue
        assert route["fallback"] in V.MODEL_REGISTRY, name


class _Stop(Exception):
    pass


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_submit(model, arguments, timeout, scene_num, job_id=None):
        calls.append((model, arguments))
        raise _Stop()

    monkeypatch.setitem(sys.modules, "fal_client", types.ModuleType("fal_client"))
    monkeypatch.setattr(V.VideoGenerationService, "_submit_and_poll", staticmethod(fake_submit))
    monkeypatch.setattr(settings, "FAL_API_KEY", "k", raising=False)
    return calls


def _clip(seconds, prompt="Rice steams on a plate"):
    scene = {"scene_number": 1, "frame_image_url": "https://img/f.png", "duration_seconds": seconds,
             "video_prompt": prompt}
    with pytest.raises(_Stop):
        _run(V.VideoGenerationService._generate_scene_fal(scene, OMNI, V.DEFAULT_AVATAR_VOICE, "job1"))


def test_an_omni_flash_clip_is_vertical_720p_with_an_integer_duration(sent):
    _clip(8)
    model, args = sent[-1]
    assert model == OMNI
    assert args == {"image_url": "https://img/f.png", "prompt": "Rice steams on a plate", "duration": 8,
                    "aspect_ratio": "9:16", "resolution": "720p"}


@pytest.mark.parametrize("asked,sent_seconds", [(1, 3), (2, 3), (3, 3), (5, 5), (15, 15), (20, 15)])
def test_the_duration_is_kept_within_omni_flashs_3_to_15_seconds(sent, asked, sent_seconds):
    _clip(asked)
    assert sent[-1][1]["duration"] == sent_seconds and isinstance(sent[-1][1]["duration"], int)


def test_a_person_clip_still_gets_the_steady_face_sentence(sent):
    _clip(5, "A young woman smiles and waves")
    assert "same face" in sent[-1][1]["prompt"]


# ── a failed High Quality scene is reported, not re-rendered on another model ─

class _Col:
    def __init__(self):
        self.clips, self.sets = [], []

    async def update_one(self, flt, update):
        if "$push" in update:
            self.clips.append(update["$push"]["clips"])
        if "$set" in update:
            self.sets.append(update["$set"])


def _run_job(monkeypatch, outcome, failing):
    col, used = _Col(), []
    monkeypatch.setattr(V, "_RETRY_PAUSE_SECONDS", 0)

    async def fake_scene(scene, model, voice, job_id, notes=None):
        used.append(model)
        if model in failing:
            raise RuntimeError(f"{model} exploded")
        return "https://cdn/clip.mp4", 0.4

    monkeypatch.setattr(V, "_jobs_collection", lambda: col)
    monkeypatch.setattr(V.VideoGenerationService, "_generate_scene_fal", staticmethod(fake_scene))
    story = {"scenes": [{"scene_number": 1, "duration_seconds": 5, "video_prompt": "x", "frame_image_url": "u"}]}
    _run(V.VideoGenerationService.run_job("J1", story, [], outcome))
    return col, used


def test_a_failed_high_quality_scene_is_recorded_as_failed_with_no_second_model(monkeypatch):
    col, used = _run_job(monkeypatch, "high_quality", failing={OMNI})
    assert used == [OMNI, OMNI]                              # one more try on the SAME model, then nothing else
    clip = col.clips[0]
    assert clip["video_url"] is None and clip["routed_model"] is None and clip["fallback_used"] is False
    assert "exploded" in clip["error"]
    assert {"dispatch_done": True, "current_scene": 1} in col.sets and {"status": "complete"} in col.sets


def test_a_successful_high_quality_scene_is_delivered_by_omni_flash(monkeypatch):
    col, used = _run_job(monkeypatch, "high_quality", failing=set())
    assert used == [OMNI]
    assert col.clips[0]["video_url"] == "https://cdn/clip.mp4" and col.clips[0]["routed_model"] == OMNI


def test_other_outcomes_still_fall_back_when_the_first_model_fails(monkeypatch):
    col, used = _run_job(monkeypatch, "quick_video", failing={V._H3_TURBO})
    assert used == [V._H3_TURBO, V._H3_TURBO, V._PIXVERSE]      # the first model gets a second try before the fallback
    assert col.clips[0]["fallback_used"] is True and col.clips[0]["routed_model"] == V._PIXVERSE
