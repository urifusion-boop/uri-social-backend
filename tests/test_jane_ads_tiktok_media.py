"""
Unit tests for tiktok_media.py — the local ffmpeg/PIL replacement for the Cloudinary
delivery-URL tricks tiktok.py used to lean on (see that module's docstring for why).

httpx (the download step) and app.utils.s3_upload.upload_bytes (the re-upload step)
are mocked throughout, same convention as test_jane_ads_tiktok_adapter.py. ffmpeg/
ffprobe/PIL themselves are NOT mocked — they're real system dependencies the
production pipeline (video_production_service.py, multi_clip_service.py) already
hard-requires, so exercising them for real here is more honest than mocking out the
exact logic under test, and confirms the crop math is actually pixel-exact.
"""
import asyncio
import io
import subprocess
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from app.agents.jane_ads.adapters.tiktok_media import (
    ensure_jpg,
    ensure_tiktok_video,
    extract_poster_frame,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_image_bytes(fmt: str, size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(200, 50, 50)).save(buf, format=fmt)
    return buf.getvalue()


def _make_video_bytes(width: int, height: int, duration: float = 1.0) -> bytes:
    """A tiny real H.264 mp4 at the given resolution, generated via ffmpeg's lavfi
    test source — no fixture file to check in, and exact dimensions per test."""
    proc = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", f"color=c=blue:s={width}x{height}:d={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", "-f", "mp4",
         "-movflags", "frag_keyframe+empty_moov", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    )
    return proc.stdout


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


def _mock_download(content: bytes):
    client = AsyncMock()
    client.get = AsyncMock(return_value=_FakeResponse(content))
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    return cm


def test_ensure_jpg_is_a_noop_when_already_jpeg():
    jpeg_bytes = _make_image_bytes("JPEG")
    with patch("httpx.AsyncClient", return_value=_mock_download(jpeg_bytes)), \
         patch("app.agents.jane_ads.adapters.tiktok_media.upload_bytes") as mock_upload:
        result = _run(ensure_jpg("https://cdn.example.com/a.jpg"))
    assert result == "https://cdn.example.com/a.jpg"
    mock_upload.assert_not_called()


def test_ensure_jpg_converts_webp_to_jpeg_and_reuploads():
    webp_bytes = _make_image_bytes("WEBP")
    with patch("httpx.AsyncClient", return_value=_mock_download(webp_bytes)), \
         patch("app.agents.jane_ads.adapters.tiktok_media.upload_bytes",
               new=AsyncMock(return_value="https://s3.example.com/uri-ads/tiktok-normalized/img-xyz.jpg")) as mock_upload:
        result = _run(ensure_jpg("https://cdn.example.com/a.webp"))

    assert result == "https://s3.example.com/uri-ads/tiktok-normalized/img-xyz.jpg"
    mock_upload.assert_called_once()
    uploaded_bytes = mock_upload.call_args.args[0]
    assert Image.open(io.BytesIO(uploaded_bytes)).format == "JPEG"
    assert mock_upload.call_args.kwargs["resource_type"] == "image"


def test_ensure_jpg_falls_back_to_original_url_on_download_failure():
    with patch("httpx.AsyncClient", side_effect=RuntimeError("network down")):
        result = _run(ensure_jpg("https://cdn.example.com/a.webp"))
    assert result == "https://cdn.example.com/a.webp"


def test_ensure_tiktok_video_is_a_noop_for_an_already_exact_9_16_h264_clip():
    # 720x1280 reduces exactly to 9:16 (720*16 == 1280*9 == 11520) and is H.264 —
    # TikTok's own accepted shape, nothing to fix.
    video_bytes = _make_video_bytes(720, 1280)
    with patch("httpx.AsyncClient", return_value=_mock_download(video_bytes)), \
         patch("app.agents.jane_ads.adapters.tiktok_media.upload_bytes") as mock_upload:
        result = _run(ensure_tiktok_video("https://cdn.example.com/clip.mp4"))
    assert result == "https://cdn.example.com/clip.mp4"
    mock_upload.assert_not_called()


def test_ensure_tiktok_video_crops_to_an_exact_9_16_ratio_and_reuploads():
    # Live-caught 2026-09-22: TikTok rejected a real 480x848 upload ("Video ratio
    # must be 16:9/1:1/9:16") even though 848*9/16=477, only 3px off 480 — its
    # checker wants the ratio exact, not just close. Confirms the crop produces a
    # pixel-exact 9:16 output (w*16 == h*9) rather than a "close enough" one.
    video_bytes = _make_video_bytes(480, 848)
    with patch("httpx.AsyncClient", return_value=_mock_download(video_bytes)), \
         patch("app.agents.jane_ads.adapters.tiktok_media.upload_bytes",
               new=AsyncMock(return_value="https://s3.example.com/uri-ads/tiktok-normalized/vid-xyz.mp4")) as mock_upload:
        result = _run(ensure_tiktok_video("https://cdn.example.com/clip.mov"))

    assert result == "https://s3.example.com/uri-ads/tiktok-normalized/vid-xyz.mp4"
    mock_upload.assert_called_once()
    assert mock_upload.call_args.kwargs["resource_type"] == "video"

    uploaded_bytes = mock_upload.call_args.args[0]
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-"],
        input=uploaded_bytes, stdout=subprocess.PIPE, check=True,
    )
    import json
    streams = json.loads(probe.stdout)["streams"]
    video_stream = next(s for s in streams if s["codec_type"] == "video")
    w, h = video_stream["width"], video_stream["height"]
    assert video_stream["codec_name"] == "h264"
    assert w * 16 == h * 9, f"{w}x{h} is not an exact 9:16 ratio"


def test_ensure_tiktok_video_falls_back_to_original_url_on_download_failure():
    with patch("httpx.AsyncClient", side_effect=RuntimeError("network down")):
        result = _run(ensure_tiktok_video("https://cdn.example.com/clip.mov"))
    assert result == "https://cdn.example.com/clip.mov"


def test_extract_poster_frame_uploads_a_jpg_and_returns_its_url():
    video_bytes = _make_video_bytes(480, 848, duration=2.0)
    with patch("httpx.AsyncClient", return_value=_mock_download(video_bytes)), \
         patch("app.agents.jane_ads.adapters.tiktok_media.upload_bytes",
               new=AsyncMock(return_value="https://s3.example.com/uri-ads/tiktok-posters/poster-xyz.jpg")) as mock_upload:
        result = _run(extract_poster_frame("https://cdn.example.com/clip.mp4"))

    assert result == "https://s3.example.com/uri-ads/tiktok-posters/poster-xyz.jpg"
    uploaded_bytes = mock_upload.call_args.args[0]
    assert Image.open(io.BytesIO(uploaded_bytes)).format == "JPEG"
    assert mock_upload.call_args.kwargs["resource_type"] == "image"


def test_extract_poster_frame_falls_back_to_original_url_on_download_failure():
    with patch("httpx.AsyncClient", side_effect=RuntimeError("network down")):
        result = _run(extract_poster_frame("https://cdn.example.com/clip.mp4"))
    assert result == "https://cdn.example.com/clip.mp4"
