"""
YarnGPT speech service: POST /synthesize {text, speaker, lang} -> 24 kHz WAV.

YarnGPT (https://github.com/saheedniyi02/yarngpt, MIT; weights saheedniyi/YarnGPT2, Apache-2.0)
is an open model with no hosted API, so this wraps it for the video pipeline. Runs on CPU
(0.4B parameters) or GPU. One synthesis at a time per process: the model is not safe to share
across threads, and a second request just waits its turn.

Environment:
  TTS_TOKEN               shared secret; requests must send "Authorization: Bearer <token>".
                          Unset = no auth (local testing only).
  YARNGPT_MODEL_ID        default saheedniyi/YarnGPT2
  WAVTOKENIZER_CONFIG     path to the WavTokenizer .yaml
  WAVTOKENIZER_CKPT       path to wavtokenizer_large_speech_320_24k.ckpt
"""
import io
import os
import threading
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from .normalize import normalize_for_speech

MODEL_ID = os.getenv("YARNGPT_MODEL_ID", "saheedniyi/YarnGPT2")
WAV_CONFIG = os.getenv("WAVTOKENIZER_CONFIG", "/models/wavtokenizer.yaml")
WAV_CKPT = os.getenv("WAVTOKENIZER_CKPT", "/models/wavtokenizer.ckpt")
TOKEN = os.getenv("TTS_TOKEN", "").strip()

ENGLISH_SPEAKERS = {
    "idera", "chinenye", "jude", "emma", "umar", "joke", "zainab", "osagie", "remi", "tayo",
    "onye", "regina", "azeez", "saheed",
}
SAMPLE_RATE = 24000

app = FastAPI(title="YarnGPT TTS")
_state: dict = {"ready": False, "error": None}
_gen_lock = threading.Lock()


class SynthRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1500)
    speaker: str = "idera"
    lang: str = "english"


def _load() -> None:
    try:
        import torch  # noqa: F401
        from transformers import AutoModelForCausalLM
        from yarngpt.audiotokenizer import AudioTokenizerV2

        tokenizer = AudioTokenizerV2(MODEL_ID, WAV_CKPT, WAV_CONFIG)
        model = AutoModelForCausalLM.from_pretrained(MODEL_ID, torch_dtype="auto").to(tokenizer.device)
        model.eval()
        _state.update(tokenizer=tokenizer, model=model, ready=True)
        print(f"[yarngpt-tts] model ready on {tokenizer.device}", flush=True)
    except Exception as e:  # surfaced on /healthz instead of crashing the container silently
        _state["error"] = f"{type(e).__name__}: {e}"
        print(f"[yarngpt-tts] load failed: {_state['error']}", flush=True)


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_load, daemon=True).start()


@app.get("/healthz")
def healthz() -> dict:
    return {"ready": _state["ready"], "error": _state["error"], "model": MODEL_ID}


def _authorize(authorization: Optional[str]) -> None:
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


def synthesize_wav(text: str, speaker: str, lang: str) -> bytes:
    import torchaudio

    tokenizer, model = _state["tokenizer"], _state["model"]
    spoken = normalize_for_speech(text)
    if not spoken:
        raise ValueError("nothing speakable in that text")
    with _gen_lock:
        prompt = tokenizer.create_prompt(spoken, lang=lang, speaker_name=speaker)
        input_ids = tokenizer.tokenize_prompt(prompt)
        output = model.generate(
            input_ids=input_ids, temperature=0.1, repetition_penalty=1.1, max_length=4000,
        )
        codes = tokenizer.get_codes(output)
        if not codes:
            raise RuntimeError("the model produced no audio")
        audio = tokenizer.get_audio(codes)
    buf = io.BytesIO()
    torchaudio.save(buf, audio, SAMPLE_RATE, format="wav")
    return buf.getvalue()


@app.post("/synthesize")
def synthesize(req: SynthRequest, authorization: Optional[str] = Header(None)) -> Response:
    _authorize(authorization)
    if not _state["ready"]:
        raise HTTPException(status_code=503, detail=_state["error"] or "model is still loading")
    if req.lang != "english":
        raise HTTPException(status_code=400, detail="only english is enabled")
    if req.speaker not in ENGLISH_SPEAKERS:
        raise HTTPException(status_code=400, detail=f"unknown speaker {req.speaker!r}")
    started = time.monotonic()
    try:
        wav = synthesize_wav(req.text, req.speaker, req.lang)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[yarngpt-tts] synthesis failed: {type(e).__name__}: {e}", flush=True)
        raise HTTPException(status_code=500, detail="synthesis failed")
    print(f"[yarngpt-tts] {req.speaker}: {len(req.text.split())} words in {time.monotonic() - started:.1f}s", flush=True)
    return Response(content=wav, media_type="audio/wav")
