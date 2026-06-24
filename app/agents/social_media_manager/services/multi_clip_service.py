"""
Multi-Clip Composition Service — Phase 1 (Founder Story)
Pipeline: Upload clips → probe + quality check → Whisper transcription per clip
          → GPT-4o narrative ordering → Shotstack multi-clip timeline → render
"""
from __future__ import annotations

import asyncio
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
        has_audio = False
        width = height = 0
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                width = s.get("width", 0)
                height = s.get("height", 0)
            if s.get("codec_type") == "audio":
                has_audio = True
        return {"duration": duration, "width": width, "height": height, "has_audio": has_audio}
    except Exception as e:
        print(f"[MultiClip] probe error: {e}", flush=True)
        return {"duration": 0, "width": 0, "height": 0, "has_audio": False}


# ── Quality flags ─────────────────────────────────────────────────────────────

async def _check_quality(path: str, probe: Dict) -> List[str]:
    """Return list of quality flag strings for a clip."""
    flags: List[str] = []

    if probe["duration"] < _MIN_CLIP_DURATION:
        flags.append("too_short")

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
        # YAVG line: YAVG:12.3
        m = re.search(r"YAVG:(\d+\.?\d*)", err_text)
        if m and float(m.group(1)) < 20:
            flags.append("too_dark")
    except Exception:
        pass

    # Audio energy: if has_audio, measure mean volume
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
            if m and float(m.group(1)) < -50:
                flags.append("too_quiet")
        except Exception:
            pass

    return flags


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


# ── Whisper transcription ─────────────────────────────────────────────────────

async def _transcribe_clip(path: str) -> Dict[str, Any]:
    """
    Transcribe a video clip using OpenAI Whisper.
    Returns {text, srt, words:[{word, start, end}]}
    """
    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        with open(path, "rb") as f:
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

        # Build SRT from segments
        srt_lines = []
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

        return {"text": text, "srt": "\n".join(srt_lines), "words": words}

    except Exception as e:
        print(f"[MultiClip] transcription error: {e}", flush=True)
        return {"text": "", "srt": "", "words": []}


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
            model="gpt-4o",
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

async def _upload_clip_to_cloudinary(video_bytes: bytes, clip_id: str) -> Optional[str]:
    """Upload a single clip to Cloudinary under the multi-clip folder."""
    cloud = settings.CLOUDINARY_CLOUD_NAME
    api_key = settings.CLOUDINARY_API_KEY
    api_secret = settings.CLOUDINARY_API_SECRET
    if not all([cloud, api_key, api_secret]):
        return None

    public_id = f"clip_{clip_id}"
    ts = int(time.time())
    params_str = f"folder={_CLOUDINARY_FOLDER}&public_id={public_id}&timestamp={ts}"
    signature = hashlib.sha1(f"{params_str}{api_secret}".encode()).hexdigest()

    form = aiohttp.FormData()
    form.add_field("file", video_bytes, filename=f"{public_id}.mp4", content_type="video/mp4")
    form.add_field("api_key", api_key)
    form.add_field("timestamp", str(ts))
    form.add_field("signature", signature)
    form.add_field("public_id", public_id)
    form.add_field("folder", _CLOUDINARY_FOLDER)
    form.add_field("resource_type", "video")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.cloudinary.com/v1_1/{cloud}/video/upload",
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

def _build_founder_timeline(
    clips_ordered: List[Dict],
    merged_srt: str,
    aspect_ratio: str = "9:16",
    music_url: str = "",
    primary_color: str = "#CD1B78",
    job_id: str = "",
) -> Dict:
    """
    Build a Shotstack timeline for a multi-clip Founder Story stitch.

    Each clip occupies its own segment. Crossfade transitions connect them.
    Captions come from the merged SRT across all clips.
    Optional music track spans the full duration.
    """
    tracks: List[Dict] = []
    total_duration = sum(c["duration_seconds"] for c in clips_ordered)

    # ── Track: video clips ────────────────────────────────────────────────────
    video_clips: List[Dict] = []
    cursor = 0.0
    for i, clip in enumerate(clips_ordered):
        dur = clip["duration_seconds"]
        clip_entry: Dict = {
            "asset": {
                "type": "video",
                "src": clip["cloudinary_url"],
                "volume": 1.0,
            },
            "start": round(cursor, 3),
            "length": round(dur, 3),
            "fit": "crop",
        }
        # Crossfade into next clip (not the last one)
        if i < len(clips_ordered) - 1:
            clip_entry["transition"] = {
                "out": "fade",
            }
        video_clips.append(clip_entry)
        cursor += dur

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
                "asset": {"type": "audio", "src": music_url, "volume": 0.12},
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


