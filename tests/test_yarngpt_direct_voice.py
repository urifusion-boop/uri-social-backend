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


# ── the client (YarnGPT's job-based hosted API) ──────────────────────────────────

class _Api:
    """Stands in for httpx.AsyncClient, routing by URL path. `post_replies` and `status_replies` are
    consumed one per call; a ready-made httpx.Response or an exception."""
    def __init__(self, voices=None, post_replies=None, status_replies=None, audio=None):
        self.voices = voices
        self.post_replies = list(post_replies or [])
        self.status_replies = list(status_replies or [])
        self.audio = audio if audio is not None else httpx.Response(200, content=_wav(6.0))
        self.posts, self.gets = [], []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, headers, json))
        item = self.post_replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        if url.endswith("/api/v1/voices"):
            if self.voices is None:
                return httpx.Response(503, text="down")
            return httpx.Response(200, json=self.voices)
        if "/api/v1/status/" in url:
            item = self.status_replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self.audio          # the signed audio_url


def _accepted(job="job1"):
    return httpx.Response(202, json={"job_id": job, "status": "queued"})


def _done(url="https://signed/audio.wav"):
    return httpx.Response(200, json={"status": "completed", "audio_url": url})


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "YARNGPT_API_KEY", "key-123", raising=False)
    monkeypatch.setattr(settings, "YARNGPT_API_URL", "https://api.yarngpt.test/", raising=False)
    monkeypatch.setitem(Y._voice_cache, "voices", None)
    monkeypatch.setitem(Y._voice_cache, "at", 0.0)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(Y.asyncio, "sleep", no_sleep)

    def install(**kw):
        fake = _Api(**kw)
        monkeypatch.setattr(httpx, "AsyncClient", fake)
        return fake

    return install


def test_not_configured_means_the_old_path_is_used(monkeypatch):
    monkeypatch.setattr(settings, "YARNGPT_API_KEY", None, raising=False)
    assert Y.is_configured() is False
    with pytest.raises(Y.YarnGPTError):
        _run(Y.synthesize("hello", "idera"))


def test_a_line_is_queued_polled_and_downloaded(api):
    fake = api(voices=["idera", "jude"], post_replies=[_accepted()],
               status_replies=[httpx.Response(200, json={"status": "queued"}), _done()])
    out = _run(Y.synthesize("Pay ₦31,000 today & relax", "idera"))

    assert out[:4] == b"RIFF"
    url, headers, body = fake.posts[0]
    assert url == "https://api.yarngpt.test/api/v1/tts"
    assert headers["Authorization"] == "Bearer key-123"
    assert headers["Idempotency-Key"].startswith("uri-")
    assert body == {"text": "Pay 31000 naira today and relax", "voice": "idera", "output_format": "wav"}
    assert "target_language" not in body        # that would translate the line


def test_the_voice_is_the_exact_id_yarngpt_lists(api):
    fake = api(voices={"voices": [{"id": "Idera"}, {"id": "Jude"}]}, post_replies=[_accepted()], status_replies=[_done()])
    _run(Y.synthesize("Hello", "idera"))
    assert fake.posts[0][2]["voice"] == "Idera"


def test_a_voice_yarngpt_no_longer_offers_is_not_requested(api):
    fake = api(voices=["jude", "umar"], post_replies=[])
    with pytest.raises(Y.YarnGPTError, match="no voice called"):
        _run(Y.synthesize("Hello", "idera"))
    assert fake.posts == []


def test_if_the_voice_list_cannot_be_read_the_name_is_sent_as_it_is(api):
    fake = api(voices=None, post_replies=[_accepted()], status_replies=[_done()])
    _run(Y.synthesize("Hello", "umar"))
    assert fake.posts[0][2]["voice"] == "umar"


def test_out_of_credits_is_refused_once_and_not_retried(api):
    refusal = httpx.Response(402, json={"error": {"code": "QUOTA_EXCEEDED", "user_message": "Top up your credits.", "trace_id": "t1"}})
    fake = api(voices=["idera"], post_replies=[refusal])
    with pytest.raises(Y.YarnGPTError, match="QUOTA_EXCEEDED"):
        _run(Y.synthesize("Hello", "idera"))
    assert len(fake.posts) == 1


def test_a_server_error_is_retried_with_the_same_idempotency_key(api):
    fake = api(voices=["idera"], post_replies=[httpx.Response(503, text="busy"), _accepted()], status_replies=[_done()])
    assert _run(Y.synthesize("Hello", "idera"))[:4] == b"RIFF"
    assert len(fake.posts) == 2
    assert fake.posts[0][1]["Idempotency-Key"] == fake.posts[1][1]["Idempotency-Key"]


def test_a_failed_job_raises_with_yarngpts_own_reason(api):
    api(voices=["idera"], post_replies=[_accepted()],
        status_replies=[httpx.Response(200, json={"status": "failed", "user_message": "Text could not be spoken."})])
    with pytest.raises(Y.YarnGPTError, match="could not be spoken"):
        _run(Y.synthesize("Hello", "idera"))


