"""
Video Production Service — Phase 2
Pipeline: Upload → FFmpeg audio cleanup → Reap transcription → GPT-4o edit decisions (cuts/zooms/SFX) → Shotstack render (video + captions + SFX audio)
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


# ── Audio cleanup ─────────────────────────────────────────────────────────────

async def _clean_audio(video_bytes: bytes) -> bytes:
    """
    Apply FFmpeg audio cleanup before Reap upload:
    - highpass=f=80: remove low-frequency rumble
    - loudnorm: normalize to broadcast spec (-16 LUFS, -1.5 TP)
    Video stream is copied (no re-encode). Falls back to original bytes on failure.
    """
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(video_bytes)
        in_path = f.name
    out_path = in_path + "_clean.mp4"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", in_path,
            "-c:v", "copy",
            "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-y", out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            print(f"[AudioClean] ffmpeg failed: {stderr.decode()[-300:]}", flush=True)
            return video_bytes
        with open(out_path, "rb") as f:
            cleaned = f.read()
        print(f"[AudioClean] {len(video_bytes)//1024}KB → {len(cleaned)//1024}KB (cleaned)", flush=True)
        return cleaned
    except Exception as e:
        print(f"[AudioClean] error: {e}, using original", flush=True)
        return video_bytes
    finally:
        for p in (in_path, out_path):
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass


# ── SFX library ───────────────────────────────────────────────────────────────
# Set SFX_ENABLED=true in .env.staging after uploading SFX files to /app/static/sfx/
# on the container (then accessible via https://api-staging.urisocial.com/static/sfx/)

SFX_ENABLED = os.getenv("SFX_ENABLED", "false").lower() == "true"

_SFX_BASE = "https://api-staging.urisocial.com/static/sfx"
SFX_LIBRARY: Dict[str, str] = {
    "whoosh":   f"{_SFX_BASE}/whoosh.mp3",    # fast cuts / transitions
    "impact":   f"{_SFX_BASE}/impact.mp3",    # emphasis zooms / key claims
    "boom":     f"{_SFX_BASE}/impact.mp3",    # alias → impact
    "pop":      f"{_SFX_BASE}/pop.mp3",       # caption word / list item
    "tick":     f"{_SFX_BASE}/tick.mp3",      # subtle emphasis
    "ding":     f"{_SFX_BASE}/ding.mp3",      # positive / product feature reveal
    "sparkle":  f"{_SFX_BASE}/ding.mp3",      # alias → ding
    "swell":    f"{_SFX_BASE}/swell.mp3",     # section change / emotional peak
}


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
- sound_effects: contextual audio punctuation at key moments. "at" in seconds within [0, {duration:.1f}].
  Types: whoosh (fast cut/transition), impact (strong claim/reveal), pop (list item/name drop), ding (positive outcome/win), swell (emotional peak/section change).
  Max 5 SFX. Be selective — only add where audio clearly enhances impact. For founder type: max 2 SFX.

Return ONLY valid JSON (no markdown):
{{
  "cuts": [
    {{"remove_start": 4.2, "remove_end": 5.8, "reason": "long pause"}}
  ],
  "zooms": [
    {{"at": 12.5, "type": "emphasis", "intensity": "subtle", "reason": "key claim"}}
  ],
  "sound_effects": [
    {{"at": 8.2, "type": "impact", "reason": "product reveal"}}
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


def _srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _original_to_timeline(t: float, keep_segments: List[Dict[str, float]], clamp: bool = False) -> Optional[float]:
    """Map a timestamp in the original video to its position in the output timeline.
    If clamp=True and t falls in a cut region, returns the start of the next kept segment
    rather than None — used so caption end-times straddling a cut don't drop the whole caption."""
    offset = 0.0
    for seg in keep_segments:
        seg_dur = seg["src_end"] - seg["src_start"]
        if seg["src_start"] <= t <= seg["src_end"]:
            return offset + (t - seg["src_start"])
        if clamp and t < seg["src_start"]:
            return offset  # t is in a cut before this segment — clamp to segment start
        offset += seg_dur
    return None


