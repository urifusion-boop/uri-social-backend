"""
Video Production Service — Phase 1
Pipeline: Upload → Reap transcription → GPT-4o edit decisions → Shotstack render
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import openai

from app.core.config import settings
from app.agents.social_media_manager.services.video_polish_service import (
    ReapProvider,
    _parse_srt,
)

# ── Shotstack ─────────────────────────────────────────────────────────────────

SHOTSTACK_EDIT_BASE = "https://api.shotstack.io/edit/stage"
SHOTSTACK_CREATE_BASE = "https://api.shotstack.io/create/stage"


SHOTSTACK_ASSET_LIMIT = 9 * 1024 * 1024  # 9MB — stay under the 10MB hard limit


async def _compress_for_shotstack(video_bytes: bytes, duration: float) -> bytes:
    """Compress video to fit under Shotstack's 10MB asset limit using FFmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as inf:
        inf.write(video_bytes)
        in_path = inf.name
    out_path = in_path + "_c.mp4"
    try:
        target_kbps = int((SHOTSTACK_ASSET_LIMIT * 8) / max(duration, 1) / 1024)
        audio_kbps = 64
        video_kbps = max(150, target_kbps - audio_kbps)
        print(f"[Shotstack] compressing {len(video_bytes)//1024}KB → target {target_kbps}kbps (v={video_kbps} a={audio_kbps})", flush=True)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", in_path,
            "-c:v", "libx264", "-b:v", f"{video_kbps}k",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-preset", "veryfast", "-movflags", "+faststart", "-y", out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        with open(out_path, "rb") as f:
            compressed = f.read()
        print(f"[Shotstack] compressed to {len(compressed)//1024}KB", flush=True)
        return compressed
    finally:
        os.unlink(in_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


class ShotstackProvider:
    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": settings.SHOTSTACK_API_KEY or "",
            "Content-Type": "application/json",
        }

    async def upload_asset(self, video_bytes: bytes, filename: str, duration: float = 120.0) -> str:
        """Upload video bytes to Shotstack assets, return hosted URL.
        Compresses automatically if over the 10MB limit."""
        if len(video_bytes) > SHOTSTACK_ASSET_LIMIT:
            video_bytes = await _compress_for_shotstack(video_bytes, duration)

        form = aiohttp.FormData()
        form.add_field("file", video_bytes, filename=filename, content_type="video/mp4")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{SHOTSTACK_CREATE_BASE}/assets",
                headers={"x-api-key": settings.SHOTSTACK_API_KEY or ""},
                data=form,
            ) as resp:
                if not resp.ok:
                    body = await resp.text()
                    raise RuntimeError(f"Shotstack upload {resp.status}: {body}")
                data = await resp.json()
                return data["data"]["attributes"]["url"]

    async def render(self, timeline: Dict[str, Any]) -> str:
        """Submit render job, return render ID."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{SHOTSTACK_EDIT_BASE}/render",
                headers=self._headers(),
                json=timeline,
            ) as resp:
                if not resp.ok:
                    body = await resp.text()
                    raise RuntimeError(f"Shotstack render {resp.status}: {body}")
                data = await resp.json()
                return data["response"]["id"]

    async def get_render(self, render_id: str) -> Tuple[str, str]:
        """Returns (status, url). Status: queued|fetching|rendering|saving|done|failed."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{SHOTSTACK_EDIT_BASE}/render/{render_id}",
                headers=self._headers(),
            ) as resp:
                if not resp.ok:
                    return "failed", ""
                data = await resp.json()
                r = data["response"]
                return r.get("status", "failed"), r.get("url", "")


# ── GPT-4o Edit Decisions ─────────────────────────────────────────────────────

async def get_edit_decisions(srt_text: str, video_type: str, duration: float) -> Dict[str, Any]:
    """Feed transcript + video type to GPT-4o; get structured edit decisions."""
    rules = {
        "tiktok": "Fast pacing, aggressive cuts on silences >0.5s, frequent emphasis zooms, hook visible in first 2s.",
        "product": "Snappy pacing, cuts on silences >1.0s, zoom on product mentions, moderate overall.",
        "founder": "Gentle pacing, cut only silences >1.5s, minimal zooms, keep natural speech rhythm.",
    }.get(video_type, "Moderate pacing, cut silences >1.0s, subtle zooms on key phrases.")

    prompt = f"""You are an expert short-form video editor. Given a transcript and a video type, produce a contextual edit decision list.

VIDEO TYPE: {video_type}
VIDEO DURATION: {duration:.1f}s
EDITING RULES: {rules}

TRANSCRIPT (SRT):
{srt_text}

INSTRUCTIONS:
- cuts: remove ranges of clear silence/dead-space/filler. Each remove_start and remove_end must be within [0, {duration:.1f}].
- zooms: emphasis punch-ins on key words/claims. "at" must be within [0, {duration:.1f}]. intensity: "subtle" or "strong".
- Be conservative — cutting real speech is worse than leaving silence.
- For founder type: max 3 cuts, max 2 zooms.

Return ONLY valid JSON (no markdown):
{{
  "cuts": [
    {{"remove_start": 4.2, "remove_end": 5.8, "reason": "long pause"}}
  ],
  "zooms": [
    {{"at": 12.5, "type": "emphasis", "intensity": "subtle", "reason": "key claim"}}
  ],
  "pacing_note": "tight and energetic"
}}"""

    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    text = response.choices[0].message.content or "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"cuts": [], "zooms": []}


