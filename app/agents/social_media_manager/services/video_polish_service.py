"""
Video Polish Service — PRD §3-9
Wraps Reap clipping API. URI Social builds Stage 1 (ingest + quality check);
Reap handles transcription, reframe, face-tracking, captions, and render.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import re as _re

import aiohttp

from app.core.config import settings


# ── SRT / transcript utilities ────────────────────────────────────────────────

def _parse_srt(srt_text: str) -> List[Dict[str, Any]]:
    """Parse SRT text into list of {start, end, text} dicts (times in seconds)."""
    entries: List[Dict[str, Any]] = []
    for block in _re.split(r'\n\n+', srt_text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        m = _re.match(
            r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)',
            lines[1],
        )
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end   = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text  = ' '.join(l.strip() for l in lines[2:] if l.strip())
        entries.append({'start': start, 'end': end, 'text': text})
    return entries


def _extract_transcript(entries: List[Dict[str, Any]], start: float, end: float) -> str:
    return ' '.join(e['text'] for e in entries if e['start'] < end and e['end'] > start)


# ─────────────────────────────────────────────────────────────────────────────
# Style presets — seeded once into video_style_presets collection
# Each maps to Reap API parameters (clipping_api_settings)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_STYLE_PRESETS: List[Dict[str, Any]] = [
    {
        "name": "naija_bold",
        "display_name": "Naija Bold",
        "description": "High-energy, bold captions, fast cuts, tight crop",
        "best_for": "Sales, promos, hype content",
        "energy_level": 5,
        "is_custom": False,
        "brand_id": None,
        "good_for_intents": ["product_promo", "announcement", "hype", "sales"],
        "clipping_api_settings": {
            "genre": "talking",
            "prompt": "Select the highest-energy moments. Fast-paced cuts. Bold, dynamic delivery. Perfect for sales, promos, and hype content.",
            "clipDurations": [[0, 30]],
            "reframeClips": True,
            "exportOrientation": "portrait",
            "exportResolution": 1080,
        },
    },
    {
        "name": "clean_professional",
        "display_name": "Clean Professional",
        "description": "Minimal captions, steady reframe, subtle transitions",
        "best_for": "Services, B2B, announcements",
        "energy_level": 2,
        "is_custom": False,
        "brand_id": None,
        "good_for_intents": ["announcement", "professional", "b2b", "service"],
        "clipping_api_settings": {
            "genre": "talking",
            "prompt": "Select clear, authoritative moments. Steady pacing. Professional tone. Good for services, B2B, and announcements.",
            "clipDurations": [[30, 60]],
            "reframeClips": True,
            "exportOrientation": "portrait",
            "exportResolution": 1080,
        },
    },
    {
        "name": "street_casual",
        "display_name": "Street Casual",
        "description": "Playful captions, relaxed pacing",
        "best_for": "Lifestyle, behind-the-scenes",
        "energy_level": 3,
        "is_custom": False,
        "brand_id": None,
        "good_for_intents": ["lifestyle", "behind_the_scenes", "casual"],
        "clipping_api_settings": {
            "genre": "talking",
            "prompt": "Relaxed, authentic feel. Keep natural moments. Playful energy. Perfect for lifestyle and behind-the-scenes content.",
            "clipDurations": [[30, 60]],
            "reframeClips": True,
            "exportOrientation": "portrait",
            "exportResolution": 1080,
        },
    },
    {
        "name": "storyteller",
        "display_name": "Storyteller",
        "description": "Emphasizes key words, slower pacing",
        "best_for": "Founder stories, testimonials",
        "energy_level": 2,
        "is_custom": False,
        "brand_id": None,
        "good_for_intents": ["story", "testimonial", "founder", "personal"],
        "clipping_api_settings": {
            "genre": "talking",
            "prompt": "Select the most emotionally resonant moments. Emphasize key words and pauses. Slow, thoughtful pacing. Perfect for founder stories and testimonials.",
            "clipDurations": [[30, 60]],
            "reframeClips": True,
            "exportOrientation": "portrait",
            "exportResolution": 1080,
        },
    },
    {
        "name": "product_pop",
        "display_name": "Product Pop",
        "description": "Punchy, product-focused, energetic captions",
        "best_for": "Product reveals, demos",
        "energy_level": 4,
        "is_custom": False,
        "brand_id": None,
        "good_for_intents": ["product_reveal", "demo", "launch"],
        "clipping_api_settings": {
            "genre": "talking",
            "prompt": "Focus on moments where the product is clearly visible and being discussed. Punchy and energetic. Great for product reveals and demos.",
            "clipDurations": [[0, 30]],
            "reframeClips": True,
            "exportOrientation": "portrait",
            "exportResolution": 1080,
        },
    },
    {
        "name": "minimal_clean",
        "display_name": "Minimal Clean",
        "description": "Simple captions, no transitions, clean crop",
        "best_for": "Elegant brands, premium feel",
        "energy_level": 1,
        "is_custom": False,
        "brand_id": None,
        "good_for_intents": ["premium", "luxury", "elegant"],
        "clipping_api_settings": {
            "genre": "talking",
            "prompt": "Select the most composed, elegant moments. Clean and minimal. No filler. Perfect for premium and luxury brand content.",
            "clipDurations": [[30, 60]],
            "reframeClips": True,
            "exportOrientation": "portrait",
            "exportResolution": 1080,
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Provider abstraction — swap Reap for OpusClip/Vizard without touching caller
# ─────────────────────────────────────────────────────────────────────────────

class AbstractClippingProvider(ABC):
    @abstractmethod
    async def upload_video(self, video_bytes: bytes, filename: str) -> str:
        """Upload video bytes; return provider upload ID."""

    @abstractmethod
    async def create_clip_job(
        self, upload_id: str, style_settings: Dict[str, Any], language: str
    ) -> str:
        """Submit clip job; return provider job/project ID."""

    @abstractmethod
    async def get_job_status(self, provider_job_id: str) -> str:
        """Return normalised status: queued | processing | completed | failed."""

    @abstractmethod
    async def get_output_clips(self, provider_job_id: str) -> List[Dict[str, Any]]:
        """Return list of {url, duration, caption_text}."""


class ReapProvider(AbstractClippingProvider):
    """
    Reap REST API wrapper.
    Base URL: https://public.reap.video/api/v1/automation/
    Auth: Authorization: Bearer <REAP_API_KEY>
    Sign up at https://reap.video to get your API key.
    """

    BASE = "https://public.reap.video/api/v1/automation"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.REAP_API_KEY}",
            "Content-Type": "application/json",
        }

    async def upload_video(self, video_bytes: bytes, filename: str) -> str:
        async with aiohttp.ClientSession() as session:
            # Step 1: get presigned S3 upload URL
            async with session.post(
                f"{self.BASE}/get-upload-url",
                headers=self._headers(),
                json={"filename": filename},
            ) as resp:
                if not resp.ok:
                    body = await resp.text()
                    print(f"[Reap] get-upload-url {resp.status}: {body}", flush=True)
                resp.raise_for_status()
                data = await resp.json()
                upload_url: str = data["uploadUrl"]
                upload_id: str = data["id"]

            # Step 2: PUT video bytes to the presigned URL
            print(f"[Reap] S3 PUT — upload_id={upload_id} size={len(video_bytes)} url_prefix={upload_url[:60]}", flush=True)
            async with session.put(
                upload_url,
                data=video_bytes,
                headers={"Content-Type": "video/mp4", "Content-Length": str(len(video_bytes))},
            ) as resp:
                s3_body = await resp.text()
                print(f"[Reap] S3 PUT response: status={resp.status} body={s3_body[:200]}", flush=True)
                if resp.status not in (200, 204):
                    raise RuntimeError(f"Reap S3 upload failed {resp.status}: {s3_body}")

        return upload_id

    async def create_clip_job(
        self, upload_id: str, style_settings: Dict[str, Any], language: str
    ) -> str:
        base_prompt = style_settings.get("prompt", "")
        # Always ask for 3-5 clips so Reap doesn't return just 1
        clip_count_instruction = "Give me exactly 3 to 5 clips."
        prompt = f"{base_prompt} {clip_count_instruction}".strip()
        if language and language != "en":
            prompt = f"[Language context: {language}] " + prompt

        payload = {
            "uploadId": upload_id,
            "genre": style_settings.get("genre", "talking"),
            "exportResolution": style_settings.get("exportResolution", 1080),
            "exportOrientation": style_settings.get("exportOrientation", "portrait"),
            "reframeClips": style_settings.get("reframeClips", True),
            "clipDurations": style_settings.get("clipDurations", [[30, 60]]),
            "prompt": prompt,
            "language": "en",
            "captionsPreset": style_settings.get("captionsPreset", "system_beasty"),
            "enableCaptions": True,
            "enableHighlights": True,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.BASE}/create-clips",
                headers=self._headers(),
                json=payload,
            ) as resp:
                if not resp.ok:
                    body = await resp.text()
                    print(f"[Reap] create-clips {resp.status}: {body}", flush=True)
                resp.raise_for_status()
                data = await resp.json()
                return data["id"]

    async def get_job_status(self, provider_job_id: str) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.BASE}/get-project-status",
                headers=self._headers(),
                params={"projectId": provider_job_id},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                raw = data.get("status", "processing")
                if raw == "invalid":
                    return "invalid_content"
                # Normalise to our vocabulary
                mapping = {
                    "queued": "processing",
                    "processing": "processing",
                    "completed": "completed",
                    "failed": "failed",
                    "invalid": "failed",
                    "expired": "failed",
                }
                return mapping.get(raw, "processing")

    async def get_output_clips(self, provider_job_id: str) -> List[Dict[str, Any]]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.BASE}/get-project-clips",
                headers=self._headers(),
                params={"projectId": provider_job_id},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                clips = data.get("clips", [])
                return [
                    {
                        "clip_url": c.get("clipUrl", ""),
                        "start_time": c.get("startTime", 0),
                        "end_time": c.get("endTime", 0),
                        "duration": c.get("duration", 0),
                        "caption_text": c.get("caption", ""),  # social media caption
                        "title": c.get("title", ""),
                        "topic": c.get("topic", ""),
                        "virality_score": c.get("viralityScore", 0),
                        "hook": c.get("hook", ""),
                        # clipWithCaptionsUrl is deprecated — captions are baked into
                        # clipUrl when captionsPreset is set.
                        "captioned_clip_url": (
                            c.get("clipWithCaptionsUrl")
                            or (c.get("clipUrl", "") if c.get("enableCaptions") else "")
                        ),
                        "transcript": "",  # enriched after transcription completes
                    }
                    for c in clips
                    if c.get("clipUrl")
                ]

    async def start_transcription(self, upload_id: str, language: str = "en") -> str:
        """Start a transcription job for the given upload. Returns Reap project ID."""
        # Reap accepts ISO 639-1 codes only (e.g. "en", not "en-NG")
        lang_code = (language or "en").split("-")[0].split("_")[0]
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.BASE}/create-transcription",
                headers=self._headers(),
                json={"uploadId": upload_id, "language": lang_code},
            ) as resp:
                if not resp.ok:
                    print(f"[Reap] create-transcription {resp.status}: {await resp.text()}", flush=True)
                    return ""
                data = await resp.json()
                tid = data.get("id", "")
                print(f"[Reap] transcription started: {tid}", flush=True)
                return tid

    async def fetch_srt(self, project_id: str, timeout_seconds: int = 300) -> str:
        """Poll transcription project and return raw SRT text when done."""
        srt, _ = await self.fetch_srt_and_source_url(project_id, timeout_seconds)
        return srt

    async def fetch_srt_and_source_url(
        self, project_id: str, timeout_seconds: int = 300
    ) -> tuple[str, str]:
        """Poll transcription project; return (srt_text, source_video_url).
        source_video_url may be empty if Reap doesn't expose it."""
        poll_interval = 10
        for _ in range(max(1, timeout_seconds // poll_interval)):
            await asyncio.sleep(poll_interval)
            status = await self.get_job_status(project_id)
            if status == "completed":
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self.BASE}/get-project-details",
                        headers=self._headers(),
                        params={"projectId": project_id},
                    ) as resp:
                        if not resp.ok:
                            print(f"[Reap] get-project-details {resp.status}", flush=True)
                            return "", ""
                        data = await resp.json()
                    urls = data.get("urls") or {}
                    print(f"[Reap] project-details urls keys: {list(urls.keys())}", flush=True)
                    srt_url = urls.get("transcription_srt", "")
                    # Try common Reap field names for the original source video
                    source_url = (
                        urls.get("videoFile")
                        or urls.get("transcription_source")
                        or urls.get("source")
                        or urls.get("source_video")
                        or urls.get("video")
                        or urls.get("original")
                        or ""
                    )
                    srt_text = ""
                    if srt_url:
                        async with session.get(srt_url) as r:
                            srt_text = await r.text()
                            print(f"[Reap] transcript fetched ({len(srt_text)} chars), source_url={bool(source_url)}", flush=True)
                    return srt_text, source_url
            if status == "failed":
                return "", ""
        return "", ""

    async def fetch_full_transcript_data(
        self, project_id: str, timeout_seconds: int = 300
    ) -> tuple[str, str, dict]:
        """
        Extended transcript fetch for the production pipeline.
        Returns (srt_text, video_url, tracking_data).
        tracking_data is parsed JSON from Reap's trackingData URL — contains
        word-level timestamps and detected silences. Empty dict on failure.
        """
        poll_interval = 10
        for tick in range(max(1, timeout_seconds // poll_interval)):
            await asyncio.sleep(poll_interval)
            status = await self.get_job_status(project_id)
            elapsed = (tick + 1) * poll_interval
            # Log every 30s so we can see live progress in the container logs
            if tick % 3 == 0:
                print(f"[Reap] transcription status={status} elapsed={elapsed}s/{timeout_seconds}s", flush=True)
            if status == "completed":
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self.BASE}/get-project-details",
                        headers=self._headers(),
                        params={"projectId": project_id},
                    ) as resp:
                        if not resp.ok:
                            return "", "", {}
                        data = await resp.json()
                    urls = data.get("urls") or {}
                    print(f"[Reap] project-details keys: {list(urls.keys())}", flush=True)

                    srt_url = urls.get("transcription_srt", "")
                    source_url = (
                        urls.get("videoFile")
                        or urls.get("transcription_source")
                        or urls.get("source")
                        or urls.get("source_video")
                        or urls.get("video")
                        or urls.get("original")
                        or ""
                    )
                    tracking_url = urls.get("trackingData", "")

                    srt_text = ""
                    tracking_data: dict = {}

                    if srt_url:
                        async with session.get(srt_url) as r:
                            srt_text = await r.text()

                    if tracking_url:
                        try:
                            async with session.get(
                                tracking_url,
                                timeout=aiohttp.ClientTimeout(total=15),
                            ) as r:
                                if r.ok:
                                    tracking_data = await r.json(content_type=None)
                                    print(
                                        f"[Reap] trackingData fetched ({len(str(tracking_data))} chars)",
                                        flush=True,
                                    )
                        except Exception as e:
                            print(f"[Reap] trackingData fetch failed: {e}", flush=True)

                    print(
                        f"[Reap] srt={len(srt_text)}ch source={bool(source_url)} tracking={bool(tracking_data)}",
                        flush=True,
                    )
                    return srt_text, source_url, tracking_data
            if status in ("failed", "invalid_content"):
                print(f"[Reap] transcription terminal status={status} after {elapsed}s", flush=True)
                return "", "", {}
        print(f"[Reap] transcription timed out after {timeout_seconds}s", flush=True)
        return "", "", {}

    async def get_caption_presets(self) -> List[Dict[str, Any]]:
        """Return all caption presets from the Reap account (system + user-created)."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.BASE}/get-all-presets",
                headers=self._headers(),
                params={"pageSize": 100},
            ) as resp:
                if not resp.ok:
                    return []
                data = await resp.json()
                return [
                    {"id": p["id"], "name": p["name"], "source": p.get("source", "system")}
                    for p in data.get("presets", [])
                ]

    async def upload_from_url(self, url: str, filename: str) -> str:
        """Download a clip from URL and re-upload to Reap. Returns new upload_id."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                video_bytes = await resp.read()
        return await self.upload_video(video_bytes, filename)

    async def create_reframe(self, upload_id: str, orientation: str = "landscape") -> str:
        """Start a reframe job. Returns Reap project ID."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.BASE}/create-reframe",
                headers=self._headers(),
                json={"uploadId": upload_id, "orientation": orientation, "genre": "talking"},
            ) as resp:
                if not resp.ok:
                    raise RuntimeError(f"create-reframe {resp.status}: {await resp.text()}")
                return (await resp.json())["id"]

    async def create_dubbing(self, upload_id: str, source_lang: str, target_lang: str) -> str:
        """Start a dubbing job. Returns Reap project ID."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.BASE}/create-dubbing",
                headers=self._headers(),
                json={
                    "uploadId": upload_id,
                    "sourceLanguage": source_lang,
                    "targetLanguage": target_lang,
                },
            ) as resp:
                if not resp.ok:
                    raise RuntimeError(f"create-dubbing {resp.status}: {await resp.text()}")
                return (await resp.json())["id"]

    async def await_reframe_url(self, project_id: str, timeout_seconds: int = 600) -> str:
        """Poll a reframe project and return the output video URL when done."""
        poll_interval = 15
        for _ in range(max(1, timeout_seconds // poll_interval)):
            await asyncio.sleep(poll_interval)
            status = await self.get_job_status(project_id)
            if status == "completed":
                # Reframe projects use get-project-clips
                clips = await self.get_output_clips(project_id)
                if clips:
                    return clips[0].get("captioned_clip_url") or clips[0].get("clip_url", "")
                return ""
            if status == "failed":
                return ""
        return ""

    async def await_dubbing_url(self, project_id: str, timeout_seconds: int = 600) -> str:
        """Poll a dubbing project and return the dubbed video URL when done."""
        poll_interval = 15
        for _ in range(max(1, timeout_seconds // poll_interval)):
            await asyncio.sleep(poll_interval)
            status = await self.get_job_status(project_id)
            if status == "completed":
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self.BASE}/get-project-status",
                        headers=self._headers(),
                        params={"projectId": project_id},
                    ) as resp:
                        data = await resp.json() if resp.ok else {}
                urls = data.get("urls") or {}
                return urls.get("video") or urls.get("dubbedVideo", "")
            if status == "failed":
                return ""
        return ""


def _get_provider() -> AbstractClippingProvider:
    """Return the configured provider. Swap here when Phase 0 testing picks a winner."""
    provider = getattr(settings, "CLIPPING_API_PROVIDER", "reap")
    if provider == "reap":
        return ReapProvider()
    raise ValueError(f"Unknown clipping provider: {provider}")


# ─────────────────────────────────────────────────────────────────────────────
# Ingest helpers — Stage 1 (the part URI Social builds)
# ─────────────────────────────────────────────────────────────────────────────

def _run(cmd: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _probe(video_path: str) -> Dict[str, Any]:
    """Return basic metadata via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
        "-show_format", video_path,
    ]
    result = _run(cmd)
    try:
        return json.loads(result.stdout)
    except Exception:
        return {}


def _check_brightness(video_path: str) -> float:
    """
    Return average brightness (0-255) by sampling the first few frames.
    Uses FFmpeg signalstats filter. Returns -1 on failure.
    """
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", "select=lt(n\\,5),signalstats",
        "-f", "null", "-",
    ]
    result = _run(cmd, timeout=20)
    output = result.stderr
    brightnesses = []
    for line in output.splitlines():
        if "YAVG" in line:
            try:
                val = float(line.split("YAVG:")[1].split()[0])
                brightnesses.append(val)
            except Exception:
                pass
    return sum(brightnesses) / len(brightnesses) if brightnesses else -1


def _check_audio_level(video_path: str) -> Optional[float]:
    """
    Return mean volume in dBFS using FFmpeg volumedetect.
    Returns None if video has no audio.
    """
    cmd = [
        "ffmpeg", "-i", video_path,
        "-af", "volumedetect",
        "-f", "null", "-",
    ]
    result = _run(cmd, timeout=20)
    output = result.stderr
    for line in output.splitlines():
        if "mean_volume" in line:
            try:
                return float(line.split(":")[1].strip().replace(" dB", ""))
            except Exception:
                pass
    return None


def _quality_flags(video_path: str, duration: float) -> Dict[str, bool]:
    """
    Analyse video and return {dark, noisy, short} quality flags.
    Flags are advisory only — user can still proceed.
    """
    flags: Dict[str, bool] = {"dark": False, "noisy": False, "short": False}

    if duration < 5:
        flags["short"] = True

    brightness = _check_brightness(video_path)
    if 0 <= brightness < 40:
        flags["dark"] = True

    mean_vol = _check_audio_level(video_path)
    if mean_vol is not None and mean_vol < -35:
        flags["noisy"] = True

    return flags


def _normalise_to_mp4(src_path: str, dst_path: str) -> None:
    """Re-encode to H.264 MP4 if needed (handles MOV, HEVC, etc.)."""
    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        dst_path,
    ]
    _run(cmd, timeout=120)


# ─────────────────────────────────────────────────────────────────────────────
# Credit helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _deduct_credit(user_id: str, amount: float, reason: str) -> bool:
    """Call the URI transactions service to deduct credits."""
    if not settings.URI_TRANSACTIONS_BASE_URL:
        return True  # dev mode — skip
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{settings.URI_TRANSACTIONS_BASE_URL}/deduct",
                json={"user_id": user_id, "amount": amount, "reason": reason},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status == 200
    except Exception as e:
        print(f"[VideoPolish] credit deduction failed: {e}")
        return False


def _credit_amount(duration_seconds: float) -> float:
    """PRD §8.2: ≤2 min = 1.0 credit, 2-10 min = 2.0 credits."""
    if duration_seconds <= 120:
        return 1.0
    return 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Cloudinary upload
# ─────────────────────────────────────────────────────────────────────────────

async def _upload_to_cloudinary(video_bytes: bytes, filename: str) -> str:
    """Upload to Cloudinary and return the public URL."""
    import io
    import cloudinary.uploader
    loop = asyncio.get_running_loop()
    buf = io.BytesIO(video_bytes)
    buf.name = f"{filename}.mp4"
    result = await loop.run_in_executor(
        None,
        lambda: cloudinary.uploader.upload_large(
            buf,
            resource_type="video",
            folder="uri_polish_source",
            public_id=filename,
        ),
    )
    return result["secure_url"]


# ─────────────────────────────────────────────────────────────────────────────
# Main service
# ─────────────────────────────────────────────────────────────────────────────

class VideoPolishService:
    COLLECTION = "video_jobs"
    STYLES_COLLECTION = "video_style_presets"

    # ── Setup ──────────────────────────────────────────────────────────────

    @staticmethod
    async def ensure_styles_seeded(db) -> None:
        """Seed the 6 default style presets on first run."""
        count = await db[VideoPolishService.STYLES_COLLECTION].count_documents(
            {"is_custom": False}
        )
        if count == 0:
            now = datetime.now(timezone.utc)
            for preset in DEFAULT_STYLE_PRESETS:
                await db[VideoPolishService.STYLES_COLLECTION].update_one(
                    {"name": preset["name"], "is_custom": False},
                    {"$setOnInsert": {**preset, "created_at": now}},
                    upsert=True,
                )
            print("[VideoPolish] Seeded 6 default style presets")

    @staticmethod
    async def list_styles(db) -> List[Dict[str, Any]]:
        await VideoPolishService.ensure_styles_seeded(db)
        cursor = db[VideoPolishService.STYLES_COLLECTION].find(
            {"is_custom": False}, {"_id": 0}
        )
        return await cursor.to_list(length=20)

    # ── Job lifecycle ──────────────────────────────────────────────────────

    @staticmethod
    async def create_job(
        user_id: str,
        style_preset: str,
        language: str,
        db,
    ) -> str:
        job_id = str(uuid.uuid4())
        await db[VideoPolishService.COLLECTION].insert_one(
            {
                "job_id": job_id,
                "user_id": user_id,
                "style_preset": style_preset,
                "language_setting": language,
                "clipping_api_provider": getattr(settings, "CLIPPING_API_PROVIDER", "reap"),
                "status": "ingesting",
                "status_message": "Uploading your video…",
                "progress": 0,
                "source_video_url": "",
                "source_duration_seconds": 0,
                "source_quality_flags": {},
                "clipping_api_job_id": "",
                "output_clips": [],
                "credits_charged": 0,
                "user_action": "pending",
                "created_at": datetime.now(timezone.utc),
                "completed_at": None,
            }
        )
        return job_id

    @staticmethod
    async def _update(job_id: str, db, **fields) -> None:
        await db[VideoPolishService.COLLECTION].update_one(
            {"job_id": job_id}, {"$set": fields}
        )

    # ── Main job runner (runs as background task) ──────────────────────────

    @staticmethod
    async def run_job(
        job_id: str,
        user_id: str,
        video_bytes: bytes,
        style_preset: str,
        language: str,
        db,
        captions_preset: str = "system_beasty",
    ) -> None:
        update = VideoPolishService._update

        try:
            provider = _get_provider()

            # ── Stage 1a: validate + quality check ────────────────────────
            with tempfile.TemporaryDirectory() as tmp:
                src_path = f"{tmp}/source.mp4"
                norm_path = f"{tmp}/normalised.mp4"

                with open(src_path, "wb") as f:
                    f.write(video_bytes)

                probe = _probe(src_path)
                fmt = probe.get("format", {})
                duration = float(fmt.get("duration", 0))
                streams = probe.get("streams", [])

                # Validation — hard stops
                if duration < 120:  # Reap requires minimum 2 minutes
                    await update(job_id, db, status="failed",
                                 status_message=f"Video is too short ({int(duration)}s). Minimum is 2 minutes for AI polishing.")
                    return
                if duration > 10800:  # 3 hours (Reap max)
                    await update(job_id, db, status="failed",
                                 status_message="Video is too long (maximum 3 hours).")
                    return

                video_stream = next(
                    (s for s in streams if s.get("codec_type") == "video"), None
                )
                if not video_stream:
                    await update(job_id, db, status="failed",
                                 status_message="No video stream found in this file.")
                    return

                width = int(video_stream.get("width", 0))
                height = int(video_stream.get("height", 0))
                if min(width, height) < 360:
                    await update(job_id, db, status="failed",
                                 status_message="Resolution too low (minimum 360p).")
                    return

                # Quality flags (advisory)
                loop = asyncio.get_running_loop()
                quality_flags = await loop.run_in_executor(
                    None, _quality_flags, src_path, duration
                )
                await update(
                    job_id, db,
                    source_duration_seconds=int(duration),
                    source_quality_flags=quality_flags,
                    progress=10,
                    status_message="Video analysed. Preparing for polish…",
                )

                # ── Stage 1b: normalise codec ──────────────────────────────
                codec = (video_stream.get("codec_name") or "").lower()
                if codec not in ("h264", "avc1"):
                    await loop.run_in_executor(
                        None, _normalise_to_mp4, src_path, norm_path
                    )
                    process_path = norm_path
                else:
                    process_path = src_path

                with open(process_path, "rb") as f:
                    processed_bytes = f.read()

            # ── Stage 1c: upload to Cloudinary ────────────────────────────
            await update(job_id, db, progress=20, status_message="Uploading to cloud storage…")
            source_url = await _upload_to_cloudinary(processed_bytes, f"polish_{job_id}")
            await update(job_id, db, source_video_url=source_url, progress=30)

            # ── Stage 1d: deduct credits ───────────────────────────────────
            credit_amount = _credit_amount(duration)
            await _deduct_credit(user_id, credit_amount, f"video_polish:{job_id}")
            await update(job_id, db, credits_charged=credit_amount)

            # ── Stages 2-5: clipping API ───────────────────────────────────
            await update(job_id, db, status="processing",
                         status_message="Sending to polish engine…", progress=35)

            # Get style settings
            style_doc = await db[VideoPolishService.STYLES_COLLECTION].find_one(
                {"name": style_preset, "is_custom": False}, {"_id": 0}
            )
            if not style_doc:
                style_doc = DEFAULT_STYLE_PRESETS[1]  # fallback: Clean Professional
            api_settings = dict(style_doc.get("clipping_api_settings", {}))

            # Only force shorter clip durations for videos that can't support 30-60s clips.
            if duration < 90:
                api_settings["clipDurations"] = [[0, 30]]
            else:
                api_settings["clipDurations"] = [[30, 60]]

            # Override captionsPreset from request (user-chosen or default)
            api_settings["captionsPreset"] = captions_preset or "system_beasty"

            # Upload to Reap (gets its own upload ID)
            await update(job_id, db, progress=40, status_message="Processing with AI…")
            upload_id = await provider.upload_video(processed_bytes, f"polish_{job_id}.mp4")

            # Start transcription (uses same upload_id) concurrently with clip job
            trans_task = asyncio.create_task(provider.start_transcription(upload_id, language or "en"))
            provider_job_id = await provider.create_clip_job(upload_id, api_settings, language)
            transcription_project_id = await trans_task
            await update(job_id, db, clipping_api_job_id=provider_job_id)

            # Poll until done — Reap needs at least 10 min even for short videos
            poll_seconds = max(600, int(duration * 2))  # at least 10 min, 2× video duration
            poll_interval = 10
            max_polls = poll_seconds // poll_interval
            await update(job_id, db, progress=50,
                         status_message="Reframing and captioning your clip…")
            for attempt in range(max_polls):
                await asyncio.sleep(poll_interval)
                status = await provider.get_job_status(provider_job_id)
                progress = min(50 + int(attempt * 38 / max_polls), 88)
                await update(job_id, db, progress=progress)
                if status == "completed":
                    break
                if status == "invalid_content":
                    await update(job_id, db, status="failed",
                                 status_message="Video not suitable for AI clipping. Please use a video with a person clearly speaking on camera (min 2 minutes of dialogue).")
                    return
                if status == "failed":
                    await update(job_id, db, status="failed",
                                 status_message="Clipping engine returned an error. Please try again.")
                    return
            else:
                await update(job_id, db, status="failed",
                             status_message="Processing timed out. Please try again.")
                return

            # ── Retrieve and store output clips ───────────────────────────
            await update(job_id, db, progress=92, status_message="Fetching your clips…")
            clips = await provider.get_output_clips(provider_job_id)

            # Enrich clips with actual spoken-word transcript from Reap transcription.
            # Transcription runs ~2-5 min; clipping runs 15+ min, so it should be ready.
            if transcription_project_id and clips:
                await update(job_id, db, progress=95, status_message="Adding transcripts…")
                srt_text = await provider.fetch_srt(transcription_project_id, timeout_seconds=120)
                if srt_text:
                    srt_entries = _parse_srt(srt_text)
                    for clip in clips:
                        start = clip.get("start_time", 0)
                        end = clip.get("end_time") or (start + clip.get("duration", 60))
                        clip["transcript"] = _extract_transcript(srt_entries, start, end)
                    print(f"[VideoPolish] transcripts added to {len(clips)} clip(s)", flush=True)

            if not clips:
                await update(job_id, db, status="failed",
                             status_message="No clips were generated. Try with different footage or a different style.")
                return

            await update(
                job_id, db,
                status="ready",
                status_message=f"Your polished clip is ready! ({len(clips)} clip{'s' if len(clips) > 1 else ''})",
                output_clips=clips,
                progress=100,
                completed_at=datetime.now(timezone.utc),
            )
            print(f"[VideoPolish] job {job_id} complete — {len(clips)} clip(s)")

        except Exception as e:
            print(f"[VideoPolish] job {job_id} error: {e}")
            import traceback; traceback.print_exc()
            try:
                from app.database import get_db
                await VideoPolishService._update(
                    job_id, get_db(),
                    status="failed",
                    status_message="An unexpected error occurred. Please try again.",
                    progress=0,
                )
            except Exception as inner:
                print(f"[VideoPolish] failed to mark job {job_id} as failed: {inner}")

    # ── Restyle (0.5 credits) ──────────────────────────────────────────────

    @staticmethod
    async def restyle_job(
        original_job_id: str,
        user_id: str,
        new_style_preset: str,
        language: str,
        db,
    ) -> str:
        """
        Re-polish an already-uploaded video with a different style.
        Creates a new job referencing the same source URL. Costs 0.5 credits.
        """
        original = await db[VideoPolishService.COLLECTION].find_one(
            {"job_id": original_job_id, "user_id": user_id}
        )
        if not original or not original.get("source_video_url"):
            raise ValueError("Original job not found or has no source video")

        new_job_id = str(uuid.uuid4())
        await db[VideoPolishService.COLLECTION].insert_one(
            {
                "job_id": new_job_id,
                "user_id": user_id,
                "style_preset": new_style_preset,
                "language_setting": language,
                "clipping_api_provider": getattr(settings, "CLIPPING_API_PROVIDER", "reap"),
                "status": "processing",
                "status_message": "Restyling your clip…",
                "progress": 10,
                "source_video_url": original["source_video_url"],
                "source_duration_seconds": original.get("source_duration_seconds", 0),
                "source_quality_flags": original.get("source_quality_flags", {}),
                "clipping_api_job_id": "",
                "output_clips": [],
                "credits_charged": 0,
                "user_action": "pending",
                "parent_job_id": original_job_id,
                "created_at": datetime.now(timezone.utc),
                "completed_at": None,
            }
        )
        return new_job_id

    @staticmethod
    async def run_restyle_job(
        job_id: str,
        user_id: str,
        source_video_url: str,
        style_preset: str,
        language: str,
        db,
    ) -> None:
        """Run a restyle using the already-uploaded source video URL."""
        update = VideoPolishService._update
        try:
            provider = _get_provider()

            await _deduct_credit(user_id, 0.5, f"video_restyle:{job_id}")
            await update(job_id, db, credits_charged=0.5)

            style_doc = await db[VideoPolishService.STYLES_COLLECTION].find_one(
                {"name": style_preset, "is_custom": False}, {"_id": 0}
            )
            if not style_doc:
                style_doc = DEFAULT_STYLE_PRESETS[1]
            api_settings = style_doc.get("clipping_api_settings", {})

            # Download source bytes for re-upload to Reap
            async with aiohttp.ClientSession() as session:
                async with session.get(source_video_url) as resp:
                    video_bytes = await resp.read()

            await update(job_id, db, progress=20, status_message="Uploading for restyle…")
            upload_id = await provider.upload_video(video_bytes, f"restyle_{job_id}.mp4")
            provider_job_id = await provider.create_clip_job(upload_id, api_settings, language)
            await update(job_id, db, clipping_api_job_id=provider_job_id, progress=35)

            for attempt in range(36):
                await asyncio.sleep(5)
                status = await provider.get_job_status(provider_job_id)
                progress = min(35 + attempt * 2, 92)
                await update(job_id, db, progress=progress)
                if status == "completed":
                    break
                if status == "failed":
                    await update(job_id, db, status="failed",
                                 status_message="Restyle failed. Please try again.")
                    return

            clips = await provider.get_output_clips(provider_job_id)
            if not clips:
                await update(job_id, db, status="failed",
                             status_message="No clips generated. Try a different style.")
                return

            await update(
                job_id, db,
                status="ready",
                status_message=f"Restyled clip ready!",
                output_clips=clips,
                progress=100,
                completed_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            print(f"[VideoPolish] restyle {job_id} error: {e}")
            import traceback; traceback.print_exc()
            await update(job_id, db, status="failed",
                         status_message="Restyle failed. Please try again.")

    # ── Caption presets ────────────────────────────────────────────────────

    @staticmethod
    async def list_caption_presets() -> List[Dict[str, Any]]:
        return await _get_provider().get_caption_presets()

    # ── Clip actions (reframe / dub) ───────────────────────────────────────

    @staticmethod
    async def clip_action(
        job_id: str,
        clip_idx: int,
        action: str,        # "reframe" | "dub"
        params: Dict[str, Any],
        user_id: str,
        db,
    ) -> Dict[str, Any]:
        """
        Download a finished clip and run a secondary Reap operation on it.
        Returns {"status": "completed"|"failed", "clip_url": "...", "project_id": "..."}.
        Long-running — call in a background task and poll.
        """
        provider = _get_provider()
        job = await db[VideoPolishService.COLLECTION].find_one(
            {"job_id": job_id, "user_id": user_id}
        )
        if not job:
            raise ValueError("Job not found")
        clips: List[Dict] = job.get("output_clips", [])
        if clip_idx >= len(clips):
            raise ValueError("Clip index out of range")

        clip = clips[clip_idx]
        clip_url = clip.get("captioned_clip_url") or clip.get("clip_url", "")
        if not clip_url:
            raise ValueError("No clip URL for this clip")

        # Re-upload clip to get a fresh Reap upload_id
        upload_id = await provider.upload_from_url(
            clip_url, f"{action}_{job_id}_{clip_idx}.mp4"
        )

        if action == "reframe":
            orientation = params.get("orientation", "landscape")
            project_id = await provider.create_reframe(upload_id, orientation)
            result_url = await provider.await_reframe_url(project_id)
        elif action == "dub":
            source_lang = params.get("source_language", "en")
            target_lang = params.get("target_language", "es")
            project_id = await provider.create_dubbing(upload_id, source_lang, target_lang)
            result_url = await provider.await_dubbing_url(project_id)
        else:
            raise ValueError(f"Unknown action: {action}")

        return {
            "status": "completed" if result_url else "failed",
            "clip_url": result_url,
            "project_id": project_id,
        }

    # ── Query ──────────────────────────────────────────────────────────────

    @staticmethod
    async def get_job(job_id: str, user_id: str, db) -> Optional[Dict[str, Any]]:
        return await db[VideoPolishService.COLLECTION].find_one(
            {"job_id": job_id, "user_id": user_id}, {"_id": 0}
        )
