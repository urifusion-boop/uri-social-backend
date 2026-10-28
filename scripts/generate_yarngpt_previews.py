"""
Make the voice-picker preview clips from YarnGPT's hosted API.

Customers press a play button next to each Nigerian voice before choosing it. Those clips must be
YarnGPT's own output (what the video will actually use), so they are made through the same API the
videos use and saved as static files for the frontend, together with a manifest.json that tells the
picker which voices have a preview. The picker shows exactly the voices in that manifest.

Standalone on purpose: it needs only `httpx`, no backend settings, so anyone with a YarnGPT key can
run it from any checkout. The key is read from the environment and never written anywhere.

    YARNGPT_API_KEY=sk_live_... python scripts/generate_yarngpt_previews.py \
        --voices idera,femi --out ../uri-social-frontent-next/public/voice-previews

--voices takes the names exactly as YarnGPT lists them (shown at the start of the run).

Each voice costs one short synthesis from the YarnGPT account's credits. Voices
YarnGPT does not offer, or that fail, are listed at the end and left out of the manifest; the exit
code is 1 if any were left out, so a half-finished set is never mistaken for a full one.
"""
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

import httpx

DEFAULT_API_URL = "https://api.yarngpt.ai"

NIGERIAN_SUFFIX = " (Nigerian)"   # the picker's label is "<Name> (Nigerian)"; keep in sync with the backend


def preview_line(display_name: str) -> str:
    # About 15 words: long enough to hear the voice, and the length the video scenes aim for.
    return (f"Hello, my name is {display_name}. I help small businesses in Nigeria "
            f"post, reply and run ads without stress.")


def filename_for(speaker: str) -> str:
    return f"{speaker}.wav"


def voice_names(payload) -> list:
    """Voice IDs from GET /voices, tolerant of the response shape (a list, or an object holding one)."""
    items = payload
    if isinstance(payload, dict):
        for key in ("voices", "data", "items", "results"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    names = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for key in ("id", "name", "voice", "voice_id"):
                if isinstance(item.get(key), str):
                    names.append(item[key])
                    break
    return names


def synthesize(client: httpx.Client, base: str, key: str, voice: str, text: str,
               deadline_seconds: int = 240) -> bytes:
    headers = {"Authorization": f"Bearer {key}"}
    resp = client.post(
        f"{base}/api/v1/tts",
        headers={**headers, "Idempotency-Key": f"uri-preview-{uuid.uuid4().hex}"},
        json={"text": text, "voice": voice, "output_format": "wav"},
    )
    if resp.status_code != 202:
        raise RuntimeError(f"refused ({resp.status_code}): {resp.text[:200]}")
    job_id = resp.json()["job_id"]

    deadline, delay = time.monotonic() + deadline_seconds, 1.0
    while time.monotonic() < deadline:
        time.sleep(delay)
        delay = min(4.0, delay * 1.5)
        status = client.get(f"{base}/api/v1/status/{job_id}", headers=headers)
        if status.status_code >= 500:
            continue
        body = status.json()
        if body.get("status") == "completed":
            audio = client.get(body["audio_url"])
            if audio.status_code != 200 or audio.content[:4] != b"RIFF":
                raise RuntimeError("the audio file was not a WAV")
            return audio.content
        if body.get("status") == "failed":
            raise RuntimeError(body.get("user_message") or "the job failed")
    raise RuntimeError("timed out waiting for the job")


def run(client: httpx.Client, base: str, key: str, out: Path, wanted: list) -> int:
    out.mkdir(parents=True, exist_ok=True)
    listing = client.get(f"{base}/api/v1/voices", headers={"Authorization": f"Bearer {key}"})
    offered = voice_names(listing.json()) if listing.status_code == 200 else []
    if offered:
        print(f"YarnGPT offers {len(offered)} voices: {', '.join(offered)}\n")
    else:
        print("Could not read YarnGPT's voice list; trying each voice by name.\n")

    manifest, left_out = [], []
    for name in wanted:
        speaker = name.strip().lower()
        label = f"{speaker.capitalize()}{NIGERIAN_SUFFIX}"
        voice: Optional[str] = next((v for v in offered if v.lower() == speaker), None) if offered else speaker
        if voice is None:
            left_out.append(label)
            print(f"MISSING {label:22s} YarnGPT does not offer a voice called {speaker!r}")
            continue
        try:
            audio = synthesize(client, base, key, voice, preview_line(speaker.capitalize()))
            (out / filename_for(speaker)).write_bytes(audio)
            manifest.append({"label": label, "speaker": speaker, "file": filename_for(speaker)})
            print(f"ok      {label:22s} -> {filename_for(speaker)} ({len(audio) // 1024} KB)")
        except Exception as e:
            left_out.append(label)
            print(f"FAILED  {label:22s} {e}")

    (out / "manifest.json").write_text(json.dumps({"voices": manifest}, indent=2) + "\n")
    print(f"\nWrote {len(manifest)} preview(s) and manifest.json to {out}")
    if left_out:
        print(f"Left out ({len(left_out)}): {', '.join(left_out)}")
        return 1
    return 0


def main(argv=None, client: Optional[httpx.Client] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--voices", required=True, help="comma-separated YarnGPT voice names, e.g. idera,femi")
    args = ap.parse_args(argv)
    wanted = [v for v in (x.strip() for x in args.voices.split(",")) if v]
    if not wanted:
        print("Name at least one voice with --voices.")
        return 2
    key = os.environ.get("YARNGPT_API_KEY", "").strip()
    if not key:
        print("Set YARNGPT_API_KEY first (the key from the Account page at yarngpt.ai).")
        return 2
    base = (os.environ.get("YARNGPT_API_URL") or DEFAULT_API_URL).rstrip("/")
    with (client or httpx.Client(timeout=60)) as c:
        return run(c, base, key, Path(args.out), wanted)


if __name__ == "__main__":
    sys.exit(main())
