"""
Direct Nigerian-accented speech from YarnGPT.

Talking videos with a Nigerian voice used to CLONE a short reference sample with a
general-purpose voice-cloning model (fal-ai/f5-tts). A clone of a clone lost the accent and
added artefacts, and the cloner had to guess the sample's transcript. This asks YarnGPT
itself to speak each line, so the audio that reaches the lip-sync model is YarnGPT's own.

YarnGPT is an open model (MIT code, Apache-2.0 weights) with no hosted API, so it runs as a
small separate service (see services/yarngpt_tts/). Nothing here loads the model: this module
only calls that service. Unset YARNGPT_TTS_URL and `is_configured()` is False, and the caller
keeps using the clone path exactly as before.
"""
from typing import Dict, Optional

import httpx

from app.core.config import settings

# UI voice label -> YarnGPT speaker name. The labels are what the voice picker sends as
# `avatar_voice`; keep them in sync with NIGERIAN_VOICES in VideoStoryboardGenerator.tsx.
# "Idera (Nigerian)" and "Jude (Nigerian)" are the two voices that already existed.
YARNGPT_SPEAKERS: Dict[str, str] = {
    "Idera (Nigerian)": "idera",
    "Jude (Nigerian)": "jude",
    "Chinenye (Nigerian)": "chinenye",
    "Emma (Nigerian)": "emma",
    "Umar (Nigerian)": "umar",
    "Joke (Nigerian)": "joke",
    "Zainab (Nigerian)": "zainab",
    "Osagie (Nigerian)": "osagie",
    "Remi (Nigerian)": "remi",
    "Tayo (Nigerian)": "tayo",
}

# One synthesis can take a while on a CPU host; the video job itself runs for minutes, so a
# generous ceiling is fine. A timeout is retried once, then raised for the caller to handle.
_TIMEOUT_SECONDS = 240
_MAX_ATTEMPTS = 2


class YarnGPTError(RuntimeError):
    """The TTS service could not produce audio for this line."""


def is_configured() -> bool:
    return bool((getattr(settings, "YARNGPT_TTS_URL", None) or "").strip())


def speaker_for(voice_label: str) -> Optional[str]:
    return YARNGPT_SPEAKERS.get(voice_label)


async def synthesize(text: str, speaker: str, lang: str = "english") -> bytes:
    """WAV bytes (24 kHz) of `text` spoken by the YarnGPT `speaker`."""
    if not is_configured():
        raise YarnGPTError("YARNGPT_TTS_URL is not set")
    text = (text or "").strip()
    if not text:
        raise YarnGPTError("nothing to say")

    url = settings.YARNGPT_TTS_URL.rstrip("/") + "/synthesize"
    token = (getattr(settings, "YARNGPT_TTS_TOKEN", None) or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    last_error: Exception = YarnGPTError("no attempt made")
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    url, headers=headers, json={"text": text, "speaker": speaker, "lang": lang},
                )
            if resp.status_code == 200 and resp.content[:4] == b"RIFF":
                return resp.content
            # 4xx means this request is wrong (bad speaker, empty text); another try won't help.
            if 400 <= resp.status_code < 500:
                raise YarnGPTError(f"TTS service rejected the request ({resp.status_code}): {resp.text[:200]}")
            last_error = YarnGPTError(f"TTS service returned {resp.status_code}")
        except YarnGPTError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = YarnGPTError(f"TTS service unreachable or slow: {type(e).__name__}")
        print(f"[YarnGPT] attempt {attempt}/{_MAX_ATTEMPTS} failed: {last_error}", flush=True)
    raise last_error
