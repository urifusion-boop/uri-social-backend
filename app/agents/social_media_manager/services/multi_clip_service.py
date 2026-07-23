"""
Multi-Clip Composition Service — Phase 1 (Founder Story)
Pipeline: Upload clips → probe + quality check → Whisper transcription per clip
          → GPT-4o narrative ordering → Shotstack multi-clip timeline → render
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import openai

from app.core.config import settings

# ── Re-use helpers from video_production_service ──────────────────────────────
from app.agents.social_media_manager.services.video_production_service import (
    _upload_to_cloudinary,
    _pick_music_url,
    ShotstackProvider,
    SHOTSTACK_EDIT_BASE,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_CLOUDINARY_FOLDER = "uri-multi-clip"
_MIN_CLIP_DURATION = 2.0   # clips shorter than this get flagged
_MAX_CLIPS = 10
_CROSSFADE_DURATION = 0.6  # seconds overlap between clips in Shotstack
_STILL_DEFAULT_DURATION = 2.5  # seconds a still image holds on screen
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
    ".gif": "image/gif", ".bmp": "image/bmp",
}


# ── FFprobe ───────────────────────────────────────────────────────────────────

async def _probe_clip(path: str) -> Dict[str, Any]:
    """Return {duration, width, height, has_audio} via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        data = json.loads(stdout.decode())
        duration = float(data.get("format", {}).get("duration", 0))
        has_audio = has_subtitles = False
        width = height = 0
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                width = s.get("width", 0)
                height = s.get("height", 0)
            if s.get("codec_type") == "audio":
                has_audio = True
            if s.get("codec_type") == "subtitle":
                has_subtitles = True
        return {
            "duration": duration, "width": width, "height": height,
            "has_audio": has_audio, "has_subtitles": has_subtitles,
        }
    except Exception as e:
        print(f"[MultiClip] probe error: {e}", flush=True)
        return {"duration": 0, "width": 0, "height": 0, "has_audio": False, "has_subtitles": False}


# ── Quality flags ─────────────────────────────────────────────────────────────

_AUDIO_TARGET_LUFS = -16.0  # target mean loudness for leveling


def _compute_volume_boost(mean_db: float) -> float:
    """Convert a measured mean dBFS to a linear boost factor targeting -16 dBFS."""
    if mean_db < -70 or mean_db > -5:
        return 1.0  # silent or already loud — don't touch
    diff_db = _AUDIO_TARGET_LUFS - mean_db
    boost = 10 ** (diff_db / 20.0)
    return max(0.3, min(2.5, round(boost, 3)))


async def _check_quality(path: str, probe: Dict) -> Tuple[List[str], float]:
    """Return (quality_flags, mean_volume_db). mean_volume_db is 0.0 if not measured."""
    flags: List[str] = []
    mean_db = 0.0

    if probe["duration"] < _MIN_CLIP_DURATION:
        flags.append("too_short")

    # Baked subtitle stream → already edited
    if probe.get("has_subtitles"):
        flags.append("pre_edited")

    # Brightness: sample one frame, check mean luminance via ffmpeg
    try:
        luma_cmd = [
            "ffmpeg", "-i", path, "-vframes", "1",
            "-vf", "scale=64:64,format=gray,signalstats",
            "-f", "null", "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *luma_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        err_text = stderr.decode()
        m = re.search(r"YAVG:(\d+\.?\d*)", err_text)
        if m and float(m.group(1)) < 20:
            flags.append("too_dark")
    except Exception:
        pass

    # Audio energy: measure mean volume for quality flags AND leveling
    if probe["has_audio"]:
        try:
            vol_cmd = [
                "ffmpeg", "-i", path, "-af", "volumedetect",
                "-f", "null", "-",
            ]
            proc = await asyncio.create_subprocess_exec(
                *vol_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            m = re.search(r"mean_volume:\s*(-?\d+\.?\d*)", stderr.decode())
            if m:
                mean_db = float(m.group(1))
                if mean_db < -50:
                    flags.append("too_quiet")
        except Exception:
            pass

    return flags, mean_db


# ── FFmpeg silence detection ──────────────────────────────────────────────────

async def _detect_silences_ffmpeg(
    path: str,
    noise_db: float = -30,
    min_duration: float = 0.5,
) -> List[Dict]:
    """
    Detect silence ranges in a clip by measuring actual audio amplitude.
    Returns [{start, end}] in clip-local seconds for every section where the
    audio drops below noise_db for at least min_duration seconds.

    Unlike Whisper word-gap detection, this catches completely silent sections
    that Whisper never transcribed — e.g. the person walked away, paused for
    5 seconds, looked at their notes.
    """
    cmd = [
        "ffmpeg", "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
        "-f", "null", "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stderr_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[MultiClip/silencedetect] FFmpeg error: {exc}", flush=True)
        return []

    silences: List[Dict] = []
    pending_start: Optional[float] = None
    for line in output.splitlines():
        if "silence_start:" in line:
            try:
                pending_start = float(line.split("silence_start:")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif "silence_end:" in line and pending_start is not None:
            try:
                end = float(line.split("silence_end:")[1].strip().split("|")[0].strip())
                silences.append({"start": round(pending_start, 3), "end": round(end, 3)})
                pending_start = None
            except (ValueError, IndexError):
                pass

    print(f"[MultiClip/silencedetect] {path[-30:]} → {len(silences)} silence range(s)", flush=True)
    return silences


# ── Speech detection ──────────────────────────────────────────────────────────

async def _detect_clip_type(path: str, probe: Dict) -> str:
    """
    Returns 'speech', 'silent', or 'still'.
    Still: duration < 0.5s (image wrapped as video).
    Speech: audio present and mean volume above threshold.
    """
    if probe["duration"] < 0.5:
        return "still"
    if not probe["has_audio"]:
        return "silent"

    # Use ffmpeg volumedetect — if mean_volume > -40 dBFS → speech/audio present
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", path, "-af", "volumedetect", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        m = re.search(r"mean_volume:\s*(-?\d+\.?\d*)", stderr.decode())
        if m and float(m.group(1)) > -40:
            return "speech"
    except Exception:
        pass
    return "silent"


# ── Frame extraction ──────────────────────────────────────────────────────────

async def _extract_frame_bytes(path: str, t: float = 1.0) -> Optional[bytes]:
    """Extract one frame at time t as JPEG bytes."""
    out_path = path + "_frame.jpg"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-ss", str(t), "-i", path,
            "-vframes", "1", "-q:v", "5", "-y", out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if os.path.exists(out_path):
            with open(out_path, "rb") as f:
                return f.read()
    except Exception as e:
        print(f"[MultiClip] frame extract error: {e}", flush=True)
    finally:
        if os.path.exists(out_path):
            os.unlink(out_path)
    return None


# ── Subject-aware crop position ───────────────────────────────────────────────

async def _detect_subject_position(path: str) -> str:
    """
    Extract a frame and ask GPT-4o-vision where the main subject sits.
    Returns 'left', 'center', or 'right'. Falls back to 'center' on any error.
    Used to set Shotstack clip position so the crop keeps the subject in frame.
    """
    try:
        frame_bytes = await _extract_frame_bytes(path, t=1.0)
        if not frame_bytes:
            return "center"
        frame_b64 = base64.b64encode(frame_bytes).decode()
        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await client.chat.completions.create(
            model="gpt-5.6-luna",
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{frame_b64}",
                            "detail": "low",
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Where is the main subject (person or product) in this frame? "
                            "Answer with only one word: left, center, or right."
                        ),
                    },
                ],
            }],
        )
        result = resp.choices[0].message.content.strip().lower()
        if result in ("left", "center", "right"):
            return result
    except Exception as e:
        print(f"[MultiClip] subject position detection error: {e}", flush=True)
    return "center"


# ── Audio extraction for Whisper ──────────────────────────────────────────────

async def _extract_audio_mp3(video_path: str) -> Optional[str]:
    """
    Extract audio from any video format to a temp mp3 file.
    Returns the path of the mp3 file, or None on failure.
    Whisper supports mp3 universally; this handles .mov, .avi, .mkv etc.
    """
    audio_path = video_path + "_audio.mp3"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", video_path,
            "-vn",                   # no video
            "-ar", "16000",          # 16kHz sample rate (Whisper prefers)
            "-ac", "1",              # mono
            "-b:a", "64k",           # small file
            "-y", audio_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 100:
            return audio_path
    except Exception as e:
        print(f"[MultiClip] audio extract error: {e}", flush=True)
    return None


# ── Whisper transcription ─────────────────────────────────────────────────────

