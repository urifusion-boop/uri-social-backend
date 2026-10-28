"""
Nigerian-voice talking scenes speak the line with YarnGPT directly, and only fall back to the
older voice-cloning path when the YarnGPT service is not configured or fails.
"""
import asyncio
import io
import sys
import types
import wave

import httpx
import pytest

from app.agents.social_media_manager.services import video_generation_service as V
from app.agents.social_media_manager.services import yarngpt_tts_service as Y
from app.core.config import settings


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _wav(seconds: float, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(seconds * rate))
    return buf.getvalue()


def _duration(raw: bytes) -> float:
    with wave.open(io.BytesIO(raw), "rb") as w:
        return w.getnframes() / w.getframerate()


# ── padding ─────────────────────────────────────────────────────────────────────

def test_a_short_clip_is_padded_up_to_the_lip_sync_minimum():
    padded = V._pad_wav_bytes(_wav(2.0))
    assert _duration(padded) == pytest.approx(5.2, abs=0.01)


def test_a_long_enough_clip_is_left_alone():
    assert V._pad_wav_bytes(_wav(6.0)) is None


def test_something_that_is_not_a_wav_is_left_alone():
    assert V._pad_wav_bytes(b"not audio") is None


# ── the client ──────────────────────────────────────────────────────────────────

class _FakeClient:
    """Stands in for httpx.AsyncClient; `responses` are consumed one per POST."""
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append((url, headers, json))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "YARNGPT_TTS_URL", "http://tts.internal/", raising=False)
    monkeypatch.setattr(settings, "YARNGPT_TTS_TOKEN", "secret", raising=False)


def test_not_configured_means_the_old_path_is_used(monkeypatch):
    monkeypatch.setattr(settings, "YARNGPT_TTS_URL", None, raising=False)
    assert Y.is_configured() is False
    with pytest.raises(Y.YarnGPTError):
        _run(Y.synthesize("hello", "idera"))


def test_the_client_sends_the_text_speaker_and_token(configured, monkeypatch):
    fake = _FakeClient([httpx.Response(200, content=_wav(1))])
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    out = _run(Y.synthesize("Hello there", "jude"))
    assert out[:4] == b"RIFF"
    url, headers, body = fake.calls[0]
    assert url == "http://tts.internal/synthesize"
    assert headers == {"Authorization": "Bearer secret"}
    assert body == {"text": "Hello there", "speaker": "jude", "lang": "english"}


