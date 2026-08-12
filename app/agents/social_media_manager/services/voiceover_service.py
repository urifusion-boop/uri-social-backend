"""
Ask Jane Video — voiceover (MVP, no semantic shot-alignment yet — see the
voiceover-spec.md Phase-2 note in the approved plan).

A silent/product video gives the transcript-driven engine (captions, silence trim,
b-roll, emphasis) almost nothing to work with. A voiceover turns it into a
talking-head-equivalent video the engine already handles well — that's the value
this ships, without the spec's most novel piece (matching phrases to shots and
retiming clips), which has no existing vision-pass/timeline-position infrastructure
to build on for this pipeline yet.

Three functions, each reusing an existing, proven pattern rather than inventing one:
  - generate_voiceover_script: same lightweight-GPT-call shape as
    video_production_service._generate_hook_text.
  - clean_voiceover_audio: reuses multi_clip_service's filler-word regex and
    same-content (Jaccard) repeat detection — a "sorry, let me start again" followed
    by the restated sentence is exactly the repeat pattern that logic already targets
    — then cuts those ranges out with short crossfades so the join doesn't sound
    broken (the existing false-start work already proved a crossfade-less cut does).
  - mix_voiceover_as_primary_audio: same download → ffmpeg → reupload shape as
    complete_social_manager._mix_music_into_video, with the voiceover always the
    loudest track.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from typing import Optional

import httpx
import openai

from app.core.config import settings
from .multi_clip_service import (
    _COMPOSE_FILLER_RE,
    _COMPOSE_REPEAT_MIN_WORDS,
    _COMPOSE_REPEAT_THRESHOLD,
    _COMPOSE_REPEAT_WINDOW,
    _content_words,
    _merge_cuts,
    _probe_clip,
    _srt_parse,
    _transcribe_clip,
)
from .video_production_service import _upload_audio_to_cloudinary, _upload_to_cloudinary

_MIN_KEEP_SEGMENT_SECONDS = 0.2  # shorter slivers between cuts are rounding noise, not real speech
_CROSSFADE_SECONDS = 0.15        # short — long enough to hide the join, short enough to stay invisible


async def generate_voiceover_script(
    transcript: str, purpose: str, brand_context: dict, user_note: str = ""
) -> Optional[str]:
    """Dedicated, lightweight GPT call — same shape/fail-soft contract as
    _generate_hook_text. Grounds the draft in the video's own transcript (if it has
    one), the plan's purpose, and brand voice; presented as a plain suggested script,
    never a constraint — the user can ignore it entirely and say their own thing."""
    brand_name = brand_context.get("brand_name", "") or "the business"
    brand_voice = brand_context.get("brand_voice", "") or "warm, direct, conversational"
    region = brand_context.get("region", "")

    context_lines = [f"Business: {brand_name}", f"Voice/tone: {brand_voice}"]
    if region:
        context_lines.append(f"Region: {region}")
    if purpose:
        context_lines.append(f"Purpose of this video: {purpose}")
    if transcript and transcript.strip():
        context_lines.append(f"Existing footage transcript: {transcript.strip()[:2000]}")
    if user_note and user_note.strip():
        context_lines.append(f"What the user wants said (use this if given): {user_note.strip()[:1000]}")

    prompt = (
        "You are writing a short voiceover script for a Nigerian small business's video, "
        "meant to be SPOKEN aloud by the business owner, not read.\n\n"
        + "\n".join(context_lines) + "\n\n"
        "Rules:\n"
        "- Short clauses, natural spoken rhythm — not written-English sentence structure.\n"
        "- If a price is given above, state it plainly (e.g. \"₦12,000\"), never invent one if none was given.\n"
        "- End with a direct ask (e.g. \"Message me to order\"), not a vague call to action.\n"
        "- Plain Nigerian English; only use Pidgin if the voice/tone above supports it.\n"
        "- 4-8 short lines, roughly 15-25 seconds spoken aloud.\n"
        "Return ONLY the script text, one line per spoken beat, no headers, no explanation."
    )
    try:
        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model="gpt-5",
            messages=[{"role": "user", "content": prompt}],
        )
        text = (response.choices[0].message.content or "").strip()
        return text or None
    except Exception as exc:
        print(f"[Voiceover] script generation failed: {exc}", flush=True)
        return None


def _find_cuts(srt: str) -> list[dict]:
    """Filler words + same-content repeats, same detection logic
    _timing_cuts_from_clip uses for clip transcripts (clip_offset=0 here — this
    operates on a single standalone audio file, not a multi-clip global timeline)."""
    entries = _srt_parse(srt)
    cuts: list[dict] = []

    for entry in entries:
        clean = entry["text"].lower().strip(".,!?;:\"' ")
        if _COMPOSE_FILLER_RE.match(clean):
            cuts.append({"at": entry["start"], "end": entry["end"], "reason": f'filler: "{entry["text"]}"'})

    already_flagged: set = set()
    for i in range(len(entries)):
        if i in already_flagged:
            continue
        cw_i = _content_words(entries[i]["text"])
        if len(cw_i) < _COMPOSE_REPEAT_MIN_WORDS:
            continue
        for j in range(i + 1, len(entries)):
            if entries[j]["start"] - entries[i]["end"] > _COMPOSE_REPEAT_WINDOW:
                break
            if j in already_flagged:
                continue
            cw_j = _content_words(entries[j]["text"])
            if len(cw_j) < _COMPOSE_REPEAT_MIN_WORDS:
                continue
            union = len(cw_i | cw_j)
            if union == 0:
                continue
            if len(cw_i & cw_j) / union >= _COMPOSE_REPEAT_THRESHOLD:
                # The EARLIER occurrence is the false start/retry — cut it, keep the
                # restated (later) version, matching "sorry, let me start again" →
                # the correction is what should survive.
                cuts.append({"at": entries[i]["start"], "end": entries[i]["end"], "reason": f'false start: "{entries[i]["text"][:60]}"'})
                already_flagged.add(i)
                break

    return cuts


async def _run_ffmpeg(args: list[str], timeout: int = 120) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode()[-500:]}")


async def clean_voiceover_audio(audio_bytes: bytes) -> tuple[bytes, str]:
    """Transcribe, cut filler/false-start segments with short crossfades at each
    join, return (cleaned_audio_bytes, cleaned_transcript). Fails soft to the
    original audio + its raw transcript if cleanup itself errors — a cleanup bug
    must never block the user's recording from being used at all."""
    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
        f.write(audio_bytes)
        in_path = f.name

    try:
        transcript = await _transcribe_clip(in_path)
        srt = transcript.get("srt", "")
        raw_text = transcript.get("text", "")
        if not srt:
            return audio_bytes, raw_text

        cuts = _merge_cuts(_find_cuts(srt))
        if not cuts:
            return audio_bytes, raw_text

        probe = await _probe_clip(in_path)
        duration = probe.get("duration", 0) or 0
        if duration <= 0:
            return audio_bytes, raw_text

        # Complement of the merged cut ranges = what to keep, in order.
        keep: list[tuple[float, float]] = []
        cursor = 0.0
        for cut in cuts:
            if cut["at"] - cursor >= _MIN_KEEP_SEGMENT_SECONDS:
                keep.append((cursor, cut["at"]))
            cursor = max(cursor, cut["end"])
        if duration - cursor >= _MIN_KEEP_SEGMENT_SECONDS:
            keep.append((cursor, duration))

        if not keep:
            # Every segment got cut (e.g. a single long false start with no clean
            # restatement) — fail soft to the original rather than ship silence.
            return audio_bytes, raw_text

        out_path = f"/tmp/voiceover-cleaned-{uuid.uuid4().hex[:8]}.mp3"
        if len(keep) == 1:
            start, end = keep[0]
            await _run_ffmpeg([
                "ffmpeg", "-y", "-i", in_path,
                "-ss", str(start), "-to", str(end),
                "-c:a", "libmp3lame", "-b:a", "128k",
                out_path,
            ])
        else:
            filter_parts = []
            for idx, (start, end) in enumerate(keep):
                filter_parts.append(f"[0:a]atrim={start}:{end},asetpts=PTS-STARTPTS[s{idx}]")
            chain = "[s0]"
            for idx in range(1, len(keep)):
                out_label = f"x{idx}" if idx < len(keep) - 1 else "out"
                filter_parts.append(f"{chain}[s{idx}]acrossfade=d={_CROSSFADE_SECONDS}[{out_label}]")
                chain = f"[{out_label}]"
            filter_complex = ";".join(filter_parts)
            await _run_ffmpeg([
                "ffmpeg", "-y", "-i", in_path,
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-c:a", "libmp3lame", "-b:a", "128k",
                out_path,
            ])

        with open(out_path, "rb") as f:
            cleaned_bytes = f.read()
        try:
            os.unlink(out_path)
        except Exception:
            pass

        cleaned_text = " ".join(
            e["text"] for e in _srt_parse(srt)
            if not any(c["at"] <= e["start"] < c["end"] for c in cuts)
        ).strip()
        return cleaned_bytes, cleaned_text or raw_text

    except Exception as exc:
        print(f"[Voiceover] cleanup failed, using original recording: {exc}", flush=True)
        return audio_bytes, ""
    finally:
        try:
            os.unlink(in_path)
        except Exception:
            pass