async def _transcribe_clip(path: str) -> Dict[str, Any]:
    """
    Transcribe a video clip using OpenAI Whisper.
    Extracts audio to mp3 first so any video format is accepted.
    Returns {text, srt, words:[{word, start, end}]}
    """
    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    # Extract audio — Whisper rejects .mov/.avi/.mkv natively
    audio_path = await _extract_audio_mp3(path)
    use_path = audio_path if audio_path else path

    try:
        with open(use_path, "rb") as f:
            # verbose_json gives word-level timestamps
            response = await client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
            )

        text = response.text or ""
        words = []
        if hasattr(response, "words") and response.words:
            for w in response.words:
                words.append({
                    "word": w.word,
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                })

        # Build SRT from segments and capture Whisper's per-segment confidence signals.
        # no_speech_prob > 0.5 means Whisper thought this segment might not be speech
        # at all — it transcribed something anyway (often a false start, mumble, or
        # noise artefact). avg_logprob < -0.8 means very low token-level confidence.
        # Both are signs Whisper may have silently papered over a disfluency.
        srt_lines = []
        uncertain_segments = []
        if hasattr(response, "segments") and response.segments:
            for i, seg in enumerate(response.segments, 1):
                def _fmt(t: float) -> str:
                    h = int(t // 3600)
                    m = int((t % 3600) // 60)
                    s = int(t % 60)
                    ms = int((t % 1) * 1000)
                    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
                srt_lines.append(str(i))
                srt_lines.append(f"{_fmt(seg.start)} --> {_fmt(seg.end)}")
                srt_lines.append(seg.text.strip())
                srt_lines.append("")

                no_sp = getattr(seg, "no_speech_prob", 0.0) or 0.0
                avg_lp = getattr(seg, "avg_logprob", 0.0) or 0.0
                if no_sp > 0.5 or avg_lp < -0.8:
                    uncertain_segments.append({
                        "start":          round(seg.start, 3),
                        "end":            round(seg.end, 3),
                        "no_speech_prob": round(no_sp, 3),
                        "avg_logprob":    round(avg_lp, 3),
                        "text":           seg.text.strip(),
                    })

        if uncertain_segments:
            print(
                f"[MultiClip/transcribe] {len(uncertain_segments)} uncertain segment(s) "
                f"(no_speech_prob>0.5 or avg_logprob<-0.8)",
                flush=True,
            )

        return {"text": text, "srt": "\n".join(srt_lines), "words": words, "uncertain_segments": uncertain_segments}

    except Exception as e:
        print(f"[MultiClip] transcription error: {e}", flush=True)
        return {"text": "", "srt": "", "words": []}
    finally:
        if audio_path and os.path.exists(audio_path):
            try:
                os.unlink(audio_path)
            except Exception:
                pass


# ── AI ordering (Founder Story) ───────────────────────────────────────────────

async def _suggest_founder_order(clips: List[Dict]) -> List[str]:
    """
    GPT-4o reads all clip transcripts and returns clip_ids in narrative order.
    Arc: hook → context → story → main point → CTA
    """
    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    clip_lines = []
    for c in clips:
        transcript = (c.get("transcript") or "").strip()
        if not transcript:
            transcript = "[no speech detected]"
        clip_lines.append(f'clip_id="{c["clip_id"]}" duration={c["duration_seconds"]:.1f}s\n  "{transcript}"')

    prompt = f"""You are a video editor ordering talking-head clips for a social media short.

Here are the clips:
{chr(10).join(clip_lines)}

Order them along this narrative arc:
1. HOOK — grabby opening that immediately engages
2. CONTEXT — who this is for / the problem
3. STORY — the main content / journey
4. MAIN POINT — the key takeaway or claim
5. CTA — call to action / close

Return ONLY a JSON array of clip_ids in the recommended order, e.g.:
["clip_abc", "clip_def", "clip_ghi"]

Rules:
- Include every clip_id exactly once
- If a clip has no speech, place it after the nearest speech clip as context
- If only one clip exists, return it as-is
"""

    try:
        resp = await client.chat.completions.create(
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        # GPT returns {"order": [...]} or just [...]
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            ordered = parsed
        else:
            ordered = parsed.get("order", parsed.get("clip_ids", []))
        # Validate — every clip_id must appear
        valid_ids = {c["clip_id"] for c in clips}
        ordered = [cid for cid in ordered if cid in valid_ids]
        # Append any missing
        for c in clips:
            if c["clip_id"] not in ordered:
                ordered.append(c["clip_id"])
        return ordered
    except Exception as e:
        print(f"[MultiClip] order suggestion error: {e}", flush=True)
        return [c["clip_id"] for c in clips]


# ── Cloudinary upload (clip) ──────────────────────────────────────────────────

_CONTENT_TYPE_MAP = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
}


async def _upload_clip_to_cloudinary(
    video_bytes: bytes, clip_id: str, original_filename: str = ""
) -> Optional[str]:
    """Upload a single clip to Cloudinary under the multi-clip folder."""
    cloud = settings.CLOUDINARY_CLOUD_NAME
    api_key = settings.CLOUDINARY_API_KEY
    api_secret = settings.CLOUDINARY_API_SECRET
    if not all([cloud, api_key, api_secret]):
        return None

    ext = os.path.splitext(original_filename)[1].lower() if original_filename else ".mp4"
    if not ext:
        ext = ".mp4"

    is_image = ext in _IMAGE_EXTS
    if is_image:
        content_type = _IMAGE_CONTENT_TYPES.get(ext, "image/jpeg")
        resource_type = "image"
    else:
        content_type = _CONTENT_TYPE_MAP.get(ext, "video/mp4")
        resource_type = "video"

    public_id = f"clip_{clip_id}"
    ts = int(time.time())
    params_str = f"folder={_CLOUDINARY_FOLDER}&public_id={public_id}&timestamp={ts}"
    signature = hashlib.sha1(f"{params_str}{api_secret}".encode()).hexdigest()

    form = aiohttp.FormData()
    form.add_field("file", video_bytes, filename=f"{public_id}{ext}", content_type=content_type)
    form.add_field("api_key", api_key)
    form.add_field("timestamp", str(ts))
    form.add_field("signature", signature)
    form.add_field("public_id", public_id)
    form.add_field("folder", _CLOUDINARY_FOLDER)
    form.add_field("resource_type", "video")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.cloudinary.com/v1_1/{cloud}/{resource_type}/upload",
                data=form,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                body = json.loads(await resp.text())
                if not resp.ok:
                    print(f"[MultiClip] Cloudinary upload failed: {body}", flush=True)
                    return None
                url = body.get("secure_url", "")
                print(f"[MultiClip] uploaded clip {clip_id} → {url}", flush=True)
                return url
    except Exception as e:
        print(f"[MultiClip] Cloudinary error: {e}", flush=True)
        return None


# ── SRT timestamp offset ──────────────────────────────────────────────────────

def _shift_srt(srt: str, offset_seconds: float) -> str:
    """Shift all SRT timestamps by offset_seconds."""
    if not srt.strip():
        return srt

    def _parse_ts(ts: str) -> float:
        h, m, rest = ts.split(":")
        s, ms = rest.replace(",", ".").split(".")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    def _fmt_ts(t: float) -> str:
        t = max(0.0, t)
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int(round((t % 1) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = srt.split("\n")
    out = []
    for line in lines:
        if " --> " in line:
            parts = line.split(" --> ")
            t_start = _parse_ts(parts[0].strip()) + offset_seconds
            t_end = _parse_ts(parts[1].strip()) + offset_seconds
            out.append(f"{_fmt_ts(t_start)} --> {_fmt_ts(t_end)}")
        else:
            out.append(line)
    return "\n".join(out)


def _trim_srt(srt: str, trim_start: float, new_dur: float) -> str:
    """
    Filter and shift SRT blocks for a center-cut clip.
    Removes blocks outside [trim_start, trim_start + new_dur) and shifts by -trim_start.
    """
    def _parse_ts(ts: str) -> float:
        h, m, rest = ts.split(":")
        s, ms = rest.replace(",", ".").split(".")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    def _fmt_ts(t: float) -> str:
        t = max(0.0, t)
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = round((t % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    if not srt.strip() or trim_start == 0.0:
        return srt

    blocks = [b.strip() for b in srt.strip().split("\n\n") if b.strip()]
    result: List[str] = []
    counter = 1
    for block in blocks:
        lines = block.split("\n")
        if len(lines) < 3 or " --> " not in lines[1]:
            continue
        parts = lines[1].split(" --> ")
        t_start = _parse_ts(parts[0].strip())
        t_end = _parse_ts(parts[1].strip())
        if t_end <= trim_start or t_start >= trim_start + new_dur:
            continue
        new_start = max(0.0, t_start - trim_start)
        new_end = min(new_dur, t_end - trim_start)
        if new_end <= 0:
            continue
        result.append(str(counter))
        result.append(f"{_fmt_ts(new_start)} --> {_fmt_ts(new_end)}")
        result.extend(lines[2:])
        result.append("")
        counter += 1
    return "\n".join(result)


def _merge_srts(clips_ordered: List[Dict]) -> str:
    """Merge SRT from all clips in order, shifting timestamps to stitched positions."""
    merged_lines: List[str] = []
    counter = 1
    timeline_offset = 0.0

    for clip in clips_ordered:
        srt = clip.get("srt") or ""
        duration = clip.get("duration_seconds", 0)
        if srt.strip():
            shifted = _shift_srt(srt, timeline_offset)
            # Re-number entries
            blocks = [b.strip() for b in shifted.strip().split("\n\n") if b.strip()]
            for block in blocks:
                lines = block.split("\n")
                if len(lines) >= 3:
                    merged_lines.append(str(counter))
                    merged_lines.extend(lines[1:])  # skip original number, keep timing + text
                    merged_lines.append("")
                    counter += 1
        # Advance timeline by clip duration (minus crossfade overlap for subsequent clips)
        timeline_offset += duration

    return "\n".join(merged_lines)


# ── Shotstack timeline builder ────────────────────────────────────────────────

_AI_TRANSITION_MAP: Dict[str, Optional[Dict]] = {
    "cut":   None,                    # hard cut — no transition object
    "fade":  {"out": "fade"},
    "slide": {"out": "slideLeft"},
    "zoom":  {"out": "zoom"},
}


_COMPOSE_FILLER_RE = re.compile(
    r"^(um+|uh+|ah+|hmm+|er+|erm+|mhm+|uhh+|umm+|huh|mm+)$",
    re.IGNORECASE,
)
_COMPOSE_SILENCE_THRESHOLD = 1.2   # seconds — gaps shorter than this are natural pauses, leave them
_COMPOSE_MAX_CUT_DURATION  = 3.0   # seconds — longer gaps are likely Whisper misses, not real silence
_COMPOSE_REPEAT_WINDOW     = 60.0  # seconds — scan this far ahead for repeated sentences
_COMPOSE_REPEAT_THRESHOLD  = 0.65  # Jaccard similarity on content words to flag a repetition
_COMPOSE_REPEAT_MIN_WORDS  = 4     # minimum content words before repetition check fires

# Common words excluded from repetition similarity so "I think we should" doesn't
# match "I think you should" as a repetition.
_STOP_WORDS: set = {
    "a", "an", "the", "is", "was", "are", "were", "i", "to", "of", "and",
    "or", "in", "on", "at", "it", "this", "that", "my", "me", "we", "you",
    "he", "she", "they", "but", "so", "if", "as", "for", "with", "be",
    "have", "has", "had", "do", "did", "not", "from", "by", "about", "just",
    "like", "what", "when", "how", "all", "there", "their", "your", "our",
    "its", "up", "out", "which", "who", "then", "than", "into", "also",
    "very", "really", "actually", "basically", "right", "yeah", "okay",
}


def _content_words(text: str) -> set:
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    return {w for w in words if w not in _STOP_WORDS and len(w) > 2}


def _srt_parse(srt: str) -> List[Dict]:
    """Parse an SRT string into [{start, end, text}] entries."""
    def _ts(s: str) -> float:
        h, m, rest = s.split(":")
        sec, ms = rest.replace(",", ".").split(".")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000

    entries = []
    for block in [b.strip() for b in srt.strip().split("\n\n") if b.strip()]:
        lines = block.split("\n")
        if len(lines) < 3 or " --> " not in lines[1]:
            continue
        parts = lines[1].split(" --> ")
        entries.append({
            "start": _ts(parts[0].strip()),
            "end":   _ts(parts[1].strip()),
            "text":  " ".join(lines[2:]).strip(),
        })
    return entries


def _timing_cuts_from_clip(
    srt: str,
    clip_offset: float,
    clip_duration: float,
    clip_type: str = "speech",
    words: Optional[List[Dict]] = None,
    silence_ranges: Optional[List[Dict]] = None,
    uncertain_segments: Optional[List[Dict]] = None,
) -> List[Dict]:
    """
    Return cuts (global timeline coordinates) for a single clip covering:
      - Silences / non-talking sections: three-tier detection in priority order:
          1. FFmpeg silencedetect (amplitude-based, catches ALL non-talking —
             the person walking away, a 10-second pause, sections Whisper never saw)
          2. Whisper word-level gaps (mid-sentence pauses SRT misses)
          3. SRT segment gaps (coarsest fallback)
      - Uncertain Whisper segments: segments Whisper transcribed but flagged with
        no_speech_prob > 0.5 or avg_logprob < -0.8 — likely false starts or mumbles
        that Whisper papered over into plausible-sounding words.
      - Filler words: whole SRT entries that are just um/uh/hmm etc.
      - Repeated sentences: SRT entries with >= 65% content-word overlap
        within a 60s window (second occurrence is cut)

    Only runs on speech clips. FFmpeg silence ranges have no duration cap —
    they come from real audio measurement. Word/SRT gaps cap at 3s to guard
    against Whisper transcript holes being mistaken for silence.
    """
    if clip_type != "speech":
        return []

    entries = _srt_parse(srt)
    cuts: List[Dict] = []

    # ── Silences and long pauses ──────────────────────────────────────────────
    if silence_ranges:
        # Primary: FFmpeg amplitude analysis — catches everything including
        # completely silent sections that Whisper never produced words for.
        # No _COMPOSE_MAX_CUT_DURATION cap: these ranges are measured from
        # the actual audio waveform and are reliable.
        for sr in silence_ranges:
            dur = sr["end"] - sr["start"]
            if dur >= _COMPOSE_SILENCE_THRESHOLD:
                cuts.append({
                    "at":     round(clip_offset + sr["start"], 3),
                    "end":    round(clip_offset + sr["end"],   3),
                    "reason": f"silence {dur:.1f}s (audio)",
                })
    else:
        # Fallback path for clips ingested before silence_ranges was stored.
        def _maybe_silence_cut(at: float, end: float, reason: str) -> None:
            dur = end - at
            # Cap at 3s: Whisper/SRT gaps beyond this are likely transcript holes,
            # not real silence the speaker is in.
            if _COMPOSE_SILENCE_THRESHOLD <= dur <= _COMPOSE_MAX_CUT_DURATION:
                cuts.append({"at": round(at, 3), "end": round(end, 3), "reason": reason})

        if words:
            _maybe_silence_cut(clip_offset, clip_offset + words[0]["start"],
                               f"leading silence {words[0]['start']:.1f}s")
            for i in range(1, len(words)):
                gap = words[i]["start"] - words[i - 1]["end"]
                _maybe_silence_cut(
                    clip_offset + words[i - 1]["end"],
                    clip_offset + words[i]["start"],
                    f"pause {gap:.1f}s",
                )
            trail = clip_duration - words[-1]["end"]
            _maybe_silence_cut(clip_offset + words[-1]["end"], clip_offset + clip_duration,
                               f"trailing silence {trail:.1f}s")
        elif entries:
            _maybe_silence_cut(clip_offset, clip_offset + entries[0]["start"],
                               f"leading silence {entries[0]['start']:.1f}s")
            for i in range(1, len(entries)):
                gap = entries[i]["start"] - entries[i - 1]["end"]
                _maybe_silence_cut(
                    clip_offset + entries[i - 1]["end"],
                    clip_offset + entries[i]["start"],
                    f"pause {gap:.1f}s",
                )
            trail = clip_duration - entries[-1]["end"]
            _maybe_silence_cut(clip_offset + entries[-1]["end"], clip_offset + clip_duration,
                               f"trailing silence {trail:.1f}s")

    # ── Uncertain Whisper segments ────────────────────────────────────────────
    # Whisper transcribed these but its own confidence signals say it wasn't sure:
    # no_speech_prob > 0.5 (model thought this might not be speech) or
    # avg_logprob < -0.8 (very low per-token probability — likely a mumble or
    # false start that Whisper guessed at rather than clearly heard).
    # These are routed through the LLM refinement layer so context can decide.
    for us in (uncertain_segments or []):
        dur = us["end"] - us["start"]
        if dur >= 0.15:
            cuts.append({
                "at":     round(clip_offset + us["start"], 3),
                "end":    round(clip_offset + us["end"],   3),
                "reason": f'uncertain speech (no_speech_prob={us["no_speech_prob"]:.2f}, avg_logprob={us["avg_logprob"]:.2f})',
            })

    # ── Filler words ─────────────────────────────────────────────────────────
    for entry in entries:
        clean = entry["text"].lower().strip(".,!?;:\"' ")
        if _COMPOSE_FILLER_RE.match(clean):
            cuts.append({
                "at":     round(clip_offset + entry["start"], 3),
                "end":    round(clip_offset + entry["end"],   3),
                "reason": f'filler: "{entry["text"]}"',
            })

    # ── Repeated sentences ────────────────────────────────────────────────────
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
                cuts.append({
                    "at":     round(clip_offset + entries[j]["start"], 3),
                    "end":    round(clip_offset + entries[j]["end"],   3),
                    "reason": f'repeated: "{entries[j]["text"][:60]}"',
                })
                already_flagged.add(j)

    return cuts


def _merge_cuts(cuts: List[Dict]) -> List[Dict]:
    """Sort and merge overlapping/adjacent cut ranges."""
    if not cuts:
        return []
    cuts = sorted(cuts, key=lambda c: c["at"])
    merged: List[Dict] = [dict(cuts[0])]
    for cut in cuts[1:]:
        if cut["at"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], cut["end"])
            merged[-1]["reason"] += f"; {cut['reason']}"
        else:
            merged.append(dict(cut))
    # Remove cuts shorter than 0.1s (rounding artifacts)
    return [c for c in merged if c["end"] - c["at"] >= 0.1]


async def _refine_silence_cuts(
    silence_cuts: List[Dict],
    srt_global: List[Dict],
    total_duration: float,
) -> List[Dict]:
    """
    LLM refinement pass on FFmpeg/Whisper-detected silence and pause cuts.

    Uses the spoken words immediately before and after each gap to decide
    whether it's dead air (cut) or an intentional dramatic pause (keep).
    Falls back to the full unfiltered list on any error — never drops cuts silently.
    """
    if not silence_cuts:
        return silence_cuts

    import json as _json
    from openai import AsyncOpenAI

    cut_contexts = []
    for i, cut in enumerate(silence_cuts):
        before = [e for e in srt_global if e["end"] <= cut["at"]][-2:]
        after  = [e for e in srt_global if e["start"] >= cut["end"]][:2]
        cut_contexts.append({
            "index":    i,
            "at":       cut["at"],
            "end":      cut["end"],
            "duration": round(cut["end"] - cut["at"], 1),
            "before":   " ".join(f'"{e["text"]}"' for e in before) or "(start of video)",
            "after":    " ".join(f'"{e["text"]}"' for e in after) or "(end of video)",
        })

    cuts_text = "\n".join(
        f'{c["index"] + 1}. [{c["at"]:.1f}s–{c["end"]:.1f}s, {c["duration"]}s]\n'
        f'   Before: {c["before"]}\n'
        f'   After:  {c["after"]}'
        for c in cut_contexts
    )

    prompt = (
        f"You are reviewing proposed silence/pause cuts in a {total_duration:.0f}s social video.\n"
        f"For each gap you see what was said immediately before and after.\n"
        f"Decide: CUT (dead air, lost train of thought, awkward gap) or "
        f"KEEP (intentional — dramatic hold, reaction beat, building tension before a reveal).\n\n"
        f"GAPS TO REVIEW:\n{cuts_text}\n\n"
        f"Return ONLY valid JSON, no markdown:\n"
        f'{{"verdicts":[{{"index":0,"decision":"cut"|"keep","reason":"<short>"}}]}}\n\n'
        f"Rules:\n"
        f"- Gap before a punchline, number reveal, or emotional peak → KEEP\n"
        f"- Gap mid-sentence where speaker clearly lost their train of thought → CUT\n"
        f"- Gap > 3s → almost always CUT unless context strongly suggests a dramatic hold\n"
        f"- 'uncertain speech' entries: Whisper transcribed something here but its own "
        f"confidence was low (no_speech_prob > 0.5 or avg_logprob < -0.8). "
        f"CUT if surrounding context suggests a false start, mumble, or restart. "
        f"KEEP only if the before/after context reads like real, intentional speech.\n"
        f"- When uncertain → CUT (tighter is better for social video)\n"
        f"Only the JSON object."
    )

    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await client.chat.completions.create(
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.rsplit("```", 1)[0].strip()
        verdicts: Dict[int, str] = {
            v["index"]: v["decision"]
            for v in _json.loads(raw).get("verdicts", [])
            if "index" in v and "decision" in v
        }
    except Exception as exc:
        print(f"[MultiClip/refine_cuts] GPT error: {exc} — passing all cuts through", flush=True)
        return silence_cuts

    approved = [silence_cuts[c["index"]] for c in cut_contexts if verdicts.get(c["index"], "cut") == "cut"]
    n_protected = len(silence_cuts) - len(approved)
    print(
        f"[MultiClip/refine_cuts] {len(silence_cuts)} silence cuts → "
        f"{len(approved)} confirmed, {n_protected} protected as intentional pauses",
        flush=True,
    )
    return approved


async def _run_ai_analysis(clips_ordered: List[Dict], story_type: str) -> Dict:
    """
    Analyze ordered clips and return {cuts, zooms, transition_style, summary}.

    Three-layer approach:
    1. Timing-based: FFmpeg silencedetect + Whisper word-gaps detect every silence
       and pause deterministically (no GPT, measures actual audio).
    2. LLM refinement: GPT-4o-mini audits each silence/pause cut using transcript
       context — protects intentional dramatic pauses from being removed.
    3. GPT content analysis: contextual cuts (repetitions, tangents), zoom moments,
       transition style, summary.

    Results are merged and deduplicated before returning.
    """
    import json as _json
    from openai import AsyncOpenAI

    # ── Layer 1: timing-based silence, pause, filler, repetition detection ───
    timing_cuts: List[Dict] = []
    cumulative = 0.0
    clip_entries = []
    for c in clips_ordered:
        if c.get("dropped"):
            continue
        dur = float(c.get("duration_seconds") or 10)
        srt = c.get("srt") or ""
        clip_type = c.get("clip_type", "speech")
        clip_words = c.get("words") or []
        clip_silence_ranges = c.get("silence_ranges") or []
        clip_uncertain = c.get("uncertain_segments") or []
        # Parse SRT entries with global timestamps for GPT input
        srt_entries_global = []
        if srt or clip_words or clip_silence_ranges or clip_uncertain:
            timing_cuts.extend(
                _timing_cuts_from_clip(
                    srt, cumulative, dur, clip_type,
                    clip_words or None,
                    clip_silence_ranges or None,
                    clip_uncertain or None,
                )
            )
            for e in _srt_parse(srt):
                srt_entries_global.append({
                    "start": round(cumulative + e["start"], 2),
                    "end":   round(cumulative + e["end"],   2),
                    "text":  e["text"],
                })
        clip_entries.append({
            "start":      cumulative,
            "end":        cumulative + dur,
            "clip_type":  clip_type,
            "transcript": (c.get("transcript") or "").strip(),   # full text, no truncation
            "srt_lines":  srt_entries_global,
        })
        cumulative += dur

    if not clip_entries:
        return {"cuts": [], "zooms": [], "transition_style": "fade", "summary": ""}

    total_duration = cumulative

    # ── Layer 1.5: LLM refinement — protect intentional dramatic pauses ───────
    # Filler and repeat cuts are always correct; only silence/pause cuts need
    # context-aware judgment (a 2s gap before a revenue reveal should stay).
    # uncertain speech goes through the refinement layer too — the LLM has context
    # to judge whether Whisper's low-confidence transcription was a real false start
    # or just quiet but genuine speech.
    _silence_reasons = ("silence", "pause", "leading silence", "trailing silence", "uncertain speech")
    silence_pause_cuts = [c for c in timing_cuts if any(c["reason"].startswith(p) for p in _silence_reasons)]
    filler_repeat_cuts = [c for c in timing_cuts if not any(c["reason"].startswith(p) for p in _silence_reasons)]

    all_srt_global = [entry for clip in clip_entries for entry in clip["srt_lines"]]
    refined_silence_cuts = await _refine_silence_cuts(silence_pause_cuts, all_srt_global, total_duration)
    timing_cuts = refined_silence_cuts + filler_repeat_cuts

    # ── Layer 2: GPT content analysis with timestamped transcript ─────────────
    # Build a per-clip block showing every SRT line with its exact timestamps.
    # GPT uses these timestamps directly for cut coordinates.
    clip_blocks: List[str] = []
    for i, e in enumerate(clip_entries):
        header = f"Clip {i + 1} ({e['start']:.1f}s–{e['end']:.1f}s, {e['clip_type']})"
        if e["srt_lines"]:
            lines = "\n".join(
                f"  [{sl['start']:.1f}s–{sl['end']:.1f}s] \"{sl['text']}\""
                for sl in e["srt_lines"]
            )
            clip_blocks.append(f"{header}:\n{lines}")
        elif e["transcript"]:
            clip_blocks.append(f"{header}: \"{e['transcript']}\"")
        else:
            clip_blocks.append(f"{header}: (no speech)")
    clips_text = "\n\n".join(clip_blocks)

    already_cut_str = (
        "Already cut by timing analysis (silences/pauses/fillers/repeats detected from audio timing):\n"
        + "\n".join(f"  {c['at']:.1f}s–{c['end']:.1f}s — {c['reason']}" for c in timing_cuts[:12])
        if timing_cuts else "No timing-based cuts detected yet."
    )

    prompt = (
        f"You are a professional video editor cutting a {story_type} video ({total_duration:.0f}s).\n"
        f"The transcript below shows every spoken sentence with its exact timestamp in the final video.\n\n"
        f"TRANSCRIPT:\n{clips_text}\n\n"
        f"ALREADY HANDLED:\n{already_cut_str}\n\n"
        f"Return ONLY valid JSON — no markdown, no extra text:\n"
        f'{{"cuts":[{{"at":<float>,"end":<float>,"reason":"<why>"}}],'
        f'"zooms":[{{"at":<float>,"duration":<float>,"reason":"<phrase>"}}],'
        f'"transition_style":"cut"|"fade"|"slide"|"zoom",'
        f'"summary":"<one sentence>"}}\n\n'
        f"RULES FOR CUTS — be aggressive, this should feel tight:\n"
        f"1. Repeated sentences or phrases: if the same idea or sentence (≥4 key words) appears "
        f"more than once, cut every occurrence AFTER the first. Use the exact [Xs–Ys] timestamps shown.\n"
        f"2. Meandering or off-topic tangents: sentences that don't advance the main point.\n"
        f"3. Contradictions or take-backs: 'actually no, what I meant was...' — cut the false start.\n"
        f"4. Do NOT cut ranges already listed in ALREADY HANDLED.\n"
        f"5. Do NOT cut anything not in the transcript. at >= 0, end <= {total_duration:.1f}.\n"
        f"6. Each kept segment between cuts must be ≥ 2s.\n\n"
        f"RULES FOR ZOOMS:\n"
        f"- 1–3 moments where the speaker makes their strongest or most emotional point.\n"
        f"- duration 1.0–3.0s. Must fall within a clip that has speech.\n\n"
        f"transition_style: match the energy — founder → 'cut' or 'slide', product → 'fade' or 'zoom'.\n"
        f"Only the JSON object."
    )

    gpt_cuts: List[Dict] = []
    gpt_zooms: List[Dict] = []
    transition_style = "fade"
    summary = ""

    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await client.chat.completions.create(
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1200,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.rsplit("```", 1)[0].strip()
        gpt = _json.loads(raw)
        gpt_cuts = gpt.get("cuts", [])
        gpt_zooms = gpt.get("zooms", [])
        transition_style = gpt.get("transition_style", "fade")
        summary = gpt.get("summary", "")
    except Exception as exc:
        print(f"[MultiClip/_run_ai_analysis] GPT error: {exc}", flush=True)

    # ── Merge and deduplicate ─────────────────────────────────────────────────
    all_cuts = timing_cuts + gpt_cuts
    merged = _merge_cuts(all_cuts)
    print(
        f"[MultiClip/analyze] timing_cuts={len(timing_cuts)} gpt_cuts={len(gpt_cuts)} "
        f"merged={len(merged)} zooms={len(gpt_zooms)}",
        flush=True,
    )
    return {"cuts": merged, "zooms": gpt_zooms, "transition_style": transition_style, "summary": summary}


def _build_founder_timeline(
    clips_ordered: List[Dict],
    merged_srt: str,
    aspect_ratio: str = "9:16",
    music_url: str = "",
    primary_color: str = "#CD1B78",
    job_id: str = "",
    mute_clips: bool = False,
    target_duration: float = 0,
    music_volume: float = 0.12,
    ai_transition_style: str = "fade",
    ai_zoom_moments: Optional[List[Dict]] = None,
    ai_cuts: Optional[List[Dict]] = None,
) -> Dict:
    """
    Build a Shotstack timeline for a multi-clip stitch.

    Each clip may be split into sub-segments by ai_cuts (removing filler sections).
    Segments alternate slideLeft/slideRight; zoom moments get the cinematic rotate+skew
    transform from the produce section. Transitions between clips use the AI-chosen style.
    Captions come from the merged SRT. Music track spans the full duration.
    """
    tracks: List[Dict] = []
    total_footage = sum(c["duration_seconds"] for c in clips_ordered)

    # Trimming applies whenever a target duration is set (both Founder and Product Story)
    trim_ratio = 1.0
    if target_duration > 0 and total_footage > 0:
        trim_ratio = min(1.0, target_duration / total_footage)

    # Resolve AI transition (None = hard cut between clips)
    _border_transition: Optional[Dict] = None
    if ai_transition_style == "cut":
        _border_transition = None
    elif ai_transition_style == "zoom":
        _border_transition = {"in": "zoom"}
    elif ai_transition_style == "slide":
        _border_transition = {"in": "slideLeft"}
    else:
        _border_transition = {"in": "fade"}

    # Map GPT original-timeline zoom timestamps → clip indices
    # GPT analyzed untrimmed durations, so use original durations for matching.
    zoom_clip_indices: set = set()
    if ai_zoom_moments:
        t = 0.0
        for idx, c in enumerate(clips_ordered):
            orig_dur = c["duration_seconds"]
            for zm in ai_zoom_moments:
                zm_at = float(zm.get("at", 0))
                if t <= zm_at < t + orig_dur:
                    zoom_clip_indices.add(idx)
            t += orig_dur

    # ── Track: video clips ─────────────────────────────────────────────────────
    # Each clip is expanded into keep-segments after applying ai_cuts within it.
    video_clips: List[Dict] = []
    global_cursor = 0.0
    seg_index = 0          # counts all output segments for alternating slide direction
    cut_ranges: List[tuple] = []   # global (start, end) ranges removed — used for caption filtering

    for clip_idx, clip in enumerate(clips_ordered):
        orig_dur = clip["duration_seconds"]
        new_dur = round(orig_dur * trim_ratio, 3)
        center_trim = round((orig_dur - new_dur) / 2.0, 3) if trim_ratio < 1.0 else 0.0

        position = clip.get("subject_position", "center")
        has_zoom = clip_idx in zoom_clip_indices
        clip_global_start = global_cursor

        # Collect ai_cuts that overlap with this clip's trimmed window
        clip_local_cuts: List[tuple] = []
        for cut in (ai_cuts or []):
            cut_at = float(cut.get("at", 0))
            cut_end = float(cut.get("end", 0))
            local_at = cut_at - clip_global_start
            local_end = cut_end - clip_global_start
            if local_end > 0 and local_at < new_dur:
                clip_local_cuts.append((max(0.0, local_at), min(new_dur, local_end)))
        clip_local_cuts.sort(key=lambda x: x[0])

        # Build keep-parts (sections of this clip NOT removed by cuts)
        keep_parts: List[tuple] = []
        prev = 0.0
        for (cs, ce) in clip_local_cuts:
            if cs > prev:
                keep_parts.append((prev, cs))
            # Record removed range in global coordinates for caption filtering
            cut_ranges.append((clip_global_start + cs, clip_global_start + ce))
            prev = ce
        if prev < new_dur:
            keep_parts.append((prev, new_dur))
        if not keep_parts:
            keep_parts = [(0.0, new_dur)]

        for part_idx, (local_start, local_end) in enumerate(keep_parts):
            seg_len = round(local_end - local_start, 3)
            if seg_len < 0.1:
                continue
            src_trim = round(center_trim + local_start, 3)
            is_first_seg_of_clip = part_idx == 0
            is_clip_boundary = clip_idx > 0 and is_first_seg_of_clip

            if clip.get("clip_type") == "still":
                clip_entry: Dict = {
                    "asset": {"type": "image", "src": clip["cloudinary_url"]},
                    "start": round(global_cursor, 3),
                    "length": seg_len,
                    "effect": "zoomIn",
                    "fit": "crop",
                    "position": position,
                }
            else:
                clip_vol = 0.0 if mute_clips else min(1.0, clip.get("volume_boost", 1.0))
                asset: Dict = {
                    "type": "video",
                    "src": clip["cloudinary_url"],
                    "volume": round(clip_vol, 3),
                }
                if src_trim > 0:
                    asset["trim"] = src_trim

                base_effect = "slideLeft" if seg_index % 2 == 0 else "slideRight"
                effect = "zoomIn" if has_zoom else base_effect

                clip_entry = {
                    "asset": asset,
                    "start": round(global_cursor, 3),
                    "length": seg_len,
                    "fit": "crop",
                    "position": position,
                    "effect": effect,
                }

                # Cinematic jank-zoom from the produce section — visible on the most
                # impactful moment in each zoom clip
                if has_zoom and is_first_seg_of_clip:
                    clip_entry["transform"] = {
                        "rotate": {
                            "angle": [
                                {"from": 6.0, "to": -3.0, "start": 0, "length": 0.35,
                                 "interpolation": "bezier", "easing": "easeOutBack"},
                                {"from": -3.0, "to": 0, "start": 0.35, "length": 0.2,
                                 "interpolation": "bezier", "easing": "easeOutCubic"},
                            ]
                        },
                        "skew": {
                            "x": [
                                {"from": 0.07, "to": 0, "start": 0, "length": 0.3,
                                 "interpolation": "bezier", "easing": "easeOutCubic"},
                            ]
                        },
                    }

            # Clip-boundary transitions only; within-clip cut segments hard-cut
            if is_clip_boundary and _border_transition is not None:
                clip_entry["transition"] = _border_transition

            video_clips.append(clip_entry)
            global_cursor = round(global_cursor + seg_len, 3)
            seg_index += 1

    total_duration = global_cursor
    tracks.append({"clips": video_clips})

    # ── Track: captions ───────────────────────────────────────────────────────
    caption_clips: List[Dict] = []
    if merged_srt.strip():
        # Parse SRT into caption clips
        def _parse_ts(ts: str) -> float:
            h, m, rest = ts.split(":")
            s, ms = rest.replace(",", ".").split(".")
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

        blocks = [b.strip() for b in merged_srt.strip().split("\n\n") if b.strip()]
        for block in blocks:
            lines = block.split("\n")
            if len(lines) < 3:
                continue
            timing_line = lines[1]
            if " --> " not in timing_line:
                continue
            parts = timing_line.split(" --> ")
            t_start = _parse_ts(parts[0].strip())
            t_end = _parse_ts(parts[1].strip())
            caption_text = " ".join(lines[2:]).strip()
            cap_dur = max(0.5, t_end - t_start)
            if t_start >= total_duration:
                continue
            # Skip captions that fall within a cut region
            if any(cs <= t_start < ce for (cs, ce) in cut_ranges):
                continue
            caption_clips.append({
                "asset": {
                    "type": "html",
                    "html": (
                        f"<!DOCTYPE html><html><head><meta charset='UTF-8'>"
                        f"<style>*{{margin:0;padding:0;box-sizing:border-box;}}"
                        f"body{{width:720px;background:transparent;overflow:hidden;}}"
                        f"p{{font-family:'Arial Black',Arial,sans-serif;font-size:42px;"
                        f"font-weight:900;color:#fff;text-align:center;"
                        f"text-shadow:0 2px 8px rgba(0,0,0,0.85);"
                        f"line-height:1.2;word-wrap:break-word;padding:8px 24px;}}"
                        f"</style></head>"
                        f"<body><p>{caption_text}</p></body></html>"
                    ),
                    "width": 720,
                    "height": 160,
                },
                "start": round(t_start, 3),
                "length": round(min(cap_dur, total_duration - t_start), 3),
                "position": "bottom",
                "offset": {"x": 0.0, "y": 0.1},
            })

    if caption_clips:
        tracks.append({"clips": caption_clips})

    # ── Track: music ──────────────────────────────────────────────────────────
    if music_url:
        tracks.append({
            "clips": [{
                "asset": {"type": "audio", "src": music_url, "volume": round(music_volume, 3)},
                "start": 0,
                "length": round(total_duration, 3),
            }]
        })

    return {
        "timeline": {"tracks": tracks},
        "output": {
            "format": "mp4",
            "resolution": "hd",
            "quality": "high",
            "aspectRatio": aspect_ratio,
            "fps": 30,
        },
    }


# ── Product Story: Vision description ────────────────────────────────────────

async def _vision_describe_clip(tmp_path: str, clip_id: str) -> Dict[str, Any]:
    """
    Extract a frame and ask GPT-4o-vision what this product clip shows.
    Returns {clip_id, shows, shot_type, role}
    shot_type: attention_shot | detail_closeup | benefit_context | packaging | cta_shot | general
    """
    frame_bytes = await _extract_frame_bytes(tmp_path, t=1.0)
    if not frame_bytes:
        return {"clip_id": clip_id, "shows": "product footage", "shot_type": "general", "role": "general"}

    frame_b64 = base64.b64encode(frame_bytes).decode()
    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    prompt = """Analyze this product video frame. Return JSON only:
{
  "shows": "brief 1-sentence description of what this clip shows",
  "shot_type": "one of: attention_shot | detail_closeup | benefit_context | packaging | cta_shot | general",
  "role": "one of: hook | detail | benefit | social_proof | cta | general"
}

Shot type guide:
- attention_shot: eye-catching hero / overview of the product
- detail_closeup: tight close-up of texture, feature, or label detail
- benefit_context: product in use or showing a clear benefit
- packaging: packaging, unboxing, or label shot
- cta_shot: price tag, sale sign, or call-to-action element visible
- general: anything else"""

    try:
        resp = await client.chat.completions.create(
            model="gpt-5.6-luna",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}", "detail": "low"}},
                    {"type": "text", "text": prompt},
                ],
            }],
            max_tokens=200,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        return {
            "clip_id": clip_id,
            "shows": parsed.get("shows", "product footage"),
            "shot_type": parsed.get("shot_type", "general"),
            "role": parsed.get("role", "general"),
        }
    except Exception as e:
        print(f"[MultiClip] vision describe error clip={clip_id}: {e}", flush=True)
        return {"clip_id": clip_id, "shows": "product footage", "shot_type": "general", "role": "general"}


# ── Product Story: Script drafting ───────────────────────────────────────────

async def _draft_product_script(description: str, clip_count: int) -> Dict[str, Any]:
    """
    Draft a short product video script from a user description.
    Returns {draft: str, lines: List[str]}
    """
    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    n = max(2, min(clip_count, 6))

    prompt = f"""You are Jane, URI Social's AI social media manager.
A business owner wants a short product video with {n} clip(s).
Their description: "{description}"

Write a punchy social media video script for this product.
The script lines will be shown as captions on the video AND guide the voiceover.

Rules:
- Write exactly {n} lines — one per clip
- Each line is 5-10 words — short, readable as an on-screen caption
- Brand voice: direct, energetic, confident
- Structure: attention/hook → feature/detail → benefit → price or CTA
- Do NOT include stage directions, clip numbers, labels, or colons — just the script text

Return JSON only:
{{
  "draft": "full script with each line separated by \\n",
  "lines": ["line 1", "line 2", ...]
}}"""

    try:
        resp = await client.chat.completions.create(
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.7,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        lines = parsed.get("lines", [])
        draft = parsed.get("draft", "\n".join(lines))
        return {"draft": draft, "lines": lines}
    except Exception as e:
        print(f"[MultiClip] script draft error: {e}", flush=True)
        return {"draft": "", "lines": []}


# ── Product Story: Vision-based ordering ─────────────────────────────────────

def _suggest_product_order_from_vision(clips: List[Dict]) -> List[str]:
    """Order product clips along showcase arc using their shot_type."""
    SHOT_RANK = {
        "attention_shot": 0,
        "detail_closeup": 2,
        "benefit_context": 3,
        "packaging": 4,
        "cta_shot": 5,
        "general": 3,
    }
    sorted_clips = sorted(clips, key=lambda c: SHOT_RANK.get(c.get("shot_type", "general"), 3))
    return [c["clip_id"] for c in sorted_clips]


# ── Product Story: Script lines → SRT ────────────────────────────────────────

def _script_to_srt(script_lines: List[str], clips_ordered: List[Dict]) -> str:
    """
    Distribute script lines as SRT entries — one line per clip.
    Each caption spans the full duration of its assigned clip.
    """
    if not script_lines or not clips_ordered:
        return ""

    def _fmt(t: float) -> str:
        t = max(0.0, t)
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int(round((t % 1) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    srt_parts: List[str] = []
    cursor = 0.0
    n_lines = len(script_lines)

    for i, clip in enumerate(clips_ordered):
        line_idx = min(i, n_lines - 1)
        text = script_lines[line_idx].strip()
        dur = clip["duration_seconds"]
        if text:
            t_start = cursor
            t_end = cursor + dur
            srt_parts.append(str(i + 1))
            srt_parts.append(f"{_fmt(t_start)} --> {_fmt(t_end)}")
            srt_parts.append(text)
            srt_parts.append("")
        cursor += dur

    return "\n".join(srt_parts)


# ── Product Story: Vision phase background task ───────────────────────────────

async def run_product_vision_phase(job_id: str, db) -> None:
    """
    Background task called after the user approves the script.
    Vision-describes each clip, suggests order, sets status=awaiting_order.
    Clips are fetched from Cloudinary by re-extracting frames from existing temp files
    — but we don't have them anymore, so we download from Cloudinary instead.
    """
    print(f"[MultiClip] vision phase start job={job_id}", flush=True)

    async def update(progress: int, msg: str, **extra):
        await _update_job(db, job_id, progress=progress, status_message=msg, **extra)

    try:
        doc = await db.multi_clip_jobs.find_one({"job_id": job_id})
        if not doc:
            raise RuntimeError("Job not found")

        clips: List[Dict] = doc.get("clips", [])
        if not clips:
            raise RuntimeError("No clips to describe")

        await update(62, "Describing clips with AI vision…")

        # Download each clip from Cloudinary and vision-describe it
        async with aiohttp.ClientSession() as session:
            for i, clip in enumerate(clips):
                pct = 62 + int((i / len(clips)) * 20)
                await update(pct, f"Analysing clip {i + 1} of {len(clips)} with vision…")

                cloud_url = clip.get("cloudinary_url") or clip.get("original_url")
                if not cloud_url:
                    continue

                try:
                    async with session.get(cloud_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if not resp.ok:
                            continue
                        video_bytes = await resp.read()

                    suffix = ".mp4"
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                        tf.write(video_bytes)
                        tmp_path = tf.name

                    try:
                        vision_data = await _vision_describe_clip(tmp_path, clip["clip_id"])
                        clip["vision_description"] = vision_data["shows"]
                        clip["shot_type"] = vision_data["shot_type"]
                        clip["vision_role"] = vision_data["role"]
                        print(f"[MultiClip] vision clip={clip['clip_id']} type={vision_data['shot_type']}", flush=True)
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                except Exception as e:
                    print(f"[MultiClip] vision download error clip={clip['clip_id']}: {e}", flush=True)

        # Order clips by vision shot type
        await update(83, "Ordering clips by visual arc…")
        suggested_ids = _suggest_product_order_from_vision(clips)
        for rank, cid in enumerate(suggested_ids):
            for c in clips:
                if c["clip_id"] == cid:
                    c["order_index"] = rank

        await update(
            88,
            "Ready to review",
            status="awaiting_order",
            clips=clips,
            suggested_order=suggested_ids,
        )
        print(f"[MultiClip] vision phase done job={job_id} suggested_order={suggested_ids}", flush=True)

    except Exception as exc:
        print(f"[MultiClip] vision phase FAILED job={job_id}: {exc}", flush=True)
        await _update_job(db, job_id, status="failed", status_message=str(exc), progress=0)


# ── DB update helper ──────────────────────────────────────────────────────────

async def _update_job(db, job_id: str, **fields) -> None:
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.multi_clip_jobs.update_one({"job_id": job_id}, {"$set": fields})


# ── Phase 1: Ingest background task ──────────────────────────────────────────

async def _ingest_one_clip(
    filename: str,
    raw_bytes: bytes,
    clip_id: str,
    order_index: int,
    story_type: str,
) -> Dict:
    """Process a single clip end-to-end. Safe to run concurrently."""
    ext = os.path.splitext(filename)[1].lower()

    if ext in _IMAGE_EXTS:
        cloud_url = await _upload_clip_to_cloudinary(raw_bytes, clip_id, filename)
        return {
            "clip_id": clip_id,
            "filename": filename,
            "original_url": cloud_url or "",
            "cloudinary_url": cloud_url or "",
            "order_index": order_index,
            "duration_seconds": _STILL_DEFAULT_DURATION,
            "width": 0, "height": 0,
            "clip_type": "still",
            "has_face": False,
            "quality_flags": [] if cloud_url else ["upload_failed"],
            "volume_boost": 1.0,
            "subject_position": "center",
            "recommended_drop": False,
            "drop_reason": None,
            "transcript": "", "srt": "", "words": [],
            "frame_url": None,
            "vision_description": None,
            "vision_role": None,
        }

    suffix = ext if filename.lower().endswith((".mov", ".avi", ".mkv", ".webm")) else ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(raw_bytes)
        tmp_path = tf.name

    try:
        probe = await _probe_clip(tmp_path)
        quality_flags, mean_db = await _check_quality(tmp_path, probe)
        clip_type = await _detect_clip_type(tmp_path, probe)
        volume_boost = _compute_volume_boost(mean_db) if clip_type == "speech" else 1.0

        # Run subject detection + cloudinary upload + silence detection in parallel.
        # _detect_silences_ffmpeg runs on all speech clips regardless of story type —
        # it only needs the local file and produces silence_ranges used at stitch time.
        if story_type == "founder" and clip_type == "speech":
            subject_pos, cloud_url, transcript_data, silence_ranges = await asyncio.gather(
                _detect_subject_position(tmp_path),
                _upload_clip_to_cloudinary(raw_bytes, clip_id, filename),
                _transcribe_clip(tmp_path),
                _detect_silences_ffmpeg(tmp_path),
            )
        elif clip_type == "speech":
            subject_pos, cloud_url, silence_ranges = await asyncio.gather(
                _detect_subject_position(tmp_path),
                _upload_clip_to_cloudinary(raw_bytes, clip_id, filename),
                _detect_silences_ffmpeg(tmp_path),
            )
            transcript_data = {"text": "", "srt": "", "words": [], "uncertain_segments": []}
        else:
            subject_pos, cloud_url = await asyncio.gather(
                _detect_subject_position(tmp_path),
                _upload_clip_to_cloudinary(raw_bytes, clip_id, filename),
            )
            transcript_data = {"text": "", "srt": "", "words": [], "uncertain_segments": []}
            silence_ranges = []

        if not cloud_url:
            quality_flags.append("upload_failed")

        print(
            f"[MultiClip] clip {clip_id}: type={clip_type} dur={probe['duration']:.1f}s "
            f"pos={subject_pos} flags={quality_flags}",
            flush=True,
        )
        return {
            "clip_id": clip_id,
            "filename": filename,
            "original_url": cloud_url or "",
            "cloudinary_url": cloud_url or "",
            "order_index": order_index,
            "duration_seconds": round(probe["duration"], 2),
            "width": probe["width"],
            "height": probe["height"],
            "clip_type": clip_type,
            "has_face": False,
            "quality_flags": quality_flags,
            "volume_boost": volume_boost,
            "subject_position": subject_pos,
            "recommended_drop": "too_dark" in quality_flags,
            "drop_reason": (
                "Clip is too dark — it may drag down the overall video quality."
                if "too_dark" in quality_flags else None
            ),
            "transcript": transcript_data["text"],
            "srt": transcript_data["srt"],
            "words": transcript_data["words"],
            "uncertain_segments": transcript_data.get("uncertain_segments", []),
            "silence_ranges": silence_ranges,
            "frame_url": None,
            "vision_description": None,
            "vision_role": None,
        }
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


async def run_multi_clip_ingest(job_id: str, clips_bytes: List[Tuple[str, bytes]], db) -> None:
    """
    Background task: probe + analyse + upload all clips in parallel,
    then suggest ordering via GPT-4o and save to DB.
    """
    print(f"[MultiClip] ingest start job={job_id} clips={len(clips_bytes)}", flush=True)

    async def update(progress: int, msg: str, **extra):
        await _update_job(db, job_id, progress=progress, status_message=msg, **extra)

    try:
        job_doc = await db.multi_clip_jobs.find_one({"job_id": job_id})
        story_type = job_doc.get("story_type", "founder") if job_doc else "founder"
        n = len(clips_bytes)

        await update(10, f"Analysing {n} clip{'s' if n > 1 else ''}…")

        clip_ids = [str(uuid.uuid4())[:8] for _ in clips_bytes]
        done_count = 0

        async def _tracked(filename: str, raw_bytes: bytes, clip_id: str, idx: int) -> Dict:
            nonlocal done_count
            result = await _ingest_one_clip(filename, raw_bytes, clip_id, idx, story_type)
            done_count += 1
            pct = 10 + int((done_count / n) * 55)
            await update(pct, f"Analysed {done_count} of {n} clip{'s' if n > 1 else ''}…")
            return result

        clip_docs: List[Dict] = list(await asyncio.gather(*[
            _tracked(filename, raw_bytes, clip_id, i)
            for i, ((filename, raw_bytes), clip_id) in enumerate(zip(clips_bytes, clip_ids))
        ]))

        # ── Length budget ─────────────────────────────────────────────────────────
        target_seconds = float(job_doc.get("target_duration_seconds", 30) if job_doc else 30)
        total_footage = sum(c["duration_seconds"] for c in clip_docs)
        budget_ratio = total_footage / max(target_seconds, 1.0)

        if budget_ratio > 2.5:
            budget_rec = "trim_heavy"
            budget_msg = (
                f"You gave me {total_footage:.0f}s of footage for a {int(target_seconds)}s video. "
                "I'll keep the best part of each clip — want a longer target instead?"
            )
        elif budget_ratio > 1.2:
            budget_rec = "trim_light"
            budget_msg = (
                f"You have {total_footage:.0f}s of footage for a {int(target_seconds)}s target — "
                "I'll trim each clip lightly."
            )
        elif budget_ratio < 0.5:
            budget_rec = "short"
            budget_msg = (
                f"You only have {total_footage:.0f}s of footage for a {int(target_seconds)}s target. "
                "The video will be shorter — consider adding more clips."
            )
        else:
            budget_rec = "ok"
            budget_msg = ""

        length_budget_info: Dict = {
            "total_footage_seconds": round(total_footage, 1),
            "target_seconds": int(target_seconds),
            "ratio": round(budget_ratio, 2),
            "recommendation": budget_rec,
            "message": budget_msg,
        }

        # ── Mismatch detection ────────────────────────────────────────────────────
        speech_count = sum(1 for c in clip_docs if c["clip_type"] == "speech")
        silent_count = sum(1 for c in clip_docs if c["clip_type"] == "silent")
        still_count  = sum(1 for c in clip_docs if c["clip_type"] == "still")
        total_count  = len(clip_docs)

        mismatch_info: Optional[Dict] = None
        if story_type == "founder" and speech_count == 0 and silent_count + still_count == total_count:
            mismatch_info = {
                "type": "no_speech_for_founder",
                "message": (
                    "None of your clips have speech. Founder Story works best with you talking. "
                    "Consider switching to Product Story — it uses a written script instead."
                ),
            }
        elif story_type == "product" and speech_count > 0:
            mismatch_info = {
                "type": "speech_in_product",
                "message": (
                    f"{speech_count} of your clips contain spoken audio. "
                    "Product Story mutes all clips and uses your script as captions. "
                    "The existing audio can be added as a voiceover after stitching."
                ),
            }

        if story_type == "product":
            # Product Story: stop here and wait for user to provide a script description
            await update(
                60,
                "Clips ready — write your script",
                status="awaiting_script",
                clips=clip_docs,
                suggested_order=[c["clip_id"] for c in clip_docs],
                mismatch_info=mismatch_info,
                length_budget_info=length_budget_info,
            )
            print(f"[MultiClip] ingest done (product) job={job_id} — awaiting script", flush=True)
        else:
            # Founder Story: suggest narrative ordering from transcripts
            await update(60, "Suggesting clip order…")
            speech_clips = [c for c in clip_docs if c["clip_type"] == "speech"]
            if len(speech_clips) > 1:
                suggested_ids = await _suggest_founder_order(clip_docs)
            else:
                suggested_ids = [c["clip_id"] for c in clip_docs]

            for rank, cid in enumerate(suggested_ids):
                for c in clip_docs:
                    if c["clip_id"] == cid:
                        c["order_index"] = rank

            await update(
                70,
                "Ready to review",
                status="awaiting_order",
                clips=clip_docs,
                suggested_order=suggested_ids,
                mismatch_info=mismatch_info,
                length_budget_info=length_budget_info,
            )
            print(f"[MultiClip] ingest done job={job_id} suggested_order={suggested_ids}", flush=True)

    except Exception as exc:
        print(f"[MultiClip] ingest FAILED job={job_id}: {exc}", flush=True)
        await _update_job(db, job_id, status="failed", status_message=str(exc), progress=0)


# ── Phase 1: Stitch background task ──────────────────────────────────────────

async def run_multi_clip_stitch(job_id: str, db) -> None:
    """
    Background task: fetch job from DB, order clips per order_index,
    merge SRTs, build Shotstack timeline, render, save output_url.
    """
    print(f"[MultiClip] stitch start job={job_id}", flush=True)

    async def update(progress: int, msg: str, **extra):
        await _update_job(db, job_id, progress=progress, status_message=msg, **extra)

    try:
        doc = await db.multi_clip_jobs.find_one({"job_id": job_id})
        if not doc:
            raise RuntimeError("Job not found")

        clips: List[Dict] = doc.get("clips", [])
        # Filter dropped clips
        active_clips = [c for c in clips if not c.get("dropped", False)]
        if not active_clips:
            raise RuntimeError("No clips to stitch — all clips were dropped")

        # Sort by order_index
        active_clips.sort(key=lambda c: c.get("order_index", 0))

        # Validate all clips have a cloudinary_url
        for c in active_clips:
            if not c.get("cloudinary_url"):
                raise RuntimeError(f"Clip {c['clip_id']} has no URL — upload may have failed")

        story_type = doc.get("story_type", "founder")
        target_duration = float(doc.get("target_duration_seconds", 0))

        if story_type == "product":
            await update(72, "Applying script captions…")
            script_lines = doc.get("script_lines", [])
            # Build SRT using trimmed durations so captions match the output timing
            if target_duration > 0:
                total_footage = sum(c["duration_seconds"] for c in active_clips)
                trim_ratio = min(1.0, target_duration / max(total_footage, 1.0))
                srt_clips = [
                    {**c, "duration_seconds": round(c["duration_seconds"] * trim_ratio, 3)}
                    for c in active_clips
                ]
            else:
                srt_clips = active_clips
            merged_srt = _script_to_srt(script_lines, srt_clips)
            mute_clips = True
        else:
            await update(72, "Merging transcripts…")
            if target_duration > 0:
                # Center-cut each clip to fit the target, then trim its SRT to match
                total_footage = sum(c["duration_seconds"] for c in active_clips)
                tr = min(1.0, target_duration / max(total_footage, 1.0))
                srt_clips = []
                for c in active_clips:
                    orig_dur = c["duration_seconds"]
                    new_dur = round(orig_dur * tr, 3)
                    trim_start = round((orig_dur - new_dur) / 2.0, 3) if tr < 1.0 else 0.0
                    trimmed_srt = _trim_srt(c.get("srt") or "", trim_start, new_dur)
                    srt_clips.append({**c, "srt": trimmed_srt, "duration_seconds": new_dur})
                merged_srt = _merge_srts(srt_clips)
            else:
                merged_srt = _merge_srts(active_clips)
            mute_clips = False

        await update(75, "Selecting music…")
        music_url = ""
        if doc.get("enable_music", True):
            music_mood = doc.get("music_mood", "chill")
            music_url = _pick_music_url(music_mood)

        await update(76, "Analyzing editing style…")
        ai_decisions = await _run_ai_analysis(active_clips, story_type)
        await db.multi_clip_jobs.update_one(
            {"job_id": job_id},
            {"$set": {"ai_decisions": ai_decisions}},
        )
        print(
            f"[MultiClip] AI decisions: transition={ai_decisions.get('transition_style')} "
            f"zooms={len(ai_decisions.get('zooms', []))} cuts={len(ai_decisions.get('cuts', []))}",
            flush=True,
        )

        await update(79, "Building timeline…")
        aspect_ratio = doc.get("orientation", "9:16")
        primary_color = doc.get("primary_color", "#CD1B78")
        music_volume = float(doc.get("music_volume", 0.12))
        timeline = _build_founder_timeline(
            clips_ordered=active_clips,
            merged_srt=merged_srt,
            aspect_ratio=aspect_ratio,
            music_url=music_url,
            primary_color=primary_color,
            job_id=job_id,
            mute_clips=mute_clips,
            target_duration=target_duration,
            music_volume=music_volume,
            ai_transition_style=ai_decisions.get("transition_style", "fade"),
            ai_zoom_moments=ai_decisions.get("zooms", []),
            ai_cuts=ai_decisions.get("cuts", []),
        )

        await update(82, "Rendering…")
        shotstack = ShotstackProvider()
        render_id = await shotstack.render(timeline)
        print(f"[MultiClip] render_id={render_id}", flush=True)

        # Poll Shotstack
        progress_steps = [84, 87, 90, 92, 94, 96, 97, 98]
        step_i = 0
        for _ in range(90):  # 15 min max
            await asyncio.sleep(10)
            render_status, render_url = await shotstack.get_render(render_id)
            print(f"[MultiClip] render status={render_status}", flush=True)

            if render_status == "done" and render_url:
                await _update_job(
                    db, job_id,
                    status="ready",
                    status_message="Done!",
                    progress=100,
                    output_url=render_url,
                    render_id=render_id,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                print(f"[MultiClip] stitch done job={job_id} url={render_url}", flush=True)
                return

            if render_status == "failed":
                raise RuntimeError("Shotstack render failed")

            p = progress_steps[min(step_i, len(progress_steps) - 1)]
            step_i += 1
            msg = {
                "queued": "Queued…",
                "fetching": "Fetching assets…",
                "rendering": "Rendering…",
                "saving": "Saving…",
            }.get(render_status, "Rendering…")
            await update(p, msg)

        raise RuntimeError("Render timed out after 15 minutes")

    except Exception as exc:
        print(f"[MultiClip] stitch FAILED job={job_id}: {exc}", flush=True)
        await _update_job(db, job_id, status="failed", status_message=str(exc), progress=0)
