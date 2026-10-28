"""
Direct Nigerian-accented speech from YarnGPT's hosted API (https://yarngpt.ai/api-docs).

Talking videos with a Nigerian voice used to CLONE a short reference sample with a
general-purpose voice-cloning model (fal-ai/f5-tts). A clone of a clone lost the accent and
added artefacts, and the cloner had to guess the sample's transcript. This asks YarnGPT
itself to speak each line, so the audio that reaches the lip-sync model is YarnGPT's own.

The API is job-based: POST /api/v1/tts queues a job and returns a job_id, then
GET /api/v1/status/{job_id} is polled until the job completes and carries a signed audio_url.
Every call spends credits from the YarnGPT account the key belongs to, and there is no sandbox.

Unset YARNGPT_API_KEY and `is_configured()` is False: the caller keeps using the clone path
exactly as before. Every failure raises YarnGPTError so the caller can fall back.
"""
import asyncio
import re
import time
import uuid
from typing import Dict, List, Optional

import httpx

from app.agents.social_media_manager.services.yarngpt_text import normalize_for_speech
from app.core.config import settings

# The picker offers voices as "<Name> (Nigerian)". The name is the YarnGPT voice; the voices on
# offer are the ones that have a preview clip (see the frontend's /voice-previews/manifest.json),
# so adding or renaming a voice needs no change here. YarnGPT says voice IDs can change, so the
# real ID is always looked up from /api/v1/voices (see _resolve_voice), never assumed.
NIGERIAN_SUFFIX = " (Nigerian)"
_NAME = re.compile(r"^[A-Za-z][A-Za-z ]{0,29}$")

_REQUEST_TIMEOUT = 30          # one HTTP call
_JOB_DEADLINE_SECONDS = 240    # queue + synthesis, start to finish
_POLL_START, _POLL_MAX = 1.0, 4.0
_MAX_SERVER_RETRIES = 2        # on 500/502/503/504, same Idempotency-Key so it never bills twice
_VOICE_CACHE_SECONDS = 3600

_voice_cache: Dict[str, object] = {"at": 0.0, "voices": None}


class YarnGPTError(RuntimeError):
    """YarnGPT could not produce audio for this line."""


def is_configured() -> bool:
    return bool((getattr(settings, "YARNGPT_API_KEY", None) or "").strip())


def speaker_for(voice_label: str) -> Optional[str]:
    """The YarnGPT voice name behind a picker label ("Femi (Nigerian)" -> "femi"), or None if the
    label is not a Nigerian voice at all (a default voice like "Sarah" is not ours to route)."""
    if not isinstance(voice_label, str) or not voice_label.endswith(NIGERIAN_SUFFIX):
        return None
    name = voice_label[: -len(NIGERIAN_SUFFIX)].strip()
    return name.lower() if _NAME.match(name) else None


def _base_url() -> str:
    return (getattr(settings, "YARNGPT_API_URL", None) or "https://api.yarngpt.ai").rstrip("/")


def _headers(extra: Optional[dict] = None) -> dict:
    return {"Authorization": f"Bearer {settings.YARNGPT_API_KEY.strip()}", **(extra or {})}


def _error_text(resp: httpx.Response) -> str:
    """The API's own message (error.user_message / error.message) plus its trace id, for the log."""
    try:
        err = (resp.json() or {}).get("error") or {}
        msg = err.get("user_message") or err.get("message") or resp.text[:160]
        code = err.get("code") or resp.status_code
        return f"{code}: {msg} (trace {err.get('trace_id') or resp.headers.get('X-Request-Id', '-')})"
    except Exception:
        return f"{resp.status_code}: {resp.text[:160]}"