async def mix_voiceover_as_primary_audio(
    video_url: str,
    voiceover_audio: bytes,
    keep_original_audio: bool = False,
    music_url: Optional[str] = None,
) -> Optional[str]:
    """Voiceover is always the loudest track. Original clip audio is muted by
    default (spec: clips with existing speech default to muted — two voices is
    confusing), or kept low under the voice when keep_original_audio=True (offered
    to the user only for clips with non-speech ambient audio). Music, if present,
    ducks under the voice the same way _mix_music_into_video already ducks it under
    original speech."""
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            video_resp = await client.get(video_url)
            music_resp = await client.get(music_url) if music_url else None

        if video_resp.status_code != 200:
            print(f"[Voiceover] video download failed {video_resp.status_code}", flush=True)
            return None
        if music_url and (not music_resp or music_resp.status_code != 200):
            print(f"[Voiceover] music download failed — continuing without music", flush=True)
            music_url = None

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as vf:
            vf.write(video_resp.content)
            video_tmp = vf.name
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as af:
            af.write(voiceover_audio)
            voice_tmp = af.name
        music_tmp = None
        if music_url:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mf:
                mf.write(music_resp.content)
                music_tmp = mf.name

        output_path = f"/tmp/voiceover-mixed-{uuid.uuid4().hex[:8]}.mp4"
        inputs = ["-i", video_tmp, "-i", voice_tmp]
        if music_tmp:
            inputs += ["-stream_loop", "-1", "-i", music_tmp]

        orig_vol = 0.15 if keep_original_audio else 0.0
        voice_label = "[1:a]volume=1.6[va]"
        mix_inputs = ["[va]"]
        filters = [voice_label]
        if orig_vol > 0:
            filters.append(f"[0:a]volume={orig_vol}[oa]")
            mix_inputs.append("[oa]")
        if music_tmp:
            filters.append("[2:a]volume=0.2[ma]")
            mix_inputs.append("[ma]")

        if len(mix_inputs) == 1:
            filter_complex = f"{voice_label}"
            audio_map = "[va]"
        else:
            filters.append(f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first[a]")
            filter_complex = ";".join(filters)
            audio_map = "[a]"

        proc_args = [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", audio_map,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            output_path,
        ]

        await _run_ffmpeg(proc_args, timeout=300)

        with open(output_path, "rb") as f:
            mixed_bytes = f.read()

        for p in [video_tmp, voice_tmp, music_tmp, output_path]:
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass

        return await _upload_to_cloudinary(mixed_bytes, f"voiceover-mixed-{uuid.uuid4().hex[:12]}")

    except Exception as exc:
        print(f"[Voiceover] mix failed: {exc}", flush=True)
        return None


async def upload_voiceover_audio(audio_bytes: bytes, job_id: str) -> Optional[str]:
    """Persist the cleaned voiceover audio itself (for storage/reference on the
    job doc — reuses the same raw-resource upload as custom music uploads)."""
    return await _upload_audio_to_cloudinary(audio_bytes, f"voiceover-{job_id}")
