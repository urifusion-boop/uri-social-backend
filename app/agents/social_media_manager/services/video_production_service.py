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
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

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
        audio_kbps = 128
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
            "-c:a", "aac", "-b:a", "192k",
            "-af", "highpass=f=80,loudnorm=I=-14:TP=-1.0:LRA=11",
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
    Upload video bytes to Cloudinary (direct byte upload).
    Only suitable for files under ~95MB. For larger files use _cloudinary_fetch_url.
    """
    cloud = settings.CLOUDINARY_CLOUD_NAME
    api_key = settings.CLOUDINARY_API_KEY
    api_secret = settings.CLOUDINARY_API_SECRET
    if not all([cloud, api_key, api_secret]):
        return None

    if len(video_bytes) > 95 * 1024 * 1024:
        print(f"[Cloudinary] file {len(video_bytes)//1024//1024}MB > 95MB limit — skipping direct upload", flush=True)
        return None

    folder = "uri-video-production"
    ts = int(time.time())
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
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                text = await resp.text()
                if not text.strip():
                    print(f"[Cloudinary] empty response status={resp.status}", flush=True)
                    return None
                body = json.loads(text)
                if not resp.ok:
                    print(f"[Cloudinary] upload failed {resp.status}: {body}", flush=True)
                    return None
                url = body.get("secure_url", "")
                print(f"[Cloudinary] uploaded {len(video_bytes)//1024}KB → {url}", flush=True)
                return url
    except Exception as e:
        print(f"[Cloudinary] error: {e}", flush=True)
        return None


async def _cloudinary_fetch_url(source_url: str, public_id: str) -> Optional[str]:
    """
    Tell Cloudinary to fetch a video from source_url — avoids file size limits entirely.
    Cloudinary downloads from the URL server-side; we never transfer bytes.
    """
    cloud = settings.CLOUDINARY_CLOUD_NAME
    api_key = settings.CLOUDINARY_API_KEY
    api_secret = settings.CLOUDINARY_API_SECRET
    if not all([cloud, api_key, api_secret]):
        return None

    folder = "uri-video-production"
    ts = int(time.time())
    params_str = f"folder={folder}&public_id={public_id}&timestamp={ts}"
    signature = hashlib.sha1(f"{params_str}{api_secret}".encode()).hexdigest()

    form = aiohttp.FormData()
    form.add_field("file", source_url)   # URL string — Cloudinary fetches it
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
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                text = await resp.text()
                if not text.strip():
                    print(f"[Cloudinary] fetch-url empty response status={resp.status}", flush=True)
                    return None
                body = json.loads(text)
                if not resp.ok:
                    print(f"[Cloudinary] fetch-url failed {resp.status}: {body}", flush=True)
                    return None
                url = body.get("secure_url", "")
                print(f"[Cloudinary] fetch-url → {url}", flush=True)
                return url
    except Exception as e:
        print(f"[Cloudinary] fetch-url error: {e}", flush=True)
        return None


# ── B-roll asset fetch ────────────────────────────────────────────────────────

# Cloudinary transition style per video type — fadewhite is the flash cut Shotstack lacked
# Luma matte public IDs uploaded to our Cloudinary account.
# Black→white wipe: black areas show the base clip, white areas show the next clip.
_LUMA_MATTE_BY_TYPE: Dict[str, str] = {
    "tiktok":   "uri-transitions/circle-wipe",    # hard-edge circle expanding from center
    "product":  "uri-transitions/diagonal-wipe",  # diagonal sweep top-left → bottom-right
    "founder":  "uri-transitions/circle-wipe",    # same circle but slower (2.5s)
}
# Per-type transition duration (seconds). Must be less than every keep-segment.
_TRANSITION_DUR_BY_TYPE: Dict[str, float] = {
    "tiktok":   0.8,
    "product":  0.8,
    "founder":  1.0,
}
_CLD_TRANSITION_DUR = 1.0   # default fallback
_MIN_SEG_DUR       = 4.0   # minimum keep-segment length (must exceed max transition_dur)

# Shotstack overlay style per video type for topic-change transitions.
# "color: None" → falls back to the brand primary_color at render time.
_TRANSITION_STYLE_BY_VIDEO_TYPE: Dict[str, Dict] = {
    "tiktok":       {"type": "flash", "color": "#ffffff", "duration": 0.08, "opacity": 0.70},
    "product":      {"type": "flash", "color": None,      "duration": 0.10, "opacity": 0.45},
    "founder":      {"type": "swipe", "color": None,      "duration": 0.20, "opacity": 0.55},
    "educational":  {"type": "flash", "color": "#ffffff", "duration": 0.10, "opacity": 0.45},
    "podcast":      {"type": "flash", "color": "#ffffff", "duration": 0.08, "opacity": 0.35},
    "professional": {"type": "swipe", "color": None,      "duration": 0.18, "opacity": 0.50},
    "social_media": {"type": "flash", "color": "#ffffff", "duration": 0.06, "opacity": 0.75},
}
_DEFAULT_TRANSITION_STYLE: Dict[str, Any] = {
    "type": "flash", "color": "#ffffff", "duration": 0.10, "opacity": 0.50,
}


# ── Icon overlay asset library ────────────────────────────────────────────────
# Each category has an emoji fallback and an optional Lottie JSON URL.
# Set LOTTIEFILES_API_KEY env var to enable dynamic search (free account suffices).
# Lottie URLs can also be replaced with Cloudinary-hosted .json raw assets.
_CDN = "https://res.cloudinary.com/df8ckaeam/raw/upload"
_ICON_OVERLAY_LIBRARY: Dict[str, Dict[str, Any]] = {
    "fire":      {"emoji": "🔥", "lottie": f"{_CDN}/uri-lottie/fire.json", "position": "topRight",     "size": 150},
    "star":      {"emoji": "⭐", "lottie": f"{_CDN}/uri-lottie/star.json", "position": "topRight",     "size": 140},
    "money":     {"emoji": "💰", "lottie": None,                            "position": "topRight",     "size": 140},
    "chart":     {"emoji": "📈", "lottie": None,                            "position": "topRight",     "size": 130},
    "celebrate": {"emoji": "🎉", "lottie": None,                            "position": "topCenter",    "size": 180},
    "arrow_up":  {"emoji": "👆", "lottie": f"{_CDN}/uri-lottie/idea.json", "position": "bottomCenter", "size": 130},
    "heart":     {"emoji": "❤️", "lottie": None,                            "position": "topRight",     "size": 130},
    "rocket":    {"emoji": "🚀", "lottie": None,                            "position": "topRight",     "size": 150},
}
_LOTTIEFILES_API_URL = "https://graphql.lottiefiles.com/2022-08"


async def _search_lottiefiles_api(keyword: str, api_key: str) -> Optional[str]:
    """
    Query the LottieFiles GraphQL API for a public animation matching `keyword`.
    Returns the JSON download URL of the first result, or None on failure.
    """
    gql = """
    query Search($q: String!) {
      searchPublicAnimations(query: $q, first: 1) {
        edges { node { jsonUrl } }
      }
    }
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _LOTTIEFILES_API_URL,
                json={"query": gql, "variables": {"q": keyword}},
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=6),
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    edges = (
                        body.get("data", {})
                            .get("searchPublicAnimations", {})
                            .get("edges", [])
                    )
                    if edges:
                        url = edges[0]["node"].get("jsonUrl", "")
                        if url:
                            return url
    except Exception as exc:
        print(f"[LottieFiles] API search failed keyword={keyword!r}: {exc}", flush=True)
    return None