# ── Timeline Builder ──────────────────────────────────────────────────────────

def _build_keep_segments(cuts: List[Dict], duration: float) -> List[Dict[str, float]]:
    """Invert cut ranges into keep segments."""
    sorted_cuts = sorted(
        [c for c in cuts if c.get("remove_start") is not None and c.get("remove_end") is not None],
        key=lambda x: float(x["remove_start"]),
    )
    keep: List[Dict[str, float]] = []
    current = 0.0
    for cut in sorted_cuts:
        rs = max(0.0, float(cut["remove_start"]))
        re_ = min(duration, float(cut["remove_end"]))
        if rs <= current:
            current = max(current, re_)
            continue
        if rs - current > 0.15:
            keep.append({"src_start": current, "src_end": rs})
        current = re_
    if duration - current > 0.15:
        keep.append({"src_start": current, "src_end": duration})
    if not keep:
        keep = [{"src_start": 0.0, "src_end": duration}]
    return keep


def _original_to_timeline(t: float, keep_segments: List[Dict[str, float]]) -> Optional[float]:
    """Map a timestamp in the original video to its position in the output timeline."""
    offset = 0.0
    for seg in keep_segments:
        seg_dur = seg["src_end"] - seg["src_start"]
        if seg["src_start"] <= t <= seg["src_end"]:
            return offset + (t - seg["src_start"])
        offset += seg_dur
    return None