# ── DB update helper ──────────────────────────────────────────────────────────

async def _update_job(db, job_id: str, **fields) -> None:
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.multi_clip_jobs.update_one({"job_id": job_id}, {"$set": fields})


# ── Phase 1: Ingest background task ──────────────────────────────────────────

async def run_multi_clip_ingest(job_id: str, clips_bytes: List[Tuple[str, bytes]], db) -> None:
    """
    Background task: for each clip —
      1. Write to tmp file
      2. Probe (duration, dimensions, has_audio)
      3. Quality check
      4. Detect clip type (speech | silent | still)
      5. Upload to Cloudinary
      6. Transcribe with Whisper (if speech)
    Then suggest ordering via GPT-4o and save to DB.
    """
    print(f"[MultiClip] ingest start job={job_id} clips={len(clips_bytes)}", flush=True)

    async def update(progress: int, msg: str, **extra):
        await _update_job(db, job_id, progress=progress, status_message=msg, **extra)

    try:
        await update(5, "Uploading clips…")
        clip_docs: List[Dict] = []

        for i, (filename, raw_bytes) in enumerate(clips_bytes):
            clip_id = str(uuid.uuid4())[:8]
            pct_base = 5 + int((i / len(clips_bytes)) * 50)
            await update(pct_base, f"Analysing clip {i + 1} of {len(clips_bytes)}…")

            # Write to tmp file
            suffix = ".mp4"
            if filename.lower().endswith((".mov", ".avi", ".mkv", ".webm")):
                suffix = os.path.splitext(filename)[1]
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                tf.write(raw_bytes)
                tmp_path = tf.name

            try:
                probe = await _probe_clip(tmp_path)
                quality_flags = await _check_quality(tmp_path, probe)
                clip_type = await _detect_clip_type(tmp_path, probe)

                # Upload to Cloudinary
                cloud_url = await _upload_clip_to_cloudinary(raw_bytes, clip_id)
                if not cloud_url:
                    quality_flags.append("upload_failed")

                # Transcribe if speech
                transcript_data = {"text": "", "srt": "", "words": []}
                if clip_type == "speech" and cloud_url:
                    await update(pct_base + 3, f"Transcribing clip {i + 1}…")
                    transcript_data = await _transcribe_clip(tmp_path)

                clip_doc = {
                    "clip_id": clip_id,
                    "filename": filename,
                    "original_url": cloud_url or "",
                    "cloudinary_url": cloud_url or "",
                    "order_index": i,
                    "duration_seconds": round(probe["duration"], 2),
                    "width": probe["width"],
                    "height": probe["height"],
                    "clip_type": clip_type,
                    "has_face": False,  # Phase 3 will detect
                    "quality_flags": quality_flags,
                    "recommended_drop": "too_dark" in quality_flags,
                    "drop_reason": "Clip is too dark — it may drag down the overall video quality." if "too_dark" in quality_flags else None,
                    "transcript": transcript_data["text"],
                    "srt": transcript_data["srt"],
                    "words": transcript_data["words"],
                    "frame_url": None,       # Product Story (Phase 2)
                    "vision_description": None,
                    "vision_role": None,
                }
                clip_docs.append(clip_doc)
                print(f"[MultiClip] clip {clip_id}: type={clip_type} dur={probe['duration']:.1f}s flags={quality_flags}", flush=True)

            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        # Suggest ordering
        await update(60, "Suggesting clip order…")
        speech_clips = [c for c in clip_docs if c["clip_type"] == "speech"]
        if len(speech_clips) > 1:
            suggested_ids = await _suggest_founder_order(clip_docs)
        else:
            suggested_ids = [c["clip_id"] for c in clip_docs]

        # Assign order_index from suggestion
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

        await update(72, "Merging transcripts…")
        merged_srt = _merge_srts(active_clips)

        await update(75, "Selecting music…")
        music_url = ""
        if doc.get("enable_music", True):
            music_mood = doc.get("music_mood", "chill")
            music_url = _pick_music_url(music_mood)

        await update(78, "Building timeline…")
        aspect_ratio = doc.get("orientation", "9:16")
        primary_color = doc.get("primary_color", "#CD1B78")
        timeline = _build_founder_timeline(
            clips_ordered=active_clips,
            merged_srt=merged_srt,
            aspect_ratio=aspect_ratio,
            music_url=music_url,
            primary_color=primary_color,
            job_id=job_id,
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