async def _resolve_icon_html(category: str) -> str:
    """
    Build a Shotstack-ready HTML string for an icon overlay.
    If a LottieFiles API key is available (env: LOTTIEFILES_API_KEY), attempts to fetch
    an animated Lottie JSON and inlines it; otherwise falls back to a bouncing emoji.
    """
    cfg  = _ICON_OVERLAY_LIBRARY.get(category, _ICON_OVERLAY_LIBRARY["star"])
    size = int(cfg["size"])

    lottie_json_str: Optional[str] = None
    lottie_url: Optional[str] = cfg.get("lottie")

    # If no pre-configured URL, try the API
    if not lottie_url:
        api_key = os.getenv("LOTTIEFILES_API_KEY", "")
        if api_key:
            lottie_url = await _search_lottiefiles_api(category.replace("_", " "), api_key)

    # Fetch the Lottie JSON so we can inline it (headless Chrome may not reach external URLs)
    if lottie_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(lottie_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        lottie_json_str = await resp.text()
        except Exception as exc:
            print(f"[IconOverlay] Lottie fetch failed url={lottie_url!r}: {exc}", flush=True)

    # Pure CSS emoji animation — no external scripts, always works in Shotstack's renderer.
    # Lottie JSON is fetched above but we rely on CSS-only rendering for reliability;
    # the Lottie bodymovin player needs an external CDN script that may not load in time.
    emoji = cfg.get("emoji", "⭐")
    fs    = int(size * 0.72)
    return (
        f"<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        f"<style>*{{margin:0;padding:0;}}"
        f"@keyframes pop{{0%,100%{{transform:scale(1) rotate(-8deg);}}"
        f"50%{{transform:scale(1.25) rotate(8deg);}}}}"
        f"body{{width:{size}px;height:{size}px;display:flex;align-items:center;"
        f"justify-content:center;background:transparent;overflow:hidden;}}"
        f"span{{font-size:{fs}px;line-height:1;"
        f"filter:drop-shadow(0 4px 8px rgba(0,0,0,0.5));"
        f"animation:pop 0.6s ease-in-out infinite;}}</style></head>"
        f"<body><span>{emoji}</span></body></html>"
    )


def _cloudinary_public_id(url: str) -> str:
    """Extract Cloudinary public_id from a full upload URL.
    e.g. .../video/upload/v1234567/uri-video-production/abc.mp4 → uri-video-production/abc
    """
    import re
    m = re.search(r"/video/upload/(?:v\d+/)?(.+?)(?:\.\w+)?$", url)
    return m.group(1) if m else ""


def _wrap_hook_text(text: str, max_chars: int = 20) -> str:
    """Split text at word boundaries for multi-line Cloudinary text overlay."""
    words = text.split()
    lines, line = [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > max_chars:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        lines.append(line)
    return "\n".join(lines)


def _cld_encode_text(text: str) -> str:
    """URL-encode text for a Cloudinary l_text layer. Commas and slashes must be double-encoded."""
    encoded = urllib.parse.quote(text, safe='')
    encoded = encoded.replace('%2C', '%252C')
    encoded = encoded.replace('%2F', '%252F')
    return encoded


def _build_cloudinary_cut_url(
    public_id: str,
    keep_segments: List[Dict],
    luma_matte_pid: Optional[str] = "uri-transitions/circle-wipe",
    transition_dur: float = 1.0,
    hook_text: str = "",
    primary_color: str = "#FFD700",
) -> str:
    """
    Build a Cloudinary transformation URL: luma-matte transitions between segments,
    then a branded hook text overlay baked into the first 2.5s of the video.

    Pass luma_matte_pid=None for hard cuts (no transition effect between segments).
    Hook overlay uses Cloudinary's native l_text layer — rendered server-side at
    full resolution, completely avoiding Shotstack's unreliable HTML renderer.
    """
    cloud = settings.CLOUDINARY_CLOUD_NAME
    if not cloud or not keep_segments:
        return ""

    pid_enc = public_id.replace("/", ":")

    first = keep_segments[0]
    parts: List[str] = [
        f"so_{first['src_start']:.3f},eo_{first['src_end']:.3f}"
    ]
    for seg in keep_segments[1:]:
        s = f"{seg['src_start']:.3f}"
        e = f"{seg['src_end']:.3f}"
        if luma_matte_pid:
            luma_enc = luma_matte_pid.replace("/", ":")
            parts.append(
                f"l_video:{pid_enc}/so_{s},eo_{e}"
                f"/e_transition,l_video:{luma_enc}/fl_layer_apply"
                f"/fl_layer_apply"
            )
        else:
            # Hard cut — just concatenate the segment with no transition layer
            parts.append(
                f"l_video:{pid_enc}/so_{s},eo_{e}/fl_layer_apply"
            )

    url = (
        f"https://res.cloudinary.com/{cloud}/video/upload/"
        + "/".join(parts)
    )

    # Hook text overlay — baked into the Cloudinary video, shown for the first 2.5s.
    # Using Montserrat 900 (Google Fonts), brand primary color background, white text.
    # Font size scales with text length so it never overflows the 1080px frame.
    if hook_text:
        text_upper  = hook_text.upper()
        # Wrap at 20 chars so each line is ~1080px wide at 100px font.
        # \n encodes to %0A in the Cloudinary URL → multi-line text layer.
        # At 100px, a 20-char line is ~1300px; c_fit scales it to 1080px
        # (effective ~83px) and the background fills the full frame width.
        wrapped     = _wrap_hook_text(text_upper, max_chars=20)
        encoded     = _cld_encode_text(wrapped)
        primary_hex = primary_color.lstrip("#")
        url += (
            f"/b_rgb:{primary_hex},co_rgb:FFFFFF"
            f",l_text:Montserrat@google_100_900:{encoded}"
            f"/w_1080,c_fit"
            f"/eo_2.5,fl_layer_apply,g_north,fl_relative,y_0.05"
        )
        print(f"[CloudinaryHook] '{text_upper}' 100px wrapped #{primary_hex}", flush=True)

    # Request high-quality audio from Cloudinary when delivering the cut video.
    url += f"/ac_aac,br_192k/{public_id}.mp4"
    return url


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
# Curated CC0 instrumentals uploaded to Cloudinary from archive.org.
# To add more tracks: download a CC0 MP3, upload via cloudinary.uploader.upload(resource_type="video"),
# then append the secure_url to the appropriate mood list below.

import random as _random

_MUSIC_BY_MOOD: Dict[str, List[str]] = {
    "upbeat": [
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851582/uri-music/upbeat/track-0.mp3",
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851597/uri-music/upbeat/track-1.mp3",
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851609/uri-music/upbeat/track-2.mp3",
    ],
    "chill": [
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851623/uri-music/chill/track-0.mp3",
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851634/uri-music/chill/track-1.mp3",
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851652/uri-music/chill/track-2.mp3",
    ],
    "cinematic": [
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851665/uri-music/cinematic/track-0.mp3",
    ],
    "dramatic": [],  # falls back to cinematic
    "acoustic": [
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851680/uri-music/acoustic/track-0.mp3",
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851690/uri-music/acoustic/track-1.mp3",
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851704/uri-music/acoustic/track-2.mp3",
    ],
    "electronic": [
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851717/uri-music/electronic/track-0.mp3",
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851729/uri-music/electronic/track-1.mp3",
        "https://res.cloudinary.com/df8ckaeam/video/upload/v1781851740/uri-music/electronic/track-2.mp3",
    ],
}

_MOOD_FALLBACK: Dict[str, str] = {
    "dramatic": "cinematic",
    "cinematic": "electronic",
}


def _pick_music_url(mood: str) -> Optional[str]:
    """Pick a random track URL for the given mood. Falls back through _MOOD_FALLBACK."""
    seen: set[str] = set()
    m = mood
    while m not in seen:
        tracks = _MUSIC_BY_MOOD.get(m, [])
        if tracks:
            url = _random.choice(tracks)
            print(f"[Music] mood={mood} → picked {m} track: {url.split('/')[-1]}", flush=True)
            return url
        seen.add(m)
        m = _MOOD_FALLBACK.get(m, "")
    print(f"[Music] no track found for mood={mood}", flush=True)
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


# ── Phase 1: Content Analysis Engine — Phase 2: Editing Rules Engine ─────────

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


_STYLE_GUIDES: Dict[str, str] = {
    "tiktok":       "TikTok / Reels — fast, punchy, zero dead air. Hook in first 3s. High energy throughout.",
    "product":      "Product demo — clean, confident, benefit-led. Show the transformation. Data-driven.",
    "founder":      "Founder story — authentic, warm, credible. Preserve natural rhythm, don't over-cut.",
    "educational":  "Educational — clear structure, highlight key insights and stats. Build understanding step by step.",
    "podcast":      "Podcast clip — conversational energy, let ideas breathe but cut all dead air.",
    "professional": "Professional — polished, confident, data-driven. Corporate credibility.",
    "social_media": "Social Media — high energy, relatable, trend-aware. Optimised for feed scroll-stop.",
}

_CONFIDENCE_THRESHOLD = 0.80  # below this, skip the decision


async def analyze_content(
    srt_text: str,
    video_type: str,
    duration: float,
    tracking_data: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Phase 1 — Content Analysis Engine.
    GPT understands the content BEFORE any editing decisions are made.
    Returns structured analysis: hook, main_points, cta, topic_changes,
    emphasis_moments (with strength 1–10), keywords, cuts, music_mood.
    """
    style = _STYLE_GUIDES.get(video_type, "Short-form social video.")
    tracking_context = _summarise_tracking_data(tracking_data or {}, duration)

    prompt = f"""You are an expert content analyst for short-form social video.
Your job is to UNDERSTAND the content structure and identify what matters — before any editing decisions are made.

VIDEO TYPE: {video_type}
DURATION: {duration:.1f}s
STYLE: {style}

TRANSCRIPT (SRT — timestamps are original video seconds):
{srt_text}
{f"WORD TIMING DATA:{tracking_context}" if tracking_context else ""}

ANALYSIS TASKS:

1. HOOK — The opening moment that earns the viewer's attention (usually 0–5s).
   hook_type: curiosity | shock | question | story | stat
   strength 1–10: how scroll-stopping is it?

2. MAIN POINTS — Map the content into 2–5 segments. What topic does each section cover?

3. CTA — Any call to action near the end (follow, subscribe, comment, buy, etc.)

4. TOPIC CHANGES — Moments where the speaker shifts to a distinctly new topic.
   confidence 0.0–1.0.

5. EMPHASIS MOMENTS — Strong claims, statistics, surprising facts, emotional peaks.
   STRENGTH SCALE (be precise — this drives the editing engine):
   8–10: Peak moment. Specific number, boldest claim, most surprising statement. Use sparingly (1–2 max).
   4–7: Important supporting point worth a subtle punch.
   1–3: Minor detail — skip it.
   Include confidence 0.0–1.0.

6. KEYWORDS — Brands, platforms, products, metrics, important nouns. Max 5.
   These feed the visual asset search engine for b-roll and icons.

7. CUTS — Remove only spoken content with zero value:
   - Filler words/phrases: "um", "uh", "like", "you know", "I mean", "basically", "right?", "okay so"
   - False starts: speaker begins, stops, restarts the same sentence
   - Exact repetitions: same idea said twice in a row
   - remove_start and remove_end within [0, {duration:.1f}]
   - Every segment kept between cuts must be ≥4s
   - confidence 0.0–1.0

8. MUSIC MOOD — One word: upbeat | chill | cinematic | dramatic | acoustic | electronic

Return ONLY valid JSON, no markdown:
{{
  "hook": {{
    "start": 0,
    "end": 4.5,
    "text": "3 POSTS PER WEEK",
    "hook_type": "curiosity",
    "strength": 9
  }},
  "main_points": [
    {{"start": 5, "end": 18, "summary": "Why consistency matters on social media"}},
    {{"start": 19, "end": 35, "summary": "How content generates leads"}},
    {{"start": 36, "end": 52, "summary": "Real-world example with results"}}
  ],
  "cta": {{
    "start": 53,
    "end": 60,
    "text": "Follow for daily content tips"
  }},
  "topic_changes": [
    {{"at": 18.5, "confidence": 0.94}},
    {{"at": 35.2, "confidence": 0.91}}
  ],
  "emphasis_moments": [
    {{"at": 12.5, "text": "This changed everything", "strength": 8, "confidence": 0.92}},
    {{"at": 29.8, "text": "500 leads in 30 days", "strength": 10, "confidence": 0.98}}
  ],
  "keywords": ["Instagram", "leads", "content strategy"],
  "cuts": [
    {{"remove_start": 4.2, "remove_end": 5.8, "reason": "filler: um you know", "confidence": 0.98}}
  ],
  "music_mood": "upbeat",
  "pacing_note": "tight with emotional peak at 30s"
}}"""

    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model="gpt-5",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    text = response.choices[0].message.content or "{}"
    try:
        result = json.loads(text)
        print(
            f"[ContentAnalysis] hook='{result.get('hook', {}).get('text', '')}' "
            f"main_points={len(result.get('main_points', []))} "
            f"emphasis={len(result.get('emphasis_moments', []))} "
            f"topic_changes={len(result.get('topic_changes', []))} "
            f"keywords={result.get('keywords', [])} "
            f"cuts={len(result.get('cuts', []))}",
            flush=True,
        )
        return result
    except json.JSONDecodeError:
        return {}


def apply_editing_rules(analysis: Dict[str, Any], duration: float, enable_sfx: bool = True) -> Dict[str, Any]:
    """
    Phase 2 — Editing Rules Engine.
    Deterministic conversion of content analysis into render decisions.
    GPT does NOT invent effects — this engine selects from the approved action library.

    Strength mapping (from PRD):
      8–10: zoom_in (strong) + impact_sound + caption_highlight
      4–7:  zoom_in (subtle)
      1–3:  no action
    """
    # ── Cuts: only high-confidence decisions apply automatically ─────────────
    cuts = [
        c for c in analysis.get("cuts", [])
        if float(c.get("confidence", 1.0)) >= _CONFIDENCE_THRESHOLD
    ]

    zooms: List[Dict] = []
    sound_effects: List[Dict] = []
    used_times: List[float] = []

    def _time_clear(at: float, min_gap: float = 4.0) -> bool:
        return all(abs(at - t) >= min_gap for t in used_times)

    # ── Emphasis rules ───────────────────────────────────────────────────────
    for moment in sorted(
        analysis.get("emphasis_moments", []), key=lambda m: -int(m.get("strength", 0))
    ):
        strength = int(moment.get("strength", 0))
        at = float(moment.get("at", 0))
        conf = float(moment.get("confidence", 1.0))

        if conf < _CONFIDENCE_THRESHOLD or at >= duration or not _time_clear(at):
            continue

        if strength >= 8:
            zooms.append({"at": at, "intensity": "strong", "reason": moment.get("text", "")})
            sound_effects.append({"at": at, "type": "impact", "reason": "emphasis ≥8"})
            used_times.append(at)
        elif strength >= 4:
            zooms.append({"at": at, "intensity": "subtle", "reason": moment.get("text", "")})
            used_times.append(at)

    # ── Topic change rules: whoosh SFX at each shift ─────────────────────────
    if enable_sfx:
        for change in analysis.get("topic_changes", []):
            at = float(change.get("at", 0))
            conf = float(change.get("confidence", 1.0))
            if conf >= _CONFIDENCE_THRESHOLD and at < duration and _time_clear(at, min_gap=2.0):
                sound_effects.append({"at": at, "type": "whoosh", "reason": "topic change"})

    # ── Icon overlays — animated emoji / Lottie at emphasis + CTA moments ───────
    icon_overlays: List[Dict[str, Any]] = []
    for moment in analysis.get("emphasis_moments", []):
        conf     = float(moment.get("confidence", 0))
        strength = int(moment.get("strength", 0))
        at       = float(moment.get("at", 0))
        if conf >= _CONFIDENCE_THRESHOLD and strength >= 7 and at < duration and _time_clear(at, min_gap=2.0):
            category = "fire" if strength >= 9 else "star"
            icon_overlays.append({"at": round(at, 2), "duration": 1.5, "category": category})

    cta_section = analysis.get("cta") or {}
    cta_start = cta_section.get("start")
    if cta_start is not None and float(cta_start) < duration:
        icon_overlays.append({"at": round(float(cta_start) + 1.0, 2), "duration": 3.0, "category": "arrow_up"})

    # ── B-roll from main_points + keywords ───────────────────────────────────
    keywords = analysis.get("keywords", [])
    broll: List[Dict] = []
    for i, point in enumerate(analysis.get("main_points", [])[:3]):
        at = float(point.get("start", 0)) + 2.0
        if at >= duration:
            continue
        # Use keyword matching the point index, fall back to summary
        concept = keywords[i] if i < len(keywords) else ""
        description = point.get("summary", concept)
        if concept or description:
            broll.append({
                "at": round(at, 2),
                "duration": 3.0,
                "description": description,
                "concept": concept or description,
                "reason": f"visual for: {description}",
            })

    # ── Hook ─────────────────────────────────────────────────────────────────
    hook = analysis.get("hook", {})
    hook_text = hook.get("text", "").upper().strip() if hook else ""

    # ── Caption cues — time windows that get styled caption tracks ────────────
    # emphasis_moments with strength ≥7 → "emphasis" style (bigger, bolder)
    # CTA section → "cta" style (brand color, higher position)
    # SRT entries with 3+ digit numbers/metrics → auto-detected as "metric"
    caption_cues: List[Dict[str, Any]] = []
    for moment in analysis.get("emphasis_moments", []):
        conf     = float(moment.get("confidence", 0))
        strength = int(moment.get("strength", 0))
        at       = float(moment.get("at", 0))
        if conf >= _CONFIDENCE_THRESHOLD and at < duration and strength >= 7:
            caption_cues.append({
                "start": max(0.0, at - 0.5),
                "end":   min(duration, at + 3.5),
                "type":  "emphasis",
            })
    cta = analysis.get("cta") or {}
    cta_start = cta.get("start")
    cta_end   = cta.get("end")
    if cta_start is not None and cta_end is not None:
        caption_cues.append({
            "start": float(cta_start),
            "end":   float(cta_end),
            "type":  "cta",
        })

    return {
        "cuts":             cuts,
        "zooms":            zooms[:4],
        "sound_effects":    sound_effects[:4],
        "broll":            broll[:3],
        "hook_text":        hook_text,
        "music_mood":       analysis.get("music_mood", "upbeat"),
        "topic_changes":    analysis.get("topic_changes", []),
        "emphasis_moments": analysis.get("emphasis_moments", []),
        "keywords":         keywords,
        "caption_cues":     caption_cues,
        "icon_overlays":    icon_overlays[:5],  # cap at 5 so the timeline doesn't get crowded
        "content_structure": {
            "hook":        hook,
            "main_points": analysis.get("main_points", []),
            "cta":         analysis.get("cta", {}),
        },
        "pacing_note": analysis.get("pacing_note", ""),
    }

# ── Caption type detection ────────────────────────────────────────────────────

# Auto-detect metric captions: 3+ digit numbers, multipliers, currency
_METRIC_PATTERN = re.compile(
    r'(?:'
    r'\b\d{3,}[\d,.]*\b'          # 500, 5000, 5,000
    r'|\b\d+\s*[kKmMbB%x]\b'     # 5k, 3M, 50%, 3x
    r'|[£$₦€]\s*\d+'              # $50, £100, ₦5000
    r')',
)


def _get_caption_type(
    orig_start: float,
    caption_cues: List[Dict[str, Any]],
    entry_text: str,
) -> str:
    """Assign an SRT entry to a caption style type based on time-based cues or text content."""
    for cue in caption_cues:
        if float(cue.get("start", -1)) <= orig_start < float(cue.get("end", -1)):
            return cue.get("type", "standard")
    if _METRIC_PATTERN.search(entry_text):
        return "metric"
    return "standard"


# ── Algorithmic silence + filler + repetition detection ──────────────────────

_SILENCE_THRESHOLD: Dict[str, float] = {
    "tiktok":   0.3,   # cut any gap ≥0.3s — tight, no dead air
    "product":  0.5,
    "founder":  0.9,
}

# Matches a whole word that is a filler sound (um, uh, etc.)
_FILLER_WORD_RE = re.compile(
    r"^(um+|uh+|ah+|hmm+|er+|erm+|mhm+|uhh+|umm+|huh|mm+)$",
    re.IGNORECASE,
)

# Multi-word filler phrases (lowercase, no punctuation). Conservative — only
# phrases that are unambiguously filler regardless of sentence position.
_FILLER_NGRAMS: List[Tuple[str, ...]] = [
    ("you", "know"),
    ("i", "mean"),
    ("you", "know", "what", "i", "mean"),
]


def _extract_words(tracking_data: dict) -> List[Dict[str, Any]]:
    """Normalize Reap trackingData into [{word, start, end}] list."""
    raw = (
        tracking_data.get("words")
        or tracking_data.get("segments")
        or tracking_data.get("transcript", {}).get("words")
        or []
    )
    out: List[Dict[str, Any]] = []
    for w in raw:
        text  = str(w.get("word") or w.get("text") or "").strip()
        start = float(w.get("start") or w.get("startTime") or 0)
        end   = float(w.get("end")   or w.get("endTime")   or start + 0.1)
        if text:
            out.append({"word": text, "start": start, "end": end})
    return out


def _auto_cuts_from_words(
    words: List[Dict[str, Any]],
    duration: float,
    video_type: str = "tiktok",
) -> List[Dict]:
    """
    Silence detection from word-level timestamps.
    Catches mid-sentence pauses that SRT entry boundaries miss entirely.
    """
    threshold = _SILENCE_THRESHOLD.get(video_type, 1.0)
    cuts: List[Dict] = []
    if not words:
        return cuts

    if words[0]["start"] > threshold:
        cuts.append({
            "remove_start": 0.0,
            "remove_end":   round(words[0]["start"], 3),
            "reason":       f"leading silence {words[0]['start']:.1f}s",
        })

    for i in range(1, len(words)):
        gap_start = words[i - 1]["end"]
        gap_end   = words[i]["start"]
        gap       = gap_end - gap_start
        if gap > threshold:
            cuts.append({
                "remove_start": round(gap_start, 3),
                "remove_end":   round(gap_end, 3),
                "reason":       f"silence {gap:.1f}s",
            })

    if duration - words[-1]["end"] > threshold:
        cuts.append({
            "remove_start": round(words[-1]["end"], 3),
            "remove_end":   round(duration, 3),
            "reason":       f"trailing silence {duration - words[-1]['end']:.1f}s",
        })

    return cuts


def _auto_cuts_from_srt(
    srt_entries: List[Dict[str, Any]],
    duration: float,
    video_type: str = "tiktok",
) -> List[Dict]:
    """SRT-entry gap fallback — used only when word-level data is unavailable."""
    threshold = _SILENCE_THRESHOLD.get(video_type, 1.0)
    cuts: List[Dict] = []
    if not srt_entries:
        return cuts

    if srt_entries[0]["start"] > threshold:
        cuts.append({
            "remove_start": 0.0,
            "remove_end":   round(srt_entries[0]["start"], 3),
            "reason":       f"leading silence {srt_entries[0]['start']:.1f}s",
        })

    for i in range(1, len(srt_entries)):
        gap_start = srt_entries[i - 1]["end"]
        gap_end   = srt_entries[i]["start"]
        gap       = gap_end - gap_start
        if gap > threshold:
            cuts.append({
                "remove_start": round(gap_start, 3),
                "remove_end":   round(gap_end, 3),
                "reason":       f"silence {gap:.1f}s",
            })

    if duration - srt_entries[-1]["end"] > threshold:
        cuts.append({
            "remove_start": round(srt_entries[-1]["end"], 3),
            "remove_end":   round(duration, 3),
            "reason":       f"trailing silence {duration - srt_entries[-1]['end']:.1f}s",
        })

    return cuts


def _filler_cuts_from_words(words: List[Dict[str, Any]]) -> List[Dict]:
    """
    Detects filler words and phrases at word level — catches fillers embedded
    inside sentences ("it was um really good") that SRT-level matching misses.
    """
    cuts: List[Dict] = []
    skip_until = -1
    for i, w in enumerate(words):
        if i <= skip_until:
            continue
        clean = w["word"].lower().strip(".,!?;:\"'")

        if _FILLER_WORD_RE.match(clean):
            cuts.append({
                "remove_start": round(w["start"], 3),
                "remove_end":   round(w["end"],   3),
                "reason":       f'filler: "{w["word"].strip()}"',
            })
            continue

        for ngram in _FILLER_NGRAMS:
            n = len(ngram)
            if i + n > len(words):
                continue
            window = tuple(
                words[k]["word"].lower().strip(".,!?;:\"'") for k in range(i, i + n)
            )
            if window == ngram:
                cuts.append({
                    "remove_start": round(words[i]["start"], 3),
                    "remove_end":   round(words[i + n - 1]["end"], 3),
                    "reason":       f'filler phrase: "{" ".join(ngram)}"',
                })
                skip_until = i + n - 1
                break

    return cuts


def _filler_cuts_from_srt(srt_entries: List[Dict[str, Any]]) -> List[Dict]:
    """SRT-level filler fallback — only whole-entry fillers."""
    cuts = []
    for entry in srt_entries:
        clean = entry["text"].lower().strip(".,!?;:\"' ")
        if _FILLER_WORD_RE.match(clean):
            cuts.append({
                "remove_start": round(entry["start"], 3),
                "remove_end":   round(entry["end"],   3),
                "reason":       f'filler: "{entry["text"].strip()}"',
            })
    return cuts


def _repetition_cuts_from_words(words: List[Dict[str, Any]]) -> List[Dict]:
    """
    Phrase-level repetition detection at word granularity.
    Scans a 25-word window for repeated sequences of ≥4 words.
    Keeps the first occurrence, cuts the second.
    """
    cuts: List[Dict] = []
    already_cut: Set[int] = set()
    look_ahead = 25

    for i in range(len(words)):
        if i in already_cut:
            continue
        for phrase_len in range(6, 3, -1):  # try longer phrases first
            if i + phrase_len > len(words):
                continue
            phrase = tuple(
                w["word"].lower().strip(".,!?;:\"'") for w in words[i:i + phrase_len]
            )
            for j in range(i + phrase_len, min(i + look_ahead, len(words) - phrase_len + 1)):
                if j in already_cut:
                    continue
                candidate = tuple(
                    w["word"].lower().strip(".,!?;:\"'") for w in words[j:j + phrase_len]
                )
                if phrase == candidate:
                    cuts.append({
                        "remove_start": round(words[j]["start"], 3),
                        "remove_end":   round(words[j + phrase_len - 1]["end"], 3),
                        "reason":       f'repetition: "{" ".join(phrase)}"',
                    })
                    for k in range(j, j + phrase_len):
                        already_cut.add(k)
                    break

    return cuts


def _repetition_cuts_from_srt(srt_entries: List[Dict[str, Any]]) -> List[Dict]:
    """SRT-level repetition fallback — whole-entry exact matches."""
    cuts: List[Dict] = []
    already_cut: Set[int] = set()
    for i, entry_i in enumerate(srt_entries):
        words_i = entry_i["text"].lower().strip().split()
        if len(words_i) < 3 or i in already_cut:
            continue
        for j in range(i + 1, min(i + 6, len(srt_entries))):
            if j in already_cut:
                continue
            words_j = srt_entries[j]["text"].lower().strip().split()
            if words_i == words_j:
                cuts.append({
                    "remove_start": round(srt_entries[j]["start"], 3),
                    "remove_end":   round(srt_entries[j]["end"],   3),
                    "reason":       f'repetition: "{srt_entries[j]["text"].strip()}"',
                })
                already_cut.add(j)
    return cuts


def _merge_cuts(auto: List[Dict], gpt: List[Dict]) -> List[Dict]:
    """Union of auto and GPT cuts. Where they overlap keep the wider range."""
    all_cuts = auto + gpt
    if not all_cuts:
        return []
    sorted_cuts = sorted(all_cuts, key=lambda c: float(c.get("remove_start", 0)))
    merged: List[Dict] = [dict(sorted_cuts[0])]
    for cut in sorted_cuts[1:]:
        rs = float(cut.get("remove_start", 0))
        re = float(cut.get("remove_end", 0))
        if rs <= float(merged[-1]["remove_end"]):
            merged[-1]["remove_end"] = max(float(merged[-1]["remove_end"]), re)
        else:
            merged.append(dict(cut))
    return merged


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

    # Merge segments shorter than _MIN_SEG_DUR — a 1-2s segment creates a jarring cut.
    # First pass: merge short trailing segments backward.
    merged: List[Dict[str, float]] = []
    for seg in keep:
        if merged and (seg["src_end"] - seg["src_start"]) < _MIN_SEG_DUR:
            merged[-1] = {"src_start": merged[-1]["src_start"], "src_end": seg["src_end"]}
        else:
            merged.append(seg)
    # Second pass: merge short leading segment forward (edge case: first seg is tiny).
    if len(merged) > 1 and (merged[0]["src_end"] - merged[0]["src_start"]) < _MIN_SEG_DUR:
        merged[1] = {"src_start": merged[0]["src_start"], "src_end": merged[1]["src_end"]}
        merged = merged[1:]
    return merged if merged else keep


def _srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _original_to_timeline(
    t: float,
    keep_segments: List[Dict[str, float]],
    clamp: bool = False,
    transition_dur: float = 0.0,
) -> Optional[float]:
    """Map a timestamp in the original video to its position in the output timeline.
    transition_dur: when Cloudinary overlaps adjacent segments by this amount, each
    subsequent segment starts earlier — pass the same value used in _build_cloudinary_cut_url.
    clamp: if t is in a cut, return the start of the next kept segment instead of None."""
    offset = 0.0
    for i, seg in enumerate(keep_segments):
        seg_dur = seg["src_end"] - seg["src_start"]
        if seg["src_start"] <= t <= seg["src_end"]:
            return offset + (t - seg["src_start"])
        if clamp and t < seg["src_start"]:
            return offset
        overlap = transition_dur if i < len(keep_segments) - 1 else 0.0
        offset += seg_dur - overlap
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
    cloudinary_cut_url: str = "",   # pre-cut + transitioned video from Cloudinary
    transition_dur: float = 0.0,    # overlap per cut used by Cloudinary (for SRT timing)
    logo_url: str = "",             # brand logo; slides in over the last 3s
    brand_name: str = "",           # brand name for lower-third and outro
    primary_color: str = "#FFD700", # caption active-word background + hook accent
    secondary_color: str = "#000000",
    tagline: str = "",              # tagline shown on outro card
    website: str = "",              # website shown on outro card
    caption_cues: Optional[List[Dict[str, Any]]] = None,  # time windows for styled captions
    topic_changes: Optional[List[Dict[str, Any]]] = None, # topic shift timestamps for transitions
    video_type: str = "founder",                          # drives flash vs. swipe style
    icon_overlays: Optional[List[Dict[str, Any]]] = None, # resolved: [{at, duration, category, html, ...}]
    transition_style: str = "auto",  # overrides video_type-based flash/swipe selection
) -> Dict[str, Any]:
    # broll items have: at (original ts), duration, url (resolved)
    keep_segments = _build_keep_segments(cuts, video_duration)
    raw_total = sum(s["src_end"] - s["src_start"] for s in keep_segments)
    # Cloudinary transitions overlap adjacent segments, reducing total duration
    total_duration = raw_total - max(0, len(keep_segments) - 1) * transition_dur

    # ── Video track ───────────────────────────────────────────────────────────
    video_clips: List[Dict] = []

    if cloudinary_cut_url:
        # Cloudinary pre-rendered the cuts + transitions — Shotstack gets ONE clip.
        # Apply a global slow zoom so the full video has forward motion.
        video_clips = [{
            "asset": {"type": "video", "src": cloudinary_cut_url, "volume": 1},
            "start": 0,
            "length": round(total_duration, 3),
            "fit": "cover",
            "effect": "zoomInSlow",
        }]
        print(f"[VideoProduction] using Cloudinary pre-cut URL ({len(keep_segments)} segments)", flush=True)
    else:
        # Fallback: multi-clip Shotstack timeline with per-segment Ken Burns + skew
        timeline_pos = 0.0
        for i, seg in enumerate(keep_segments):
            seg_dur = seg["src_end"] - seg["src_start"]
            if seg_dur < 0.1:
                continue

            seg_zooms = [z for z in zooms if seg["src_start"] <= float(z.get("at", -1)) < seg["src_end"]]
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

            if i > 0:
                clip["transition"] = {"in": "zoom"}

            if seg_zooms:
                z = seg_zooms[0]
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

    # ── Caption track — 4 style types ────────────────────────────────────────
    # Each SRT entry is routed to exactly one type (standard / emphasis / metric / cta).
    # Each type gets its own SRT file + rich-caption clip with distinct styling.
    # All clips run for the full video duration; only entries in their SRT are shown.
    _cues = caption_cues or []
    _MAX_CAPTION_WORDS = 5
    _CAP_TYPES = ("standard", "emphasis", "metric", "cta")
    type_srt_lines: Dict[str, List[str]] = {t: [] for t in _CAP_TYPES}
    type_entry_num: Dict[str, int]       = {t: 1    for t in _CAP_TYPES}

    for entry in srt_entries:
        tl_start = _original_to_timeline(entry["start"], keep_segments, transition_dur=transition_dur)
        tl_end   = _original_to_timeline(entry["end"],   keep_segments, clamp=True, transition_dur=transition_dur)
        if tl_start is None or tl_end is None:
            continue
        tl_start = max(0.0, tl_start)
        tl_end   = min(total_duration, tl_end)
        entry_dur = tl_end - tl_start
        if entry_dur < 0.1:
            continue

        cap_type = _get_caption_type(entry["start"], _cues, entry["text"])

        words = entry["text"].split()
        if len(words) <= _MAX_CAPTION_WORDS:
            n = type_entry_num[cap_type]
            type_srt_lines[cap_type] += [
                str(n), f"{_srt_time(tl_start)} --> {_srt_time(tl_end)}", entry["text"], ""
            ]
            type_entry_num[cap_type] += 1
        else:
            chunks = [words[j:j + _MAX_CAPTION_WORDS] for j in range(0, len(words), _MAX_CAPTION_WORDS)]
            per_word_dur = entry_dur / len(words)
            chunk_start  = tl_start
            for chunk in chunks:
                chunk_dur = per_word_dur * len(chunk)
                chunk_end = min(tl_end, round(chunk_start + chunk_dur, 3))
                if chunk_end - chunk_start < 0.05:
                    continue
                n = type_entry_num[cap_type]
                type_srt_lines[cap_type] += [
                    str(n), f"{_srt_time(chunk_start)} --> {_srt_time(chunk_end)}", " ".join(chunk), ""
                ]
                type_entry_num[cap_type] += 1
                chunk_start = chunk_end

    srt_dir = "/app/static/srt"
    os.makedirs(srt_dir, exist_ok=True)

    # Per-type Shotstack rich-caption styling
    _cap_style_map: Dict[str, Dict] = {
        "standard": {
            "font":    {"family": "Montserrat", "size": 46, "color": "#ffffff", "weight": 800},
            "stroke":  {"width": 2, "color": "#000000", "opacity": 1},
            "active":  {"font": {"color": secondary_color, "background": primary_color},
                        "stroke": {"width": 0, "color": "#000000", "opacity": 0}},
            "width":   580, "height": 220, "y": 0.07,
        },
        "emphasis": {
            # Bigger, heavier — reserved for high-strength emphasis moments
            "font":    {"family": "Montserrat", "size": 58, "color": "#ffffff", "weight": 900},
            "stroke":  {"width": 3, "color": "#000000", "opacity": 1},
            "active":  {"font": {"color": "#ffffff", "background": primary_color},
                        "stroke": {"width": 0, "color": "#000000", "opacity": 0}},
            "width":   640, "height": 260, "y": 0.09,
        },
        "metric": {
            # Gold text so numbers/stats stand out visually
            "font":    {"family": "Montserrat", "size": 52, "color": "#FFE566", "weight": 900},
            "stroke":  {"width": 2, "color": "#000000", "opacity": 1},
            "active":  {"font": {"color": "#000000", "background": "#FFE566"},
                        "stroke": {"width": 0, "color": "#000000", "opacity": 0}},
            "width":   580, "height": 220, "y": 0.07,
        },
        "cta": {
            # Slightly higher position + brand accent to feel like a call-to-action
            "font":    {"family": "Montserrat", "size": 44, "color": "#ffffff", "weight": 800},
            "stroke":  {"width": 2, "color": "#000000", "opacity": 0.8},
            "active":  {"font": {"color": "#ffffff", "background": primary_color},
                        "stroke": {"width": 0, "color": "#000000", "opacity": 0}},
            "width":   580, "height": 220, "y": 0.12,
        },
    }

    caption_clips: List[Dict] = []
    total_cap_entries = 0
    for cap_type in _CAP_TYPES:
        lines = type_srt_lines[cap_type]
        if not lines:
            continue
        n_entries = type_entry_num[cap_type] - 1
        total_cap_entries += n_entries
        srt_filename = f"{job_id or 'job'}_{cap_type}.srt"
        with open(f"{srt_dir}/{srt_filename}", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        srt_url = f"https://api-staging.urisocial.com/static/srt/{srt_filename}"
        print(
            f"[VideoProduction] caption:{cap_type} {n_entries} entries → {srt_url}",
            flush=True,
        )
        st = _cap_style_map[cap_type]
        caption_clips.append({
            "asset": {
                "type":      "rich-caption",
                "src":       srt_url,
                "font":      st["font"],
                "stroke":    st["stroke"],
                "animation": {"style": "karaoke"},
                "active":    st["active"],
                "style":     {"textTransform": "uppercase"},
                "padding":   {"top": 6, "right": 20, "bottom": 6, "left": 20},
            },
            "start":    0,
            "length":   "end",
            "width":    st["width"],
            "height":   st["height"],
            "position": "bottom",
            "offset":   {"x": 0, "y": st["y"]},
        })
    print(
        f"[VideoProduction] captions: {total_cap_entries} total entries "
        f"across {len(caption_clips)} style track(s)",
        flush=True,
    )

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
            tl_at = _original_to_timeline(orig_at, keep_segments, transition_dur=transition_dur)
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

    # ── Topic-change transition overlays ─────────────────────────────────────
    # transition_style "none" disables overlays entirely.
    # "flash" / "swipe" force that style regardless of video_type.
    # "auto", "circle_wipe", "diagonal_wipe", "hard_cut" fall back to video_type lookup.
    transition_overlay_clips: List[Dict] = []
    if transition_style == "none":
        _tc_style = None
    elif transition_style == "flash":
        _tc_style = {"type": "flash", "color": "#ffffff", "duration": 0.10, "opacity": 0.65}
    elif transition_style == "swipe":
        _tc_style = {"type": "swipe", "color": None, "duration": 0.20, "opacity": 0.55}
    else:
        _tc_style = _TRANSITION_STYLE_BY_VIDEO_TYPE.get(video_type, _DEFAULT_TRANSITION_STYLE)

    if _tc_style is not None:
      _tc_color  = _tc_style["color"] or primary_color
      _tc_dur    = float(_tc_style["duration"])
      _tc_op     = float(_tc_style["opacity"])
      _tc_type   = _tc_style["type"]  # "flash" | "swipe"

    for change in (topic_changes or []) if _tc_style is not None else []:
        orig_at = float(change.get("at", -1))
        conf    = float(change.get("confidence", 1.0))
        if orig_at < 0 or orig_at >= video_duration or conf < _CONFIDENCE_THRESHOLD:
            continue
        tl_at = _original_to_timeline(orig_at, keep_segments, transition_dur=transition_dur)
        if tl_at is None:
            continue
        clip_start  = max(0.0, round(tl_at - _tc_dur / 2, 3))
        actual_dur  = min(_tc_dur, total_duration - clip_start)
        if actual_dur < 0.05:
            continue
        if _tc_type == "swipe":
            tr_in, tr_out = "slideLeft", "slideRight"
        else:
            tr_in, tr_out = "fade", "fade"
        transition_overlay_clips.append({
            "asset": {
                "type":   "html",
                "html":   f"<div style='width:100%;height:100%;background:{_tc_color};'></div>",
                "width":  1080,
                "height": 1920,
            },
            "start":      clip_start,
            "length":     round(actual_dur, 3),
            "opacity":    _tc_op,
            "transition": {"in": tr_in, "out": tr_out},
            "position":   "fill",
        })
        print(
            f"[VideoProduction] topic-change {_tc_type} at orig={orig_at:.1f}s → tl={tl_at:.1f}s",
            flush=True,
        )

    # ── Icon overlay clips (emoji / Lottie) ──────────────────────────────────
    icon_clips: List[Dict] = []
    for ov in (icon_overlays or []):
        html = ov.get("html", "")
        if not html:
            continue
        orig_at = float(ov.get("at", -1))
        ov_dur  = float(ov.get("duration", 1.5))
        if orig_at < 0 or orig_at >= video_duration:
            continue
        tl_at = _original_to_timeline(orig_at, keep_segments, transition_dur=transition_dur)
        if tl_at is None:
            continue
        actual_dur = min(ov_dur, total_duration - tl_at)
        if actual_dur < 0.3:
            continue
        category = ov.get("category", "star")
        cfg      = _ICON_OVERLAY_LIBRARY.get(category, _ICON_OVERLAY_LIBRARY["star"])
        size     = int(cfg["size"])
        position = cfg.get("position", "topRight")
        icon_clips.append({
            "asset": {
                "type":   "html",
                "html":   html,
                "width":  size,
                "height": size,
            },
            "start":      round(tl_at, 3),
            "length":     round(actual_dur, 3),
            "position":   position,
            "offset":     {"x": -0.04, "y": -0.06} if "Right" in position else {"x": 0.0, "y": 0.15},
            "opacity":    0.92,
            "transition": {"in": "fade", "out": "fade"},
        })
        print(
            f"[VideoProduction] icon {category} ({cfg['emoji']}) at orig={orig_at:.1f}s → tl={tl_at:.1f}s",
            flush=True,
        )

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
        tl_at = _original_to_timeline(orig_at, keep_segments, transition_dur=transition_dur)
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
        fade = min(2.5, total_duration * 0.10)
        music_clips = [{
            "asset": {
                "type": "audio",
                "src": music_url,
                "volume": 0.08,   # low enough to never compete with speech
                "trim": 0,
            },
            "start": 0,
            "length": round(total_duration, 3),
            "transition": {"in": "fade", "out": "fade"},  # smooth fade at both ends
        }]
        print(f"[Music] added track volume=0.08 length={total_duration:.1f}s fade={fade:.1f}s", flush=True)

    # ── Hook title card (rich-text overlay, first 2.5s) ──────────────────────
    # Shotstack's renderer collapses multi-element stacking (both <br> and separate
    # <p> elements render at the same y). Single element only — scale font to fit.
    hook_clips: List[Dict] = []
    if hook_text and not cloudinary_cut_url:
        _hook_upper = hook_text.upper()
        # Scale font so text stays within ~1000px at ~0.60 width-per-em for Montserrat Black.
        # Clamp 40–62px so it's always readable.
        _font = min(62, max(40, int(1000 / max(len(_hook_upper) * 0.60, 1))))
        _shadow = (
            f"3px 3px 0 {primary_color},-3px -3px 0 {primary_color},"
            f"3px -3px 0 {primary_color},-3px 3px 0 {primary_color}"
        )
        hook_css = (
            "body{margin:0;padding:0;background:transparent;}"
            f"p{{font-family:'Montserrat',sans-serif;font-size:{_font}px;font-weight:900;"
            f"color:#FFFFFF;text-align:center;text-transform:uppercase;"
            f"letter-spacing:-2px;white-space:nowrap;"
            f"text-shadow:{_shadow};margin:0;padding:8px 20px;}}"
        )
        hook_clips = [{
            "asset": {
                "type": "html",
                "html": f"<p>{_hook_upper}</p>",
                "css": hook_css,
                "width": 1060,
                "height": 180,
            },
            "start": 0,
            "length": round(min(2.5, total_duration * 0.25), 3),
            "position": "center",
            "offset": {"y": 0.10},
            "transition": {"in": "slideUp", "out": "fade"},
        }]
        print(f"[VideoProduction] hook '{_hook_upper}' font={_font}px", flush=True)

    # ── Lower-third brand name (slides up at start, shown for 3.5s) ──────────
    lower_third_clips: List[Dict] = []
    if brand_name:
        lt_dur = min(3.5, total_duration * 0.20)
        # The BODY itself is the pill — no inner div needed.
        # Canvas width is calculated to match the text so the pill can't be wider than the text.
        # ~26px per uppercase char at 32px Montserrat + 64px padding.
        lt_canvas_w = max(180, min(560, len(brand_name) * 26 + 80))
        lt_css = (
            f"body{{margin:0;padding:12px 32px;background:{primary_color};}}"
            f"p{{font-family:'Montserrat',sans-serif;font-size:32px;font-weight:700;"
            f"color:{secondary_color};text-transform:uppercase;letter-spacing:3px;"
            f"margin:0;white-space:nowrap;text-align:center;}}"
        )
        lower_third_clips = [{
            "asset": {
                "type": "html",
                "html": f"<p>{brand_name}</p>",
                "css": lt_css,
                "width": lt_canvas_w,
                "height": 76,
            },
            "start": 0,
            "length": round(lt_dur, 3),
            "position": "bottomLeft",
            "offset": {"x": 0.02, "y": 0.18},
            "transition": {"in": "slideUp", "out": "fade"},
        }]
        print(f"[VideoProduction] lower-third: '{brand_name}' w={lt_canvas_w}px dur={lt_dur:.1f}s", flush=True)

    # ── Outro card (last 3s — brand color bg + logo + tagline + website) ─────
    outro_clips: List[Dict] = []
    if total_duration > 5.0 and (brand_name or logo_url or tagline or website):
        outro_dur = min(3.0, total_duration * 0.15)
        outro_start = round(max(0.0, total_duration - outro_dur), 3)
        tagline_line = f"<p class='tag'>{tagline}</p>" if tagline else ""
        website_line = f"<p class='web'>{website}</p>" if website else ""
        name_line    = f"<p class='name'>{brand_name}</p>" if brand_name else ""
        outro_css = (
            f"body{{margin:0;padding:0;background:{primary_color};}}"
            f"div{{display:flex;flex-direction:column;align-items:center;"
            f"justify-content:center;width:100%;height:100%;padding:40px 60px;box-sizing:border-box;}}"
            f".name{{font-family:'Montserrat',sans-serif;font-size:56px;font-weight:900;"
            f"color:{secondary_color};text-transform:uppercase;letter-spacing:-1px;margin:0 0 12px;}}"
            f".tag{{font-family:'Montserrat',sans-serif;font-size:32px;font-weight:400;"
            f"color:{secondary_color};opacity:0.85;margin:0 0 8px;text-align:center;}}"
            f".web{{font-family:'Montserrat',sans-serif;font-size:26px;font-weight:600;"
            f"color:{secondary_color};opacity:0.7;margin:0;}}"
        )
        outro_html_clip: Dict = {
            "asset": {
                "type": "html",
                "html": f"<div>{name_line}{tagline_line}{website_line}</div>",
                "css": outro_css,
                "width": 1080,
                "height": 1920,
            },
            "start": outro_start,
            "length": round(outro_dur, 3),
            "position": "center",
            "transition": {"in": "fade", "out": "fade"},
        }
        outro_clips.append(outro_html_clip)
        if logo_url:
            outro_clips.append({
                "asset": {"type": "image", "src": logo_url},
                "start": outro_start,
                "length": round(outro_dur, 3),
                "fit": "contain",
                "scale": 0.30,
                "position": "center",
                "offset": {"y": 0.25},
                "transition": {"in": "slideDown", "out": "fade"},
            })
        print(
            f"[VideoProduction] outro card: start={outro_start}s dur={outro_dur}s "
            f"brand='{brand_name}'",
            flush=True,
        )

    # ── Brand logo overlay — slides in from bottom-left for the last 3s ─────────
    logo_clips: List[Dict] = []
    if logo_url and not outro_clips:
        # Show logo overlay only when there's no full outro card
        logo_dur = min(3.0, total_duration * 0.15)
        logo_start = round(max(0.0, total_duration - logo_dur), 3)
        logo_clips = [{
            "asset": {"type": "image", "src": logo_url},
            "start": logo_start,
            "length": round(logo_dur, 3),
            "fit": "contain",
            "scale": 0.25,
            "position": "bottomLeft",
            "offset": {"x": 0.03, "y": 0.05},
            "transition": {"in": "slideRight", "out": "fade"},
        }]
        print(f"[VideoProduction] logo overlay: start={logo_start}s dur={logo_dur}s", flush=True)

    # Track order (index 0 = top layer):
    # 0: hook  1: outro  2: lower-third  3: logo  4: icons  5: transitions
    # 6: captions  7: b-roll  8: main video  9: sfx  10: music
    tracks: List[Dict] = []
    if hook_clips:
        tracks.append({"clips": hook_clips})
    if outro_clips:
        tracks.append({"clips": outro_clips})
    if lower_third_clips:
        tracks.append({"clips": lower_third_clips})
    if logo_clips:
        tracks.append({"clips": logo_clips})
    if icon_clips:
        tracks.append({"clips": icon_clips})
    if transition_overlay_clips:
        tracks.append({"clips": transition_overlay_clips})
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
            "quality": "high",
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
    enable_music: bool = True,
    enable_sfx: bool = True,
    transition_style: str = "auto",  # auto | circle_wipe | diagonal_wipe | flash | swipe | hard_cut | none
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

        # ── Load brand profile for branding overlays ──────────────────────────────
        job_doc = await db.video_production_jobs.find_one({"job_id": job_id}, {"user_id": 1})
        user_id = (job_doc or {}).get("user_id", "")
        logo_url    = ""
        brand_name  = ""
        brand_colors: List[str] = []
        tagline     = ""
        website     = ""
        if user_id:
            bp = await db.brand_profiles.find_one({"user_id": user_id}, {
                "logo_url": 1, "brand_name": 1, "brand_colors": 1,
                "tagline": 1, "website": 1,
            })
            if bp:
                logo_url    = (bp.get("logo_url") or "").strip()
                brand_name  = (bp.get("brand_name") or "").strip()
                brand_colors = [c.strip() for c in (bp.get("brand_colors") or []) if c]
                tagline     = (bp.get("tagline") or "").strip()
                website     = (bp.get("website") or "").strip()
        primary_color   = brand_colors[0] if brand_colors else "#FFD700"
        secondary_color = brand_colors[1] if len(brand_colors) > 1 else "#000000"
        print(
            f"[VideoProduction] brand={brand_name or 'none'} "
            f"colors={brand_colors[:2]} logo={bool(logo_url)}",
            flush=True,
        )

        # ── Stage 1: Reap — word-level transcript + timestamps + clip detection ──
        await update(5, "Transcribing…")
        upload_id = await reap.upload_video(video_bytes, f"{job_id}_raw.mp4")
        if not upload_id:
            raise RuntimeError("Upload to Reap failed")

        trans_id = await reap.start_transcription(upload_id, "en")
        if not trans_id:
            raise RuntimeError("Transcription failed to start")

        srt_text, _reap_video_url, tracking_data = await reap.fetch_full_transcript_data(
            trans_id, timeout_seconds=600
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

        # Step 1: always write to static server (reliable, no size limit).
        # Step 2: tell Cloudinary to fetch from that URL — avoids direct byte upload
        #         and Cloudinary's 100MB per-request limit entirely.
        await update(38, "Uploading clean video…")
        video_static_dir = "/app/static/videos"
        os.makedirs(video_static_dir, exist_ok=True)
        static_url = ""
        try:
            with open(f"{video_static_dir}/{job_id}.mp4", "wb") as vf:
                vf.write(cleaned_bytes)
            static_url = f"https://api-staging.urisocial.com/static/videos/{job_id}.mp4"
            print(f"[VideoProduction] static: {len(cleaned_bytes)//1024}KB → {static_url}", flush=True)
        except Exception as e:
            print(f"[VideoProduction] static write failed: {e}", flush=True)

        # Now try to register on Cloudinary via URL fetch (no byte transfer).
        clean_video_url = None
        if static_url:
            clean_video_url = await _cloudinary_fetch_url(static_url, job_id)
        # Fallback chain: Cloudinary → static → Reap source
        if not clean_video_url:
            clean_video_url = static_url or _reap_video_url

        if not clean_video_url:
            raise RuntimeError("Could not obtain a video URL for rendering")

        print(f"[VideoProduction] render source={clean_video_url[:80]}…", flush=True)

        # ── Stage 3: Content Analysis (Phase 1) + Editing Rules (Phase 2) ────────
        await update(48, "AI analyzing content…")
        srt_entries = _parse_srt(srt_text)

        analysis  = await analyze_content(srt_text, video_type, duration, tracking_data)
        decisions = apply_editing_rules(analysis, duration, enable_sfx=enable_sfx)

        gpt_cuts        = decisions["cuts"]
        zooms           = decisions["zooms"]
        sound_effects   = decisions["sound_effects"]
        broll_decisions = decisions["broll"]
        hook_text       = decisions["hook_text"]
        music_mood      = decisions["music_mood"]
        pacing_note     = decisions["pacing_note"]
        topic_changes   = decisions.get("topic_changes", [])
        caption_cues    = decisions.get("caption_cues", [])
        icon_overlays   = decisions.get("icon_overlays", [])

        # Algorithmic cuts — word-level when Reap provides timestamps, SRT fallback otherwise.
        words = _extract_words(tracking_data)
        if words:
            auto_cuts   = _auto_cuts_from_words(words, duration, video_type)
            filler_cuts = _filler_cuts_from_words(words)
            rep_cuts    = _repetition_cuts_from_words(words)
        else:
            auto_cuts   = _auto_cuts_from_srt(srt_entries, duration, video_type)
            filler_cuts = _filler_cuts_from_srt(srt_entries)
            rep_cuts    = _repetition_cuts_from_srt(srt_entries)
        cuts = _merge_cuts(auto_cuts + filler_cuts + rep_cuts, gpt_cuts)
        print(
            f"[VideoProduction] mode={'word' if words else 'srt'} words={len(words)} "
            f"silence={len(auto_cuts)} filler={len(filler_cuts)} "
            f"repetition={len(rep_cuts)} gpt={len(gpt_cuts)} merged={len(cuts)} "
            f"zooms={len(zooms)} sfx={len(sound_effects)} broll={len(broll_decisions)} "
            f"pacing={pacing_note} hook='{hook_text}'",
            flush=True,
        )

        # ── REVIEW PAUSE — store decisions + render context, wait for user approval ─
        await db.video_production_jobs.update_one(
            {"job_id": job_id},
            {"$set": {
                "status": "awaiting_review",
                "status_message": "Review AI decisions before rendering",
                "progress": 55,
                "ai_decisions": {
                    "cuts":           cuts,
                    "zooms":          zooms,
                    "sound_effects":  sound_effects,
                    "broll":          broll_decisions,
                    "hook_text":      hook_text,
                    "music_mood":     music_mood,
                    "pacing_note":    pacing_note,
                    "topic_changes":  topic_changes,
                    "caption_cues":   caption_cues,
                    "icon_overlays":  icon_overlays,
                },
                "render_context": {
                    "static_url": static_url,
                    "cloudinary_url": clean_video_url,
                    "srt_text": srt_text,
                    "duration": duration,
                    "video_type": video_type,
                    "logo_url": logo_url,
                    "brand_name": brand_name,
                    "primary_color": primary_color,
                    "secondary_color": secondary_color,
                    "tagline": tagline,
                    "website": website,
                    "enable_music": enable_music,
                    "transition_style": transition_style,
                },
            }}
        )
        print(
            f"[VideoProduction] job={job_id} awaiting_review — "
            f"{len(cuts)} cuts {len(zooms)} zooms {len(sound_effects)} sfx "
            f"{len(icon_overlays)} icons hook='{hook_text}'",
            flush=True,
        )
        return  # pipeline resumes via POST /produce-video-job/{id}/start-render

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

        # ── Stage 4b: Pick background music from Cloudinary library ─────────────
        music_url = ""
        if enable_music:
            await update(59, "Selecting background music…")
            music_url = _pick_music_url(music_mood)

        # ── Stage 5: Shotstack render + mix ──────────────────────────────────────
        await update(62, "Building edit timeline…")

        # Build Cloudinary cut URL — cuts + transitions in a single CDN-served video.
        # Shotstack then receives ONE clip and only handles captions, hook, music, SFX.
        luma_pid     = _LUMA_MATTE_BY_TYPE.get(video_type, "uri-transitions/circle-wipe")
        transition_dur = _TRANSITION_DUR_BY_TYPE.get(video_type, _CLD_TRANSITION_DUR)
        cloudinary_cut_url = ""
        if "res.cloudinary.com" in (clean_video_url or ""):
            cld_pid = _cloudinary_public_id(clean_video_url)
            if cld_pid:
                keep_segs_preview = _build_keep_segments(cuts, duration)
                if len(keep_segs_preview) > 1:
                    cloudinary_cut_url = _build_cloudinary_cut_url(
                        cld_pid, keep_segs_preview, luma_pid, transition_dur,
                        hook_text=hook_text,
                        primary_color=primary_color,
                    )
                    # Compute where each cut appears in the OUTPUT video.
                    # With custom luma-matte transitions, total duration = sum(seg_durs).
                    # (Transitions are overlaid, not additive — cuts happen at segment ends.)
                    _cum = 0.0
                    _marks = []
                    for _seg in keep_segs_preview[:-1]:
                        _cum += _seg["src_end"] - _seg["src_start"]
                        _marks.append(f"{int(_cum // 60)}:{int(_cum % 60):02d}")
                    print(
                        f"[CloudinaryEdit] {len(keep_segs_preview)} segments → "
                        f"luma-matte {luma_pid.split('/')[-1]} ({transition_dur}s) | "
                        f"cuts at {', '.join(_marks)} in output | "
                        f"{cloudinary_cut_url[:70]}…",
                        flush=True,
                    )

        timeline = build_shotstack_timeline(
            video_url=clean_video_url,      # cleaned voice track (fallback path)
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
            cloudinary_cut_url=cloudinary_cut_url,
            transition_dur=transition_dur if cloudinary_cut_url else 0.0,
            logo_url=logo_url,
            brand_name=brand_name,
            primary_color=primary_color,
            secondary_color=secondary_color,
            tagline=tagline,
            website=website,
            caption_cues=decisions.get("caption_cues", []),
            topic_changes=decisions.get("topic_changes", []),
            video_type=video_type,
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


async def run_render_phase(job_id: str, db) -> None:
    """
    Resume a production job from the render phase.
    Reads ai_decisions + render_context saved at the review pause point.
    Called after the user approves (or modifies) decisions via /start-render.
    """
    shotstack = ShotstackProvider()

    async def update(progress: int, message: str, status: str = "processing", **extra):
        doc: Dict[str, Any] = {"progress": progress, "status_message": message, "status": status}
        doc.update(extra)
        await db.video_production_jobs.update_one({"job_id": job_id}, {"$set": doc})
        print(f"[VideoProduction:render] job={job_id} {progress}% {message}", flush=True)

    try:
        job_doc = await db.video_production_jobs.find_one({"job_id": job_id})
        if not job_doc:
            raise RuntimeError("Job not found")

        decisions       = job_doc.get("ai_decisions", {})
        ctx             = job_doc.get("render_context", {})

        cuts            = decisions.get("cuts", [])
        zooms           = decisions.get("zooms", [])
        sound_effects   = decisions.get("sound_effects", [])
        broll_decisions = decisions.get("broll", [])
        hook_text       = decisions.get("hook_text", "")
        music_mood      = decisions.get("music_mood", "upbeat")
        pacing_note     = decisions.get("pacing_note", "")
        caption_cues    = decisions.get("caption_cues", [])
        topic_changes   = decisions.get("topic_changes", [])
        icon_overlays   = decisions.get("icon_overlays", [])

        clean_video_url   = ctx.get("cloudinary_url") or ctx.get("static_url", "")
        srt_text          = ctx.get("srt_text", "")
        duration          = float(ctx.get("duration", 120.0))
        video_type        = ctx.get("video_type", "founder")
        enable_music      = ctx.get("enable_music", True)
        transition_style  = ctx.get("transition_style", "auto")
        logo_url        = ctx.get("logo_url", "")
        brand_name      = ctx.get("brand_name", "")
        primary_color   = ctx.get("primary_color", "#FFD700")
        secondary_color = ctx.get("secondary_color", "#000000")
        tagline         = ctx.get("tagline", "")
        website         = ctx.get("website", "")

        srt_entries = _parse_srt(srt_text)

        # ── Stage 4a: B-roll ──────────────────────────────────────────────────────
        broll: List[Dict] = []
        if broll_decisions:
            await update(58, "Fetching b-roll assets…")
            tasks = [
                _fetch_broll_url(br.get("description", ""), br.get("concept", ""))
                for br in broll_decisions
            ]
            urls = await asyncio.gather(*tasks, return_exceptions=True)
            for br, url in zip(broll_decisions, urls):
                if isinstance(url, str) and url:
                    broll.append({**br, "url": url})
            print(f"[VideoProduction:render] broll {len(broll)}/{len(broll_decisions)}", flush=True)

        # ── Stage 4b: Music ───────────────────────────────────────────────────────
        music_url = ""
        if enable_music:
            await update(62, "Selecting background music…")
            music_url = _pick_music_url(music_mood)

        # ── Stage 5: Build Cloudinary cut URL + Shotstack timeline ───────────────
        await update(65, "Building edit timeline…")

        # Resolve luma-matte PID from transition_style
        _LUMA_BY_STYLE: Dict[str, Optional[str]] = {
            "auto":          _LUMA_MATTE_BY_TYPE.get(video_type, "uri-transitions/circle-wipe"),
            "circle_wipe":   "uri-transitions/circle-wipe",
            "diagonal_wipe": "uri-transitions/diagonal-wipe",
            "flash":         None,   # hard cut; flash overlay handled by Shotstack
            "swipe":         None,   # hard cut; swipe overlay handled by Shotstack
            "hard_cut":      None,
            "none":          None,
        }
        luma_pid       = _LUMA_BY_STYLE.get(transition_style, _LUMA_MATTE_BY_TYPE.get(video_type, "uri-transitions/circle-wipe"))
        transition_dur = _TRANSITION_DUR_BY_TYPE.get(video_type, _CLD_TRANSITION_DUR) if luma_pid else 0.0

        cloudinary_cut_url = ""
        if "res.cloudinary.com" in (clean_video_url or ""):
            cld_pid = _cloudinary_public_id(clean_video_url)
            if cld_pid:
                keep_segs = _build_keep_segments(cuts, duration)
                if len(keep_segs) > 1:
                    cloudinary_cut_url = _build_cloudinary_cut_url(
                        cld_pid, keep_segs, luma_pid, transition_dur,
                        hook_text=hook_text,
                        primary_color=primary_color,
                    )

        # ── Resolve icon overlays — fetch Lottie JSON (or build emoji HTML) ────────
        resolved_icon_overlays: List[Dict[str, Any]] = []
        if icon_overlays:
            await update(66, "Resolving icon overlays…")
            html_tasks = [_resolve_icon_html(ov["category"]) for ov in icon_overlays]
            html_results = await asyncio.gather(*html_tasks, return_exceptions=True)
            for ov, html in zip(icon_overlays, html_results):
                if isinstance(html, str) and html:
                    resolved_icon_overlays.append({**ov, "html": html})
            print(
                f"[VideoProduction:render] icon_overlays resolved "
                f"{len(resolved_icon_overlays)}/{len(icon_overlays)}",
                flush=True,
            )

        timeline = build_shotstack_timeline(
            video_url=clean_video_url,
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
            cloudinary_cut_url=cloudinary_cut_url,
            transition_dur=transition_dur if cloudinary_cut_url else 0.0,
            logo_url=logo_url,
            brand_name=brand_name,
            primary_color=primary_color,
            secondary_color=secondary_color,
            tagline=tagline,
            website=website,
            caption_cues=caption_cues,
            topic_changes=topic_changes,
            video_type=video_type,
            icon_overlays=resolved_icon_overlays,
            transition_style=transition_style,
        )

        await update(68, "Rendering video…")
        render_id = await shotstack.render(timeline)
        print(f"[VideoProduction:render] render_id={render_id}", flush=True)

        progress_steps = [70, 75, 80, 85, 88, 90, 92, 94, 96, 97, 98]
        step_i = 0
        for _ in range(90):
            await asyncio.sleep(10)
            render_status, render_url = await shotstack.get_render(render_id)
            print(f"[VideoProduction:render] status={render_status}", flush=True)

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
        print(f"[VideoProduction:render] FAILED job={job_id}: {exc}", flush=True)
        await db.video_production_jobs.update_one(
            {"job_id": job_id},
            {"$set": {"status": "failed", "status_message": str(exc), "progress": 0}},
        )
