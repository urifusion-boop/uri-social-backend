"""
EXPERIMENT: Complex / Cinematic (Wan 3.0) and Advanced References (Seedance 2.0) are given a scene's spoken line in
the clip prompt, so they can be tried on a script with dialogue. Only the talking avatar reads `dialogue` otherwise.
The customer sees a normal finished clip (no warning); the scene is only logged.
"""
import asyncio
import sys
import types

import pytest

from app.agents.social_media_manager.services import video_generation_service as V
from app.core.config import settings


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


LINE = 'I used to spend hours "posting" every week'


def test_the_spoken_line_is_added_in_quotes_with_lip_sync_and_single_speaker_instructions():
    out = V.with_spoken_line("A woman at her desk, slow push-in", LINE)
    assert out.startswith("A woman at her desk, slow push-in")
    assert "saying exactly: \"I used to spend hours 'posting' every week\"" in out    # inner quotes can't break it
    assert "lips moving in sync" in out and "Only this person speaks" in out


def test_no_line_means_no_change():
    assert V.with_spoken_line("A woman at her desk", "  ") == "A woman at her desk"
    assert V.with_spoken_line("A woman at her desk", None) == "A woman at her desk"


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


def _clip(model, dialogue, notes):
    scene = {"scene_number": 1, "frame_image_url": "https://img/f.png", "duration_seconds": 5,
             "video_prompt": "A woman at her desk", "dialogue": dialogue}
    with pytest.raises(_Stop):
        _run(V.VideoGenerationService._generate_scene_fal(scene, model, V.DEFAULT_AVATAR_VOICE, "job1", notes=notes))


@pytest.mark.parametrize("model", [V._WAN_3, V._SEEDANCE_FAST])
def test_cinematic_and_references_models_get_the_line_but_the_clip_carries_no_warning(sent, model, capsys):
    notes = {}
    _clip(model, "Jane has got this", notes)
    prompt = sent[-1][1]["prompt"]
    assert 'saying exactly: "Jane has got this"' in prompt
    assert prompt.startswith("A woman at her desk") and "same face" in prompt      # face guidance is still there
    assert notes == {}                                                              # nothing for the customer to see
    assert "SPEECH-IN-PROMPT experiment" in capsys.readouterr().out                 # ...but the log records it


@pytest.mark.parametrize("model", [V._WAN_3, V._SEEDANCE_FAST])
def test_a_scene_with_no_dialogue_is_sent_without_speech(sent, model, capsys):
    notes = {}
    _clip(model, None, notes)
    assert "saying exactly" not in sent[-1][1]["prompt"] and notes == {}
    assert "SPEECH-IN-PROMPT" not in capsys.readouterr().out


@pytest.mark.parametrize("model", [V._H3_MAX, V._KLING_STANDARD, V._OMNI_FLASH])
def test_other_models_are_left_alone(sent, model):
    notes = {}
    _clip(model, "Jane has got this", notes)
    assert "saying exactly" not in sent[-1][1]["prompt"] and notes == {}


def test_a_finished_clip_on_the_experiment_models_has_no_warning(monkeypatch):
    clips = []

    class Col:
        async def update_one(self, flt, update):
            if "$push" in update:
                clips.append(update["$push"]["clips"])

    async def fake_scene(scene, model, voice, job_id, notes=None):
        return "https://cdn/clip.mp4", 0.5

    monkeypatch.setattr(V, "_jobs_collection", lambda: Col())
    monkeypatch.setattr(V.VideoGenerationService, "_generate_scene_fal", staticmethod(fake_scene))
    story = {"scenes": [{"scene_number": 1, "duration_seconds": 5, "video_prompt": "x", "dialogue": "Hi",
                         "frame_image_url": "u"}]}
    _run(V.VideoGenerationService.run_job("J1", story, [], "advanced_references"))
    assert clips[0]["warning"] is None and clips[0]["routed_model"] == V._SEEDANCE_FAST
    assert clips[0]["fallback_used"] is False