def build_shotstack_timeline(
    video_url: str,
    video_duration: float,
    cuts: List[Dict],
    zooms: List[Dict],
    srt_entries: List[Dict[str, Any]],
    sound_effects: Optional[List[Dict]] = None,
    aspect_ratio: str = "9:16",
    job_id: str = "",
) -> Dict[str, Any]:
    keep_segments = _build_keep_segments(cuts, video_duration)
    total_duration = sum(s["src_end"] - s["src_start"] for s in keep_segments)

    # ── Video track ───────────────────────────────────────────────────────────
    video_clips: List[Dict] = []
    timeline_pos = 0.0

    for i, seg in enumerate(keep_segments):
        seg_dur = seg["src_end"] - seg["src_start"]
        if seg_dur < 0.1:
            continue

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
            # effect: "zoomIn" / "zoomOut" are Shotstack's built-in Ken Burns zoom —
            # clip.scale is not a valid Shotstack property and is silently ignored.
            clip["effect"] = "zoomIn" if z.get("intensity") != "strong" else "zoomOut"

        # Fade-in on every cut for a polished transition
        clip["transition"] = {"in": "fade"}

        video_clips.append(clip)
        timeline_pos += seg_dur

    # ── Caption track ─────────────────────────────────────────────────────────
    # Build a remapped SRT with timeline timestamps (after cuts applied) and
    # host it on our static server. Shotstack's rich-caption asset reads it
    # natively — gives us built-in animation, stroke, and font rendering without
    # the html asset's background/visibility quirks.
    caption_clips: List[Dict] = []
    srt_dir = "/app/static/srt"
    os.makedirs(srt_dir, exist_ok=True)

    srt_lines: List[str] = []
    entry_num = 1
    for entry in srt_entries:
        tl_start = _original_to_timeline(entry["start"], keep_segments)
        tl_end = _original_to_timeline(entry["end"], keep_segments, clamp=True)
        if tl_start is None or tl_end is None:
            continue
        tl_start = max(0.0, tl_start)
        tl_end = min(total_duration, tl_end)
        if tl_end - tl_start < 0.1:
            continue
        srt_lines += [
            str(entry_num),
            f"{_srt_time(tl_start)} --> {_srt_time(tl_end)}",
            entry["text"],
            "",
        ]
        entry_num += 1

    srt_filename = f"{job_id or 'job'}.srt"
    with open(f"{srt_dir}/{srt_filename}", "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))

    srt_url = f"https://api-staging.urisocial.com/static/srt/{srt_filename}"
    print(f"[VideoProduction] caption SRT written: {srt_dir}/{srt_filename} ({entry_num - 1} entries) → {srt_url}", flush=True)

    caption_clips = [{
        "asset": {
            "type": "rich-caption",
            "src": srt_url,
            "font": {
                "family": "Montserrat",
                "size": 52,
                "color": "#ffffff",
                "weight": 700,
            },
            "stroke": {
                "width": 3,
                "color": "#000000",
                "opacity": 1,
            },
            "animation": {"style": "pop"},
            "style": {"textTransform": "uppercase"},
        },
        "start": 0,
        "length": "end",
        "width": 900,
        "height": 300,
        "position": "bottom",
        "offset": {"x": 0, "y": 0.05},
    }]

    # ── SFX audio track ───────────────────────────────────────────────────────
    sfx_clips: List[Dict] = []
    if SFX_ENABLED and sound_effects:
        for sfx in sound_effects:
            sfx_type = sfx.get("type", "").lower()
            sfx_url = SFX_LIBRARY.get(sfx_type, "")
            if not sfx_url:
                continue
            orig_at = float(sfx.get("at", -1))
            if orig_at < 0 or orig_at > video_duration:
                continue
            tl_at = _original_to_timeline(orig_at, keep_segments)
            if tl_at is None:
                continue
            sfx_clips.append({
                "asset": {
                    "type": "audio",
                    "src": sfx_url,
                    "volume": 0.65,
                    "trim": 0,
                },
                "start": round(tl_at, 3),
                "length": 1.5,
            })

    tracks = [{"clips": caption_clips}, {"clips": video_clips}]
    if sfx_clips:
        tracks.append({"clips": sfx_clips})

    return {
        "timeline": {"tracks": tracks},
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
        # ── Stage 1: probe duration + audio cleanup + upload to Reap ─────────
        await update(5, "Uploading video…")
        duration = _probe_duration(video_bytes)
        print(f"[VideoProduction] duration={duration:.1f}s", flush=True)

        cleaned_bytes = await _clean_audio(video_bytes)
        upload_id = await reap.upload_video(cleaned_bytes, f"{job_id}.mp4")
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
        sound_effects = decisions.get("sound_effects", [])
        pacing_note = decisions.get("pacing_note", "")
        print(f"[VideoProduction] cuts={len(cuts)} zooms={len(zooms)} sfx={len(sound_effects)} pacing={pacing_note}", flush=True)

        # ── Stage 4: Build + submit Shotstack render ──────────────────────────
        await update(58, "Building edit timeline…")
        srt_entries = _parse_srt(srt_text)
        timeline = build_shotstack_timeline(
            video_url=video_url,
            video_duration=duration,
            cuts=cuts,
            zooms=zooms,
            srt_entries=srt_entries,
            sound_effects=sound_effects,
            aspect_ratio="9:16",
            job_id=job_id,
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
                             sound_effects=sound_effects,
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