def _voice_names(payload) -> List[str]:
    """Voice IDs from GET /voices, tolerant of the response shape (a list, or an object holding one)."""
    items = payload
    if isinstance(payload, dict):
        for key in ("voices", "data", "items", "results"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    names: List[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for key in ("id", "name", "voice", "voice_id"):
                if isinstance(item.get(key), str):
                    names.append(item[key])
                    break
    return names


async def _resolve_voice(client: httpx.AsyncClient, wanted: str) -> str:
    """The exact, case-sensitive voice ID YarnGPT knows `wanted` by.

    The list is cached for an hour. If the list can't be read at all, `wanted` is sent as it is
    (the docs' own examples use plain names like "umar"); if the list was read and the voice is
    not in it, that is a real "this voice is gone" and raises."""
    now = time.monotonic()
    voices = _voice_cache["voices"]
    if voices is None or now - float(_voice_cache["at"]) > _VOICE_CACHE_SECONDS:
        try:
            resp = await client.get(f"{_base_url()}/api/v1/voices", headers=_headers(), timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 200:
                voices = _voice_names(resp.json())
                _voice_cache.update(at=now, voices=voices)
        except (httpx.HTTPError, ValueError):
            voices = None
    if not voices:
        return wanted
    for name in voices:
        if name.lower() == wanted.lower():
            return name
    raise YarnGPTError(f"YarnGPT has no voice called {wanted!r} (it offers: {', '.join(voices[:12])})")


async def synthesize(text: str, speaker: str, lang: str = "english") -> bytes:
    """WAV bytes of `text` spoken by the YarnGPT voice for `speaker`.

    `lang` is accepted for the caller's convenience; YarnGPT picks the language from the voice and
    the text, and `target_language` (which translates) is deliberately never sent."""
    if not is_configured():
        raise YarnGPTError("YARNGPT_API_KEY is not set")
    spoken = normalize_for_speech(text)
    if not spoken:
        raise YarnGPTError("nothing to say")

    idem_key = f"uri-{uuid.uuid4().hex}"   # one key per call: a retry after a 5xx re-uses it
    async with httpx.AsyncClient() as client:
        voice = await _resolve_voice(client, speaker)
        job_id = await _queue_job(client, spoken, voice, idem_key)
        audio_url = await _wait_for_audio(client, job_id)
        try:
            audio = await client.get(audio_url, timeout=60)
        except httpx.HTTPError as e:
            raise YarnGPTError(f"could not download the audio: {type(e).__name__}")
        if audio.status_code != 200 or audio.content[:4] != b"RIFF":
            raise YarnGPTError(f"the audio file was not a WAV (HTTP {audio.status_code})")
        return audio.content


async def _queue_job(client: httpx.AsyncClient, text: str, voice: str, idem_key: str) -> str:
    last: Exception = YarnGPTError("no attempt made")
    for attempt in range(1, _MAX_SERVER_RETRIES + 2):
        try:
            resp = await client.post(
                f"{_base_url()}/api/v1/tts",
                headers=_headers({"Idempotency-Key": idem_key, "Content-Type": "application/json"}),
                json={"text": text, "voice": voice, "output_format": "wav"},
                timeout=_REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as e:
            last = YarnGPTError(f"YarnGPT unreachable: {type(e).__name__}")
        else:
            if resp.status_code == 202:
                job_id = (resp.json() or {}).get("job_id")
                if not job_id:
                    raise YarnGPTError("YarnGPT accepted the job but returned no job_id")
                return job_id
            if resp.status_code == 409:
                # The same key is still running: the first attempt did reach them.
                await asyncio.sleep(2)
                last = YarnGPTError(f"YarnGPT: {_error_text(resp)}")
            elif resp.status_code >= 500:
                last = YarnGPTError(f"YarnGPT server error: {_error_text(resp)}")
            else:
                # 401 bad key, 402 out of credits, 404 voice, 422/400 bad input, 429 rate limit:
                # another try with the same request will not change the answer.
                raise YarnGPTError(f"YarnGPT refused the request: {_error_text(resp)}")
        print(f"[YarnGPT] queue attempt {attempt} failed: {last}", flush=True)
    raise last


async def _wait_for_audio(client: httpx.AsyncClient, job_id: str) -> str:
    deadline = time.monotonic() + _JOB_DEADLINE_SECONDS
    delay = _POLL_START
    while time.monotonic() < deadline:
        await asyncio.sleep(delay)
        delay = min(_POLL_MAX, delay * 1.5)
        try:
            resp = await client.get(f"{_base_url()}/api/v1/status/{job_id}", headers=_headers(),
                                    timeout=_REQUEST_TIMEOUT)
        except httpx.HTTPError:
            continue        # one failed poll is not a failed job
        if resp.status_code >= 500:
            continue
        if resp.status_code != 200:
            raise YarnGPTError(f"YarnGPT status check failed: {_error_text(resp)}")
        body = resp.json() or {}
        status = (body.get("status") or "").lower()
        if status == "completed":
            if not body.get("audio_url"):
                raise YarnGPTError("YarnGPT finished the job but returned no audio_url")
            return body["audio_url"]
        if status == "failed":
            raise YarnGPTError(f"YarnGPT could not synthesize this line: {body.get('user_message') or 'no reason given'}")
    raise YarnGPTError(f"YarnGPT did not finish within {_JOB_DEADLINE_SECONDS}s")