def test_seedance_is_given_longer_than_the_default_to_finish():
    assert V._MODEL_TIMEOUT_OVERRIDES[V._SEEDANCE_FAST] == 900 > V._FAL_SUBSCRIBE_TIMEOUT_SECONDS


# ── a refused clip is retried unchanged before its sound is ever dropped ─────

AUDIO = "content_policy_violation: the generated audio was flagged as sensitive content"
IMAGE = ("content_policy_violation: image_url: The images or videos provided may contain likenesses of real people "
         "or other private information that cannot be processed.")


@pytest.fixture
def refusals(monkeypatch):
    """_submit_and_poll answers in order from `plan`: a refusal message, or None for success. Returns the generate_audio
    value of each request actually sent."""
    calls = []

    def configure(plan):
        plan = list(plan)

        async def fake_submit(model, arguments, timeout, scene_num, job_id=None):
            calls.append(arguments.get("generate_audio"))
            refusal = plan.pop(0)
            if refusal:
                raise RuntimeError(refusal)
            return {"video": {"url": "https://fal/clip.mp4"}}

        async def fake_store(result, model, seconds):
            return "https://cdn/clip.mp4", 0.1

        monkeypatch.setitem(sys.modules, "fal_client", types.ModuleType("fal_client"))
        monkeypatch.setattr(V.VideoGenerationService, "_submit_and_poll", staticmethod(fake_submit))
        monkeypatch.setattr(V.VideoGenerationService, "_store_clip", staticmethod(fake_store))
        monkeypatch.setattr(settings, "FAL_API_KEY", "k", raising=False)
        return calls
    return configure


def _scene_clip(model, dialogue):
    scene = {"scene_number": 1, "frame_image_url": "https://img/f.png", "duration_seconds": 5,
             "video_prompt": "A woman at her desk", "dialogue": dialogue}
    return _run(V.VideoGenerationService._generate_scene_fal(scene, model, V.DEFAULT_AVATAR_VOICE, "job1"))


def test_a_refused_start_image_is_retried_with_the_sound_on_not_made_silent(refusals):
    """The staging case: 'likenesses of real people' on image_url, then it passed on the retry."""
    calls = refusals([IMAGE, None])
    assert _scene_clip(V._SEEDANCE_FAST, "Jane has got this")[0] == "https://cdn/clip.mp4"
    assert calls == [True, True]


def test_a_refused_start_image_is_retried_even_when_nobody_speaks_in_the_scene(refusals):
    calls = refusals([IMAGE, None])
    _scene_clip(V._SEEDANCE_FAST, None)
    assert calls == [True, True]                      # the audio is not the problem, so it is not touched


def test_a_start_image_refused_three_times_finally_goes_ahead_without_sound(refusals):
    calls = refusals([IMAGE, IMAGE, IMAGE, None])
    _scene_clip(V._SEEDANCE_FAST, "Jane has got this")
    assert calls == [True, True, True, False]         # initial + 2 unchanged retries, then sound off as a last resort


def test_a_flagged_audio_track_on_a_spoken_scene_gets_one_unchanged_retry_first(refusals):
    calls = refusals([AUDIO, None])
    _scene_clip(V._SEEDANCE_FAST, "Jane has got this")
    assert calls == [True, True]


def test_a_flagged_audio_track_on_a_scene_nobody_speaks_in_drops_the_sound_straight_away(refusals):
    calls = refusals([AUDIO, None])
    _scene_clip(V._SEEDANCE_FAST, None)
    assert calls == [True, False]                     # unchanged behaviour


def test_a_retry_that_fails_for_another_reason_is_not_swallowed(refusals):
    refusals([IMAGE, "503 service unavailable"])
    with pytest.raises(RuntimeError):
        _scene_clip(V._SEEDANCE_FAST, "Jane has got this")


def test_a_refused_start_image_counts_as_worth_retrying_on_any_model():
    assert V.is_retryable(RuntimeError(IMAGE))
    assert not V.is_retryable(RuntimeError("content_policy_violation: prompt refused"))