def test_a_job_that_never_finishes_gives_up(api, monkeypatch):
    monkeypatch.setattr(Y, "_JOB_DEADLINE_SECONDS", 0)
    api(voices=["idera"], post_replies=[_accepted()], status_replies=[])
    with pytest.raises(Y.YarnGPTError, match="did not finish"):
        _run(Y.synthesize("Hello", "idera"))


def test_audio_that_is_not_a_wav_is_rejected(api):
    api(voices=["idera"], post_replies=[_accepted()], status_replies=[_done()],
        audio=httpx.Response(200, content=b"ID3 not a wav"))
    with pytest.raises(Y.YarnGPTError, match="not a WAV"):
        _run(Y.synthesize("Hello", "idera"))


def test_a_flaky_poll_is_not_a_failed_job(api):
    api(voices=["idera"], post_replies=[_accepted()],
        status_replies=[httpx.ReadTimeout("slow"), httpx.Response(502, text="bad gateway"), _done()])
    assert _run(Y.synthesize("Hello", "idera"))[:4] == b"RIFF"


@pytest.mark.parametrize("payload,expected", [
    (["idera", "jude"], ["idera", "jude"]),
    ({"voices": [{"id": "Idera"}, {"name": "Jude"}]}, ["Idera", "Jude"]),
    ({"data": [{"voice": "Umar", "default": True}]}, ["Umar"]),
    ({"unexpected": 1}, []),
])
def test_the_voice_list_is_read_whatever_shape_it_comes_in(payload, expected):
    assert Y._voice_names(payload) == expected


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


# ── preview generation (scripts/generate_yarngpt_previews.py) ───────────────────

import json

from scripts import generate_yarngpt_previews as G


def _api_transport(offered, fail_voice=None):
    """A fake YarnGPT: lists `offered` voices; each job completes with a WAV unless it is `fail_voice`."""
    jobs = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/voices":
            return httpx.Response(200, json={"voices": [{"id": v} for v in offered]})
        if path == "/api/v1/tts":
            voice = json.loads(request.content)["voice"]
            jobs[f"job-{voice}"] = voice
            return httpx.Response(202, json={"job_id": f"job-{voice}", "status": "queued"})
        if path.startswith("/api/v1/status/"):
            voice = jobs[path.rsplit("/", 1)[1]]
            if voice == fail_voice:
                return httpx.Response(200, json={"status": "failed", "user_message": "could not speak"})
            return httpx.Response(200, json={"status": "completed", "audio_url": "https://signed.test/a.wav"})
        if request.url.host == "signed.test":
            return httpx.Response(200, content=_wav(6.0))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def preview_env(monkeypatch):
    monkeypatch.setenv("YARNGPT_API_KEY", "sk_live_test")
    monkeypatch.setenv("YARNGPT_API_URL", "https://api.yarngpt.test")
    monkeypatch.setattr(G.time, "sleep", lambda _: None)


def _run_script(out, transport):
    return G.main(["--out", str(out)], client=httpx.Client(transport=transport))


def test_the_script_and_the_backend_agree_on_which_voices_exist():
    assert G.LABELS == Y.YARNGPT_SPEAKERS


def test_every_offered_voice_gets_a_preview_and_a_manifest_entry(tmp_path, preview_env):
    assert _run_script(tmp_path, _api_transport(list(Y.YARNGPT_SPEAKERS.values()))) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert [v["label"] for v in manifest["voices"]] == list(Y.YARNGPT_SPEAKERS)
    for v in manifest["voices"]:
        assert (tmp_path / v["file"]).read_bytes()[:4] == b"RIFF"


def test_a_voice_yarngpt_does_not_offer_is_left_out_and_reported(tmp_path, preview_env):
    offered = [v for v in Y.YARNGPT_SPEAKERS.values() if v != "tayo"]
    assert _run_script(tmp_path, _api_transport(offered)) == 1
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert "Tayo (Nigerian)" not in [v["label"] for v in manifest["voices"]]
    assert len(manifest["voices"]) == 9
    assert not (tmp_path / "tayo.wav").exists()


def test_a_voice_whose_job_fails_is_left_out_but_the_rest_are_kept(tmp_path, preview_env):
    assert _run_script(tmp_path, _api_transport(list(Y.YARNGPT_SPEAKERS.values()), fail_voice="emma")) == 1
    labels = [v["label"] for v in json.loads((tmp_path / "manifest.json").read_text())["voices"]]
    assert "Emma (Nigerian)" not in labels and len(labels) == 9


def test_voice_matching_ignores_case(tmp_path, preview_env):
    assert _run_script(tmp_path, _api_transport([v.capitalize() for v in Y.YARNGPT_SPEAKERS.values()])) == 0


def test_without_a_key_the_script_stops_before_spending_anything(tmp_path, monkeypatch):
    monkeypatch.delenv("YARNGPT_API_KEY", raising=False)
    assert G.main(["--out", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []
