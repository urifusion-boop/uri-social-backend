"""
Video Production Service — Phase 2
Pipeline: Upload → FFmpeg audio cleanup → Reap transcription → GPT-4o edit decisions (cuts/zooms/SFX) → Shotstack render (video + captions + SFX audio)
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
    Stage 2 audio enhancement per PRD:
    - highpass=f=80: cut low-frequency rumble (mic handling, room boom)
    - afftdn: FFT-based noise reduction + de-essing (nf=-25 floor, nr=33 reduction)
    - loudnorm: normalize to broadcast standard (-16 LUFS, -1.5 TP)
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
            "-af", "highpass=f=80,afftdn=nf=-25:nr=33:nt=w,loudnorm=I=-16:TP=-1.5:LRA=11",
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
# SFX files hosted on Cloudinary — permanent CDN URLs, no container dependency
_SFX_CDN = "https://res.cloudinary.com/df8ckaeam/video/upload/uri-sfx"
SFX_LIBRARY: Dict[str, str] = {
    "whoosh":   f"{_SFX_CDN}/whoosh.mp3",    # fast cuts / transitions
    "impact":   f"{_SFX_CDN}/impact.mp3",    # emphasis zooms / key claims
    "boom":     f"{_SFX_CDN}/impact.mp3",    # alias → impact
    "pop":      f"{_SFX_CDN}/pop.mp3",       # caption word / list item
    "tick":     f"{_SFX_CDN}/tick.mp3",      # subtle emphasis
    "ding":     f"{_SFX_CDN}/ding.mp3",      # positive / product feature reveal
    "sparkle":  f"{_SFX_CDN}/ding.mp3",      # alias → ding
    "swell":    f"{_SFX_CDN}/swell.mp3",     # section change / emotional peak
}


# ── Cloudinary upload ─────────────────────────────────────────────────────────

async def _upload_to_cloudinary(video_bytes: bytes, public_id: str) -> Optional[str]:
    """
    Upload video bytes to Cloudinary and return the secure CDN URL.
    Uses the signed upload API — no SDK required.
    Returns None if credentials are missing or upload fails.
    """
    cloud = settings.CLOUDINARY_CLOUD_NAME
    api_key = settings.CLOUDINARY_API_KEY
    api_secret = settings.CLOUDINARY_API_SECRET
    if not all([cloud, api_key, api_secret]):
        return None

    folder = "uri-video-production"
    ts = int(time.time())
    # Signature = SHA-1 of sorted param string + api_secret (Cloudinary spec)
    params_str = f"folder={folder}&public_id={public_id}&timestamp={ts}"
    signature = hashlib.sha1(f"{params_str}{api_secret}".encode()).hexdigest()

    form = aiohttp.FormData()
    form.add_field("file", video_bytes, filename=f"{public_id}.mp4", content_type="video/mp4")
    form.add_field("api_key", api_key)
    form.add_field("timestamp", str(ts))
    form.add_field("signature", signature)
    form.add_field("public_id", public_id)
    form.add_field("folder", folder)
    form.add_field("resource_type", "video")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.cloudinary.com/v1_1/{cloud}/video/upload",
                data=form,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                body = await resp.json(content_type=None)
                if not resp.ok:
                    print(f"[Cloudinary] upload failed {resp.status}: {body}", flush=True)
                    return None
                url = body.get("secure_url", "")
                print(f"[Cloudinary] uploaded {len(video_bytes)//1024}KB → {url}", flush=True)
                return url
    except Exception as e:
        print(f"[Cloudinary] error: {e}", flush=True)
        return None


# ── B-roll asset fetch ────────────────────────────────────────────────────────

async def _pexels_search(query: str) -> Optional[str]:
    """Search Pexels for a short video clip. Returns a direct MP4 URL or None."""
    key = settings.PEXELS_API_KEY
    if not key:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": key},
                params={"query": query, "per_page": 8, "orientation": "portrait", "size": "medium"},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if not resp.ok:
                    print(f"[B-roll] Pexels {resp.status} for '{query}'", flush=True)
                    return None
                data = await resp.json()
                videos = data.get("videos", [])
                if not videos:
                    # Retry without orientation filter — portrait stock is sparse
                    async with session.get(
                        "https://api.pexels.com/videos/search",
                        headers={"Authorization": key},
                        params={"query": query, "per_page": 8, "size": "medium"},
                        timeout=aiohttp.ClientTimeout(total=12),
                    ) as resp2:
                        if resp2.ok:
                            data = await resp2.json()
                            videos = data.get("videos", [])
                for video in videos:
                    files = sorted(
                        video.get("video_files", []),
                        key=lambda f: f.get("height", 0), reverse=True,
                    )
                    for f in files:
                        link = f.get("link", "")
                        if link and f.get("height", 0) >= 720:
                            return link
    except Exception as e:
        print(f"[B-roll] Pexels error for '{query}': {e}", flush=True)
    return None


async def _fal_generate(description: str) -> Optional[str]:
    """Generate a short video clip with fal.ai Wan T2V. Returns URL or None."""
    key = settings.FAL_API_KEY
    if not key:
        return None
    model = "fal-ai/wan/t2v-1.3b"
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            # Submit job
            async with session.post(
                f"https://queue.fal.run/{model}",
                headers=headers,
                json={"prompt": description, "duration": "3"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if not resp.ok:
                    print(f"[B-roll] fal.ai submit {resp.status}", flush=True)
                    return None
                job = await resp.json()
                req_id = job.get("request_id")
                if not req_id:
                    return None

            # Poll for up to 90s
            for _ in range(18):
                await asyncio.sleep(5)
                async with session.get(
                    f"https://queue.fal.run/{model}/requests/{req_id}/status",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as s:
                    status_data = await s.json()
                    if status_data.get("status") == "COMPLETED":
                        break
                    if status_data.get("status") == "FAILED":
                        return None

            # Fetch result
            async with session.get(
                f"https://queue.fal.run/{model}/requests/{req_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as res:
                result = await res.json()
                return (result.get("video") or {}).get("url")
    except Exception as e:
        print(f"[B-roll] fal.ai error: {e}", flush=True)
    return None


async def _fetch_broll_url(description: str, concept: str) -> Optional[str]:
    """Priority: Pexels stock → fal.ai generate. Returns a public video URL or None."""
    url = await _pexels_search(concept or description)
    if url:
        print(f"[B-roll] Pexels hit: '{concept}' → {url[:70]}…", flush=True)
        return url
    print(f"[B-roll] Pexels miss for '{concept}', trying fal.ai…", flush=True)
    url = await _fal_generate(description)
    if url:
        print(f"[B-roll] fal.ai generated for '{description[:40]}' → {url[:70]}…", flush=True)
    return url


# ── Background music ──────────────────────────────────────────────────────────

_MOOD_TAGS: Dict[str, str] = {
    "upbeat":     "positive",
    "chill":      "relaxing",
    "cinematic":  "cinematic",
    "dramatic":   "dramatic",
    "acoustic":   "acoustic",
    "electronic": "electronic",
}


async def _fetch_music_url(mood: str) -> Optional[str]:
    """Fetch a royalty-free instrumental track from Jamendo by mood. Returns MP3 URL or None."""
    client_id = settings.JAMENDO_CLIENT_ID
    if not client_id:
        return None
    tags = _MOOD_TAGS.get(mood, "positive")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.jamendo.com/v3.0/tracks/",
                params={
                    "client_id": client_id,
                    "format": "json",
                    "audiodlformat": "mp32",
                    "tags": tags,
                    "vocalinstrumental": "instrumental",
                    "limit": 5,
                    "order": "popularity_total",
                    "boost": "popularity_total",
                },
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if not resp.ok:
                    print(f"[Music] Jamendo {resp.status}", flush=True)
                    return None
                data = await resp.json()
                tracks = data.get("results", [])
                if tracks:
                    url = tracks[0].get("audio")
                    print(f"[Music] '{tracks[0].get('name')}' ({mood}) → {(url or '')[:70]}", flush=True)
                    return url
    except Exception as e:
        print(f"[Music] Jamendo error: {e}", flush=True)
    return None


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

def _summarise_tracking_data(tracking_data: dict, duration: float) -> str:
    """
    Compress Reap's trackingData into a compact string for GPT-4o.
    Extracts detected silences and word-gap pauses. Returns "" if nothing useful.
    """
    lines: List[str] = []

    # Format A: {"words": [{"word": "hi", "start": 0.1, "end": 0.4}, ...]}
    words = (
        tracking_data.get("words")
        or tracking_data.get("segments")
        or tracking_data.get("transcript", {}).get("words")
        or []
    )
    if words and isinstance(words, list):
        silences = []
        for i in range(1, len(words)):
            prev_end = float(words[i - 1].get("end") or words[i - 1].get("endTime") or 0)
            curr_start = float(words[i].get("start") or words[i].get("startTime") or 0)
            gap = curr_start - prev_end
            if gap >= 0.3:
                silences.append(f"{prev_end:.2f}–{curr_start:.2f}s ({gap:.2f}s)")
        if silences:
            lines.append(f"Word-gap silences (≥0.3s): {', '.join(silences[:25])}")

    # Format B: {"silences": [{"start": 1.0, "end": 2.5}, ...]}
    raw_silences = (
        tracking_data.get("silences")
        or tracking_data.get("pauses")
        or tracking_data.get("silence_segments")
        or []
    )
    if raw_silences and isinstance(raw_silences, list):
        sil_strs = [
            f"{float(s.get('start') or s.get('startTime') or 0):.2f}–"
            f"{float(s.get('end') or s.get('endTime') or 0):.2f}s"
            for s in raw_silences[:20]
        ]
        lines.append(f"Detected silences: {', '.join(sil_strs)}")

    return ("\n" + "\n".join(lines)) if lines else ""


async def get_edit_decisions(
    srt_text: str,
    video_type: str,
    duration: float,
    tracking_data: Optional[dict] = None,
) -> Dict[str, Any]:
    """Feed transcript + video type to GPT-4o; get structured edit decisions."""
    rules = {
        "tiktok": "Fast pacing, aggressive cuts on silences >0.5s, frequent emphasis zooms, hook in first 2s.",
        "product": "Snappy pacing, cuts on silences >1.0s, zoom on product mentions, moderate overall.",
        "founder": "Gentle pacing, cut only silences >1.5s, minimal zooms, keep natural speech rhythm.",
    }.get(video_type, "Moderate pacing, cut silences >1.0s, subtle zooms on key phrases.")

    tracking_context = _summarise_tracking_data(tracking_data or {}, duration)

    prompt = f"""You are an expert short-form video editor. Given a transcript and a video type, produce a contextual edit decision list.

VIDEO TYPE: {video_type}
VIDEO DURATION: {duration:.1f}s
EDITING RULES: {rules}

TRANSCRIPT (SRT):
{srt_text}
{f"REAP CLIP DETECTION (word-level timing + silences):{tracking_context}" if tracking_context else ""}
INSTRUCTIONS:
- cuts: remove ranges of clear silence/dead-space/filler. Each remove_start and remove_end must be within [0, {duration:.1f}].
- zooms: emphasis punch-ins on key words/claims. "at" must be within [0, {duration:.1f}]. intensity: "subtle" or "strong".
- Be conservative — cutting real speech is worse than leaving silence.
- For founder type: max 3 cuts, max 2 zooms.
- sound_effects: contextual audio punctuation at key moments. "at" in seconds within [0, {duration:.1f}].
  Types: whoosh (fast cut/transition), impact (strong claim/reveal), pop (list item/name drop), ding (positive outcome/win), swell (emotional peak/section change).
  Max 5 SFX. Be selective — only add where audio clearly enhances impact. For founder type: max 2 SFX.
- broll: visual cutaway over the speaker at moments where showing something reinforces the message.
  "at": when cutaway starts (original video seconds, within [0, {duration:.1f}]).
  "duration": how long to show it (2.0–4.0 seconds).
  "description": specific visual to show (e.g. "hands typing on a laptop keyboard", "close-up of a smartphone screen showing social media feed").
  "concept": 1–3 word Pexels search query (e.g. "typing laptop", "smartphone social media", "money cash").
  Max 3 b-roll clips. Only add where a visual genuinely helps — skip if the speaker's face/expression is the key content at that moment.
  Do NOT add b-roll that overlaps a zoom. Space b-roll clips at least 3s apart.