def test_a_slow_service_is_retried_once_then_raises(configured, monkeypatch):
    fake = _FakeClient([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")])
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    with pytest.raises(Y.YarnGPTError):
        _run(Y.synthesize("Hello", "idera"))
    assert len(fake.calls) == 2


def test_a_server_error_is_retried_and_can_recover(configured, monkeypatch):
    fake = _FakeClient([httpx.Response(503, text="loading"), httpx.Response(200, content=_wav(1))])
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    assert _run(Y.synthesize("Hello", "idera"))[:4] == b"RIFF"


def test_a_rejected_request_is_not_retried(configured, monkeypatch):
    fake = _FakeClient([httpx.Response(400, text="unknown speaker")])
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    with pytest.raises(Y.YarnGPTError):
        _run(Y.synthesize("Hello", "nobody"))
    assert len(fake.calls) == 1


# ── routing inside a talking scene ──────────────────────────────────────────────

class _Stop(BaseException):
    """Raised by the fake fal call so a test stops right after the call it cares about.
    A BaseException on purpose: the code under test swallows ordinary exceptions."""


@pytest.fixture
def scene_env(monkeypatch):
    calls = []

    async def fake_submit(model, arguments, timeout, scene_num, job_id=None):
        calls.append((model, arguments))
        if model == "fal-ai/f5-tts":
            return {"audio_url": {"url": "https://fal/cloned.wav"}}
        raise _Stop()

    uploads = []

    async def fake_upload(data, folder="", resource_type=""):
        uploads.append(data)
        return "https://cdn/yarn.wav"

    async def fake_pad_url(url, min_seconds=5.2):
        return url

    monkeypatch.setitem(sys.modules, "fal_client", types.ModuleType("fal_client"))
    monkeypatch.setattr(V.VideoGenerationService, "_submit_and_poll", staticmethod(fake_submit))
    monkeypatch.setattr(V, "upload_bytes", fake_upload)
    monkeypatch.setattr(V, "_pad_wav_silence", fake_pad_url)
    monkeypatch.setattr(settings, "FAL_API_KEY", "k", raising=False)
    return calls, uploads


def _scene():
    return {"scene_number": 1, "frame_image_url": "https://img/f.png", "duration_seconds": 5,
            "dialogue": "Abeg, if you are tired of posting every single day, I have good news for you."}


def _gen(voice):
    return V.VideoGenerationService._generate_scene_fal(_scene(), V._AVATAR_TALKING, voice, "job1")


def test_a_yarngpt_voice_goes_straight_to_lip_sync_with_no_cloning(scene_env, monkeypatch):
    calls, uploads = scene_env
    monkeypatch.setattr(Y, "is_configured", lambda: True)

    async def fake_synth(text, speaker, lang="english"):
        assert speaker == "chinenye"
        return _wav(6.0)

    monkeypatch.setattr(Y, "synthesize", fake_synth)
    with pytest.raises(_Stop):
        _run(_gen("Chinenye (Nigerian)"))

    models = [m for m, _ in calls]
    assert "fal-ai/f5-tts" not in models
    assert models == [V._NG_LIPSYNC]
    assert calls[0][1]["audio_url"] == "https://cdn/yarn.wav"
    assert len(uploads) == 1


def test_a_failed_yarngpt_call_falls_back_to_the_clone_for_idera(scene_env, monkeypatch):
    calls, _ = scene_env
    monkeypatch.setattr(Y, "is_configured", lambda: True)

    async def boom(text, speaker, lang="english"):
        raise Y.YarnGPTError("down")

    monkeypatch.setattr(Y, "synthesize", boom)
    with pytest.raises(_Stop):
        _run(_gen("Idera (Nigerian)"))
    assert [m for m, _ in calls] == ["fal-ai/f5-tts", V._NG_LIPSYNC]
    assert calls[1][1]["audio_url"] == "https://fal/cloned.wav"


def test_not_configured_keeps_the_clone_path_exactly_as_before(scene_env, monkeypatch):
    calls, _ = scene_env
    monkeypatch.setattr(Y, "is_configured", lambda: False)
    with pytest.raises(_Stop):
        _run(_gen("Jude (Nigerian)"))
    assert [m for m, _ in calls] == ["fal-ai/f5-tts", V._NG_LIPSYNC]


def test_a_voice_with_no_reference_clip_cannot_be_cloned_so_it_raises(scene_env, monkeypatch):
    # run_job catches this and falls back to the outcome's paired model.
    monkeypatch.setattr(Y, "is_configured", lambda: False)
    with pytest.raises(ValueError):
        _run(_gen("Chinenye (Nigerian)"))


def test_every_ui_voice_label_maps_to_a_real_speaker_name():
    assert Y.speaker_for("Idera (Nigerian)") == "idera"
    assert Y.speaker_for("Tayo (Nigerian)") == "tayo"
    assert Y.speaker_for("Sarah") is None
    assert set(V.NIGERIAN_VOICE_REFS) <= set(Y.YARNGPT_SPEAKERS)


def test_a_scene_that_drops_to_the_default_voice_says_so(scene_env, monkeypatch):
    # The Nigerian lip-sync call failing used to switch that scene to a Western voice with only
    # a server log line; the customer got a mixed-voice video and no explanation.
    monkeypatch.setattr(Y, "is_configured", lambda: True)

    async def fake_synth(text, speaker, lang="english"):
        return _wav(6.0)

    monkeypatch.setattr(Y, "synthesize", fake_synth)
    seen = []

    async def submit(model, arguments, timeout, scene_num, job_id=None):
        seen.append(model)
        if model == V._NG_LIPSYNC:
            raise RuntimeError("audio rejected")
        raise _Stop()

    monkeypatch.setattr(V.VideoGenerationService, "_submit_and_poll", staticmethod(submit))
    notes = {}
    with pytest.raises(_Stop):
        _run(V.VideoGenerationService._generate_scene_fal(
            _scene(), V._AVATAR_TALKING, "Chinenye (Nigerian)", "job1", notes=notes))

    assert seen == [V._NG_LIPSYNC, V._AVATAR_TALKING]
    assert "Chinenye (Nigerian)" in notes["voice_fallback"]
    assert "default voice" in notes["voice_fallback"]


def test_a_scene_that_keeps_its_voice_adds_no_warning(scene_env, monkeypatch):
    monkeypatch.setattr(Y, "is_configured", lambda: False)
    notes = {}
    with pytest.raises(_Stop):
        _run(V.VideoGenerationService._generate_scene_fal(
            _scene(), V._AVATAR_TALKING, "Jude (Nigerian)", "job1", notes=notes))
    assert notes == {}


# ── preview generation ──────────────────────────────────────────────────────────

def test_the_preview_script_covers_every_voice_and_names_files_by_speaker(tmp_path, monkeypatch):
    from services.yarngpt_tts import generate_previews as G

    seen = []

    def fake_post(url, headers=None, timeout=None, json=None):
        seen.append(json["speaker"])
        return httpx.Response(200, content=_wav(6.0))

    monkeypatch.setattr(G.httpx, "post", fake_post)
    assert G.main(["--url", "http://svc", "--token", "t", "--out", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(f"{s}.wav" for s in Y.YARNGPT_SPEAKERS.values())
    assert sorted(seen) == sorted(Y.YARNGPT_SPEAKERS.values())


def test_a_failed_voice_makes_the_script_exit_nonzero(tmp_path, monkeypatch):
    from services.yarngpt_tts import generate_previews as G

    def fake_post(url, headers=None, timeout=None, json=None):
        return httpx.Response(500, text="boom") if json["speaker"] == "tayo" else httpx.Response(200, content=_wav(6.0))

    monkeypatch.setattr(G.httpx, "post", fake_post)
    assert G.main(["--url", "http://svc", "--out", str(tmp_path)]) == 1
    assert not (tmp_path / "tayo.wav").exists()