def build_shotstack_timeline(
    video_url: str,
    video_duration: float,
    cuts: List[Dict],
    zooms: List[Dict],
    srt_entries: List[Dict[str, Any]],
    aspect_ratio: str = "9:16",
) -> Dict[str, Any]:
    keep_segments = _build_keep_segments(cuts, video_duration)
    total_duration = sum(s["src_end"] - s["src_start"] for s in keep_segments)

    # ── Video track ───────────────────────────────────────────────────────────
    video_clips: List[Dict] = []
    timeline_pos = 0.0

    for seg in keep_segments:
        seg_dur = seg["src_end"] - seg["src_start"]
        if seg_dur < 0.1:
            continue

        # Does any zoom land in this segment?
        seg_zooms = [z for z in zooms if seg["src_start"] <= float(z.get("at", -1)) < seg["src_end"]]

        clip: Dict[str, Any] = {
            "asset": {
                "type": "video",
                "src": video_url,
                "trim": seg["src_start"],
                "volume": 1,
            },
            "start": round(timeline_pos, 3),
            "length": round(seg_dur, 3),
            "fit": "cover",
        }

        if seg_zooms:
            z = seg_zooms[0]
            clip["scale"] = 1.08 if z.get("intensity") == "strong" else 1.04

        video_clips.append(clip)
        timeline_pos += seg_dur

    # ── Caption track ─────────────────────────────────────────────────────────
    caption_clips: List[Dict] = []
    for entry in srt_entries:
        tl_start = _original_to_timeline(entry["start"], keep_segments)
        tl_end = _original_to_timeline(entry["end"], keep_segments)

        if tl_start is None or tl_end is None:
            continue
        tl_start = max(0.0, tl_start)
        tl_end = min(total_duration, tl_end)
        cap_dur = tl_end - tl_start
        if cap_dur < 0.1:
            continue

        safe_text = (
            entry["text"]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

        caption_clips.append({
            "asset": {
                "type": "html",
                "html": f"<p>{safe_text}</p>",
                "css": (
                    "p {"
                    "  font-family: 'Montserrat', sans-serif;"
                    "  font-size: 54px;"
                    "  font-weight: 900;"
                    "  color: #ffffff;"
                    "  text-align: center;"
                    "  -webkit-text-stroke: 3px #000000;"
                    "  text-stroke: 3px #000000;"
                    "  margin: 0;"
                    "  padding: 0 24px;"
                    "  line-height: 1.25;"
                    "}"
                ),
                "width": 600,
                "height": 220,
            },
            "start": round(tl_start, 3),
            "length": round(cap_dur, 3),
            "position": "bottom",
            "offset": {"x": 0, "y": -0.12},
            "transition": {"in": "fade", "out": "fade"},
        })

    return {
        "timeline": {
            "tracks": [
                {"clips": caption_clips},
                {"clips": video_clips},
            ]
        },
        "output": {
            "format": "mp4",
            "resolution": "hd",
            "aspectRatio": aspect_ratio,
            "fps": 30,
        },
    }


# ── Video duration util ───────────────────────────────────────────────────────

def _probe_duration(video_bytes: bytes) -> float:
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(video_bytes)
            tmp = f.name
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", tmp],
            capture_output=True, text=True, timeout=30,
        )
        os.unlink(tmp)
        info = json.loads(result.stdout)
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "video":
                return float(stream.get("duration", 120.0))
    except Exception:
        pass
    return 120.0


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run_production_job(
    job_id: str,
    video_bytes: bytes,
    video_type: str,
    db,
) -> None:
    reap = ReapProvider()
    shotstack = ShotstackProvider()

    async def update(progress: int, message: str, status: str = "processing", **extra):
        doc: Dict[str, Any] = {
            "progress": progress,
            "status_message": message,
            "status": status,
        }
        doc.update(extra)
        await db.video_production_jobs.update_one({"job_id": job_id}, {"$set": doc})
        print(f"[VideoProduction] job={job_id} {progress}% {message}", flush=True)

    try:
        # ── Stage 1: probe duration + upload to Reap ─────────────────────────
        await update(5, "Uploading video…")
        duration = _probe_duration(video_bytes)
        print(f"[VideoProduction] duration={duration:.1f}s", flush=True)

        upload_id = await reap.upload_video(video_bytes, f"{job_id}.mp4")
        if not upload_id:
            raise RuntimeError("Upload to Reap failed")

        # ── Stage 2: Reap transcription ───────────────────────────────────────
        await update(15, "Getting transcript…")
        trans_id = await reap.start_transcription(upload_id, "en")
        if not trans_id:
            raise RuntimeError("Transcription failed to start")

        srt_text, video_url = await reap.fetch_srt_and_source_url(trans_id, timeout_seconds=300)

        if not srt_text:
            raise RuntimeError("Transcription timed out or returned empty")

        # If Reap didn't expose the source URL, compress + upload to Shotstack as fallback
        if not video_url:
            await update(40, "Uploading to render engine…")
            video_url = await shotstack.upload_asset(video_bytes, f"{job_id}.mp4", duration)
        if not video_url:
            raise RuntimeError("Could not obtain a source video URL for rendering")

        print(f"[VideoProduction] video_url={video_url[:80]}… srt_len={len(srt_text)}", flush=True)

        # ── Stage 3: GPT-4o edit decisions ───────────────────────────────────
        await update(48, "Analysing transcript with AI…")
        decisions = await get_edit_decisions(srt_text, video_type, duration)
        cuts = decisions.get("cuts", [])
        zooms = decisions.get("zooms", [])
        pacing_note = decisions.get("pacing_note", "")
        print(f"[VideoProduction] cuts={len(cuts)} zooms={len(zooms)} pacing={pacing_note}", flush=True)

        # ── Stage 4: Build + submit Shotstack render ──────────────────────────
        await update(58, "Building edit timeline…")
        srt_entries = _parse_srt(srt_text)
        timeline = build_shotstack_timeline(
            video_url=video_url,
            video_duration=duration,
            cuts=cuts,
            zooms=zooms,
            srt_entries=srt_entries,
            aspect_ratio="9:16",
        )

        await update(65, "Rendering video…")
        render_id = await shotstack.render(timeline)
        print(f"[VideoProduction] render_id={render_id}", flush=True)

        # ── Stage 5: Poll Shotstack until done ────────────────────────────────
        progress_steps = [70, 75, 80, 85, 88, 90, 92, 94, 96, 97, 98]
        step_i = 0
        for _ in range(90):  # 15 min max
            await asyncio.sleep(10)
            render_status, render_url = await shotstack.get_render(render_id)
            print(f"[VideoProduction] render status={render_status}", flush=True)

            if render_status == "done" and render_url:
                await update(100, "Done!", status="ready",
                             output_url=render_url,
                             render_id=render_id,
                             cuts=cuts, zooms=zooms,
                             pacing_note=pacing_note,
                             srt=srt_text,
                             completed_at=datetime.now(timezone.utc).isoformat())
                return
            if render_status == "failed":
                raise RuntimeError("Shotstack render failed")

            p = progress_steps[min(step_i, len(progress_steps) - 1)]
            step_i += 1
            msg = {"queued": "Queued…", "fetching": "Fetching assets…",
                   "rendering": "Rendering…", "saving": "Saving…"}.get(render_status, "Rendering…")
            await update(p, msg)

        raise RuntimeError("Render timed out after 15 minutes")

    except Exception as exc:
        print(f"[VideoProduction] FAILED job={job_id}: {exc}", flush=True)
        await db.video_production_jobs.update_one(
            {"job_id": job_id},
            {"$set": {"status": "failed", "status_message": str(exc), "progress": 0}},
        )