- music_mood: background music mood. Options: upbeat, chill, cinematic, dramatic, acoustic, electronic. Pick what best fits the video energy and topic.
- hook_text: A punchy 3–6 word ALL-CAPS hook/title that captures the video's core message or biggest claim. Used as an animated title card in the first 2s. E.g. "HOW I MADE $10K", "STOP DOING THIS WRONG", "THE TRUTH ABOUT AI".

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
  "broll": [
    {{"at": 6.0, "duration": 3.0, "description": "hands typing on laptop keyboard", "concept": "typing laptop", "reason": "speaker mentions working"}}
  ],
  "pacing_note": "tight and energetic",
  "music_mood": "upbeat",
  "hook_text": "THE REAL SECRET HERE"
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
    broll: Optional[List[Dict]] = None,
    aspect_ratio: str = "9:16",
    job_id: str = "",
    music_url: Optional[str] = None,
    hook_text: str = "",
) -> Dict[str, Any]:
    # broll items have: at (original ts), duration, url (resolved)
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

        # Base clips pan left/right (slide Ken Burns) — zero zoom so emphasis zooms
        # are visually distinct and unmissable by contrast.
        base_effect = "slideLeft" if i % 2 == 0 else "slideRight"

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
            "effect": base_effect,
        }

        # Zoom transition at every cut except the very first clip
        if i > 0:
            clip["transition"] = {"in": "zoom"}

        if seg_zooms:
            z = seg_zooms[0]
            # Swap from pan → zoom effect so the audience feels the camera push in.
            # Strong rotation overshoot (6° → -3° → 0°) adds a kinetic smash-cut feel.
            # Skew snap (0.07 → 0 in 0.3s) adds perspective warp on zoom entry.
            clip["effect"] = "zoomIn" if z.get("intensity") != "strong" else "zoomOut"
            clip["transform"] = {
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
                "size": 46,
                "color": "#ffffff",
                "weight": 800,
            },
            "stroke": {
                "width": 2,
                "color": "#000000",
                "opacity": 1,
            },
            "animation": {"style": "karaoke"},
            # Active word: black text on yellow background box (Instagram Reels/TikTok style)
            "active": {
                "font": {"color": "#000000", "background": "#FFD700"},
                "stroke": {"width": 0, "color": "#000000", "opacity": 0},
            },
            "style": {"textTransform": "uppercase"},
            "padding": {"top": 6, "right": 20, "bottom": 6, "left": 20},
        },
        "start": 0,
        "length": "end",
        "width": 580,      # ~54% of 1080px — tight block, not edge-to-edge
        "height": 220,
        "position": "bottom",
        "offset": {"x": 0, "y": 0.07},
    }]

    # ── SFX audio track ───────────────────────────────────────────────────────
    sfx_clips: List[Dict] = []
    if sound_effects:
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

    # ── B-roll track ──────────────────────────────────────────────────────────
    broll_clips: List[Dict] = []
    for br in (broll or []):
        br_url = br.get("url")
        if not br_url:
            continue
        orig_at = float(br.get("at", -1))
        br_dur = min(float(br.get("duration", 3.0)), 4.0)
        if orig_at < 0 or orig_at >= video_duration:
            continue
        tl_at = _original_to_timeline(orig_at, keep_segments)
        if tl_at is None:
            continue
        # Clamp duration so b-roll doesn't run past the timeline end
        br_dur = min(br_dur, total_duration - tl_at)
        if br_dur < 0.5:
            continue
        fade = min(0.25, br_dur / 4)
        broll_clips.append({
            "asset": {
                "type": "video",
                "src": br_url,
                "trim": 0,
                "volume": 0,  # keep original voice; mute b-roll audio
            },
            "start": round(tl_at, 3),
            "length": round(br_dur, 3),
            "fit": "cover",
            "effect": "slideUp",
            "opacity": [
                {"from": 0, "to": 1, "start": 0, "length": fade,
                 "interpolation": "bezier", "easing": "easeOutCubic"},
                {"from": 1, "to": 0, "start": round(br_dur - fade, 3), "length": fade,
                 "interpolation": "bezier", "easing": "easeInCubic"},
            ],
        })

    # ── Background music track ────────────────────────────────────────────────
    music_clips: List[Dict] = []
    if music_url:
        fade = min(2.0, total_duration * 0.08)
        music_clips = [{
            "asset": {
                "type": "audio",
                "src": music_url,
                "volume": 0.15,   # sits beneath speech; voice stays dominant
                "trim": 0,
            },
            "start": 0,
            "length": round(total_duration, 3),
        }]
        print(f"[Music] added track volume=0.15 length={total_duration:.1f}s fade={fade:.1f}s", flush=True)

    # ── Hook title card (rich-text overlay, first 2.5s) ──────────────────────
    hook_clips: List[Dict] = []
    if hook_text:
        hook_css = (
            "body{margin:0;padding:0;background:transparent;}"
            "p{font-family:'Montserrat',sans-serif;font-size:74px;font-weight:900;"
            "color:#FFFFFF;text-align:center;text-transform:uppercase;"
            "letter-spacing:-2px;"
            "text-shadow:3px 3px 0 #000,-3px -3px 0 #000,3px -3px 0 #000,-3px 3px 0 #000;"
            "margin:0;padding:12px 24px;}"
        )
        hook_clips = [{
            "asset": {
                "type": "html",
                "html": f"<p>{hook_text.upper()}</p>",
                "css": hook_css,
                "width": 960,
                "height": 320,
            },
            "start": 0,
            "length": round(min(2.5, total_duration * 0.25), 3),
            "position": "center",
            "offset": {"y": 0.12},
            "transition": {"in": "slideUp", "out": "fade"},
        }]
        print(f"[VideoProduction] hook title card: '{hook_text}'", flush=True)

    # Track order (index 0 = top layer):
    # 0: hook title, 1: captions, 2: b-roll overlays, 3: main video, 4: sfx audio, 5: bg music
    tracks: List[Dict] = []
    if hook_clips:
        tracks.append({"clips": hook_clips})
    tracks.append({"clips": caption_clips})
    if broll_clips:
        tracks.append({"clips": broll_clips})
    tracks.append({"clips": video_clips})
    if sfx_clips:
        tracks.append({"clips": sfx_clips})
    if music_clips:
        tracks.append({"clips": music_clips})

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
        duration = _probe_duration(video_bytes)
        print(f"[VideoProduction] duration={duration:.1f}s", flush=True)

        # ── Stage 1: Reap — word-level transcript + timestamps + clip detection ──
        await update(5, "Transcribing…")
        upload_id = await reap.upload_video(video_bytes, f"{job_id}_raw.mp4")
        if not upload_id:
            raise RuntimeError("Upload to Reap failed")

        trans_id = await reap.start_transcription(upload_id, "en")
        if not trans_id:
            raise RuntimeError("Transcription failed to start")

        srt_text, _reap_video_url, tracking_data = await reap.fetch_full_transcript_data(
            trans_id, timeout_seconds=300
        )
        if not srt_text:
            raise RuntimeError("Transcription timed out or returned empty")

        print(
            f"[VideoProduction] srt={len(srt_text)}ch tracking={bool(tracking_data)}",
            flush=True,
        )

        # ── Stage 2: Audio cleanup — noise reduction, leveling, de-essing ────────
        await update(30, "Cleaning audio…")
        cleaned_bytes = await _clean_audio(video_bytes)

        # Host the cleaned video on Cloudinary so Shotstack can fetch it via CDN URL.
        # Priority: Cloudinary → static server → Reap raw URL (fallback).
        await update(38, "Uploading clean video…")
        clean_video_url = await _upload_to_cloudinary(cleaned_bytes, job_id)

        if not clean_video_url:
            # Cloudinary not configured or failed — write to static server
            print("[VideoProduction] Cloudinary unavailable, writing to static server", flush=True)
            video_static_dir = "/app/static/videos"
            os.makedirs(video_static_dir, exist_ok=True)
            try:
                with open(f"{video_static_dir}/{job_id}.mp4", "wb") as vf:
                    vf.write(cleaned_bytes)
                clean_video_url = f"https://api-staging.urisocial.com/static/videos/{job_id}.mp4"
                print(f"[VideoProduction] static fallback: {len(cleaned_bytes)//1024}KB → {clean_video_url}", flush=True)
            except Exception as e:
                print(f"[VideoProduction] static write failed ({e}), using Reap URL", flush=True)
                clean_video_url = _reap_video_url

        if not clean_video_url:
            raise RuntimeError("Could not obtain a video URL for rendering")

        print(f"[VideoProduction] render source={clean_video_url[:80]}…", flush=True)

        # ── Stage 3: GPT-4o edit decisions ───────────────────────────────────────
        await update(48, "AI making edit decisions…")
        decisions = await get_edit_decisions(srt_text, video_type, duration, tracking_data)
        cuts = decisions.get("cuts", [])
        zooms = decisions.get("zooms", [])
        sound_effects = decisions.get("sound_effects", [])
        broll_decisions = decisions.get("broll", [])[:3]
        pacing_note = decisions.get("pacing_note", "")
        music_mood = decisions.get("music_mood", "upbeat")
        hook_text = decisions.get("hook_text", "")
        print(
            f"[VideoProduction] cuts={len(cuts)} zooms={len(zooms)} "
            f"sfx={len(sound_effects)} broll={len(broll_decisions)} pacing={pacing_note} "
            f"hook='{hook_text}'",
            flush=True,
        )

        # ── Stage 4: Fetch assets — b-roll (Pexels → fal.ai) + SFX library ──────
        broll: List[Dict] = []
        if broll_decisions:
            await update(55, "Fetching b-roll assets…")
            tasks = [
                _fetch_broll_url(br.get("description", ""), br.get("concept", ""))
                for br in broll_decisions
            ]
            urls = await asyncio.gather(*tasks, return_exceptions=True)
            for br, url in zip(broll_decisions, urls):
                if isinstance(url, str) and url:
                    broll.append({**br, "url": url})
            print(f"[VideoProduction] broll resolved {len(broll)}/{len(broll_decisions)}", flush=True)

        # ── Stage 4b: Fetch background music from Jamendo ────────────────────────
        await update(59, "Fetching background music…")
        music_url = await _fetch_music_url(music_mood)
        if not music_url:
            print(f"[Music] no track found for mood={music_mood}, skipping", flush=True)

        # ── Stage 5: Shotstack render + mix ──────────────────────────────────────
        await update(62, "Building edit timeline…")
        srt_entries = _parse_srt(srt_text)
        timeline = build_shotstack_timeline(
            video_url=clean_video_url,      # cleaned voice track
            video_duration=duration,
            cuts=cuts,
            zooms=zooms,
            srt_entries=srt_entries,
            sound_effects=sound_effects,
            broll=broll,
            aspect_ratio="9:16",
            job_id=job_id,
            music_url=music_url,
            hook_text=hook_text,
        )

        await update(68, "Rendering video…")
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
                             broll=broll,
                             pacing_note=pacing_note,
                             music_mood=music_mood,
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
