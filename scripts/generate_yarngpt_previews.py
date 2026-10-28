"""
Make the voice-picker preview clips from YarnGPT's hosted API.

Customers press a play button next to each Nigerian voice before choosing it. Those clips must be
YarnGPT's own output (what the video will actually use), so they are generated through the same
client the videos use and saved as static files for the frontend. Each voice costs one short
synthesis from the YarnGPT account's credits.

    YARNGPT_API_KEY=... python -m scripts.generate_yarngpt_previews \
        --out ../uri-social-frontent-next/public/voice-previews

Writes one <voice>.wav per voice (idera.wav, jude.wav, ...). Voices that fail are listed at the end
and the exit code is 1, so a half-finished set is never mistaken for a full one.
"""
import argparse
import asyncio
import sys
from pathlib import Path

from app.agents.social_media_manager.services import yarngpt_tts_service as Y


def preview_line(display_name: str) -> str:
    # About 15 words: long enough to hear the voice, and the length the video scenes aim for.
    return (f"Hello, my name is {display_name}. I help small businesses in Nigeria "
            f"post, reply and run ads without stress.")


def filename_for(speaker: str) -> str:
    return f"{speaker}.wav"


async def _run(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    failed = []
    for label, speaker in Y.YARNGPT_SPEAKERS.items():
        try:
            audio = await Y.synthesize(preview_line(speaker.capitalize()), speaker)
            (out / filename_for(speaker)).write_bytes(audio)
            print(f"ok      {label:22s} -> {filename_for(speaker)} ({len(audio) // 1024} KB)")
        except Exception as e:
            failed.append(label)
            print(f"FAILED  {label:22s} {e}")
    if failed:
        print(f"\n{len(failed)} voice(s) failed: {', '.join(failed)}")
        return 1
    print(f"\nAll {len(Y.YARNGPT_SPEAKERS)} previews written to {out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if not Y.is_configured():
        print("Set YARNGPT_API_KEY first (the key from the Account page at yarngpt.ai).")
        return 2
    return asyncio.get_event_loop().run_until_complete(_run(Path(args.out)))


if __name__ == "__main__":
    sys.exit(main())
