"""
TikTok creative media normalization — local replacement for the Cloudinary
delivery-URL tricks tiktok.py used to lean on (_force_jpg_delivery,
_force_tiktok_video_ratio, _as_mp4, _video_thumbnail_url, _wait_for_cloudinary_video).

Those were all "no-op for anything not hosted on Cloudinary" by design, so once
creative storage moved to S3 they'd silently stop firing — quietly reopening four
already-fixed, live-caught TikTok rejection bugs (WEBP images, near-but-not-exact
9:16 video, HEVC .MOV, missing campaign-list thumbnails). This module does the same
jobs with real local processing (ffmpeg/PIL) instead of a hosting-provider transform,
so it works the same regardless of where the source asset lives, and re-uploads the
result to S3 so TikTok's own UPLOAD_BY_URL fetch has something to pull.
"""
from __future__ import annotations

import asyncio
import io
import json
import tempfile
import uuid
from typing import Optional

import httpx
import numpy as np
from PIL import Image

from app.utils.s3_upload import upload_bytes

_TIKTOK_RATIO_TARGETS = ((9, 16), (1, 1), (16, 9))


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def _probe(path: str) -> dict:
    """width, height, codec_name of a local video file via ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    data = json.loads(stdout.decode() or "{}")
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return {"width": s.get("width", 0), "height": s.get("height", 0),
                    "codec_name": s.get("codec_name", "")}
    return {"width": 0, "height": 0, "codec_name": ""}


def _matches_tiktok_ratio(width: int, height: int) -> bool:
    """Live-caught 2026-09-22: TikTok rejected a 480×848 upload outright even
    though it's only 0.35% off 9:16 — its checker wants the ratio exact, not
    merely close, so this checks the exact integer relation (width*16==height*9
    etc.) rather than a float tolerance that would let that same case slip
    through as "already fine."."""
    if not width or not height:
        return False
    return any(width * h == height * w for w, h in _TIKTOK_RATIO_TARGETS)


async def ensure_jpg(image_url: str) -> str:
    """TikTok Carousel Ads reject WEBP ("image cannot be decoded") — Meta and our own
    upload picker both accept it, so this mismatch is invisible everywhere except
    TikTok's decoder. Converts to JPEG via PIL and re-uploads; no-op (same URL back)
    if the source is already a JPEG, so a repeat call on an already-fixed asset costs
    nothing extra."""
    try:
        raw = await _download(image_url)
        img = Image.open(io.BytesIO(raw))
        if (img.format or "").upper() in ("JPEG", "JPG"):
            return image_url
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=90)
        new_url = await upload_bytes(buf.getvalue(), folder="uri-ads/tiktok-normalized",
                                      resource_type="image", public_id=f"img-{uuid.uuid4().hex[:12]}")
        print(f"[TikTokMedia] converted {image_url} → {new_url}", flush=True)
        return new_url
    except Exception as e:
        print(f"[TikTokMedia] ensure_jpg failed for {image_url}: {e}", flush=True)
        return image_url


def _best_crop_offset(frame: "Image.Image", crop_w: int, crop_h: int) -> tuple[int, int]:
    """Where Cloudinary's g_auto used face/object/saliency detection to pick a crop
    that keeps the "interesting" part of the frame, this approximates the same goal
    without a new ML dependency: a Sobel-gradient energy map of one representative
    frame, sliding a crop_w×crop_h window across the dimension being cropped and
    keeping the position with the most retained edge energy (busy/detailed regions —
    faces, text, objects — score higher than flat background)."""
    gray = np.asarray(frame.convert("L"), dtype=np.float32)
    gy, gx = np.gradient(gray)
    energy = np.abs(gx) + np.abs(gy)
    h, w = energy.shape

    if crop_w < w:
        col_energy = energy.sum(axis=0)
        window = np.convolve(col_energy, np.ones(crop_w), mode="valid")
        x = int(np.argmax(window))
        return x, 0
    if crop_h < h:
        row_energy = energy.sum(axis=1)
        window = np.convolve(row_energy, np.ones(crop_h), mode="valid")
        y = int(np.argmax(window))
        return 0, y
    return 0, 0


async def ensure_tiktok_video(video_url: str) -> str:
    """TikTok flatly rejects delivery for a video whose ratio isn't exactly
    16:9/1:1/9:16 (live-caught: a 480×848 WhatsApp export, close to 9:16 but not
    exact), and its UPLOAD_BY_URL fetch doesn't reliably take an HEVC .MOV either.
    Re-encodes to H.264 .mp4 and, only when the ratio is actually off, crops to an
    exact 9:16 first — both in one ffmpeg pass. No-op (same URL back) when the
    source is already H.264 at an accepted ratio, so a clean upload isn't re-encoded
    for nothing."""
    try:
        raw = await _download(video_url)
    except Exception as e:
        print(f"[TikTokMedia] download failed for {video_url}: {e}", flush=True)
        return video_url

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(raw)
        in_path = f.name
    out_path = in_path + "_tt.mp4"

    try:
        info = await _probe(in_path)
        width, height, codec = info["width"], info["height"], info["codec_name"]
        ratio_ok = _matches_tiktok_ratio(width, height)
        if codec == "h264" and ratio_ok:
            return video_url

        vf_parts = []
        if not ratio_ok and width and height:
            # TikTok rejected a 480×848 source (ratio 0.5660, only 0.6% off 9:16's
            # 0.5625) outright — its checker isn't a tolerance match, it wants the
            # ratio exact. 9:16 only reduces exactly when both sides are a multiple
            # of 9 and 16 respectively, so snap to crop_h=16u / crop_w=9u for the
            # largest even u that fits inside the source — guarantees width*16 ==
            # height*9 exactly (and both dimensions even, required for yuv420p).
            u = min(height // 16, width // 9)
            u -= u % 2
            u = max(u, 2)
            crop_h, crop_w = u * 16, u * 9

            mid_frame_path = in_path + "_mid.jpg"
            extract = await asyncio.create_subprocess_exec(
                "ffmpeg", "-ss", "1", "-i", in_path, "-frames:v", "1", "-y", mid_frame_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await extract.wait()
            try:
                frame = Image.open(mid_frame_path)
                x, y = _best_crop_offset(frame, min(crop_w, width), min(crop_h, height))
            except Exception:
                x, y = max(0, (width - crop_w) // 2), max(0, (height - crop_h) // 2)
            finally:
                import os as _os
                if _os.path.exists(mid_frame_path):
                    _os.unlink(mid_frame_path)
            vf_parts.append(f"crop={crop_w}:{crop_h}:{x}:{y}")

        cmd = ["ffmpeg", "-i", in_path]
        if vf_parts:
            cmd += ["-vf", ",".join(vf_parts)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                "-movflags", "+faststart", "-y", out_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        with open(out_path, "rb") as of:
            processed = of.read()
        new_url = await upload_bytes(processed, folder="uri-ads/tiktok-normalized",
                                      resource_type="video", public_id=f"vid-{uuid.uuid4().hex[:12]}")
        print(f"[TikTokMedia] normalized {video_url} ({width}x{height}, {codec}) → {new_url}", flush=True)
        return new_url
    except Exception as e:
        print(f"[TikTokMedia] ensure_tiktok_video failed for {video_url}: {e}", flush=True)
        return video_url
    finally:
        import os as _os
        _os.unlink(in_path)
        if _os.path.exists(out_path):
            _os.unlink(out_path)


async def extract_poster_frame(video_url: str) -> str:
    """Display-only thumbnail for Jane's own Campaign Manager list (TikTok's API
    separately returns its own video_cover_url for the actual ad creative — this is
    just so the list view's <img> tag has something to render instead of a raw .mp4
    URL). Pulls one frame via ffmpeg and uploads it as a JPG; no-op (returns the
    original URL) if extraction fails, same as before."""
    try:
        raw = await _download(video_url)
    except Exception as e:
        print(f"[TikTokMedia] poster download failed for {video_url}: {e}", flush=True)
        return video_url

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(raw)
        in_path = f.name
    out_path = in_path + "_poster.jpg"

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-ss", "1", "-i", in_path, "-frames:v", "1", "-y", out_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        with open(out_path, "rb") as of:
            frame_bytes = of.read()
        new_url = await upload_bytes(frame_bytes, folder="uri-ads/tiktok-posters",
                                      resource_type="image", public_id=f"poster-{uuid.uuid4().hex[:12]}")
        return new_url
    except Exception as e:
        print(f"[TikTokMedia] extract_poster_frame failed for {video_url}: {e}", flush=True)
        return video_url
    finally:
        import os as _os
        _os.unlink(in_path)
        if _os.path.exists(out_path):
            _os.unlink(out_path)
