"""
Serves a draft's images to TikTok from our own API.

Why this exists: TikTok's photo Direct Post has no file-upload mode — it only
PULLs each image from a URL, and that URL has to sit under a prefix verified in
the TikTok Developer Portal (otherwise `url_ownership_unverified`). Our images
live on Cloudinary / as base64, domains we can't verify. So the photo publish
hands TikTok links under MEDIA_PATH on our own API instead, and the endpoint
here streams the (normalised) image back.

The links are signed and expire quickly: the endpoint is necessarily public
(TikTok's fetcher can't authenticate), and the signature is what stops anyone
from enumerating draft ids to read other people's images.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

# The prefix to verify in the TikTok Developer Portal (URL properties), appended
# to the public API base. Keep in sync with the route in complete_social_manager.
MEDIA_PATH = "/social-media/tiktok-media/"

# TikTok fetches within seconds-to-minutes of the init call.
_LINK_TTL_SECONDS = 60 * 60

# TikTok's photo requirements (Media Transfer Guide): JPEG/WebP, max 1080p, max 20MB.
_MAX_DIMENSION = 1080
_MAX_BYTES = 20 * 1024 * 1024


def public_base() -> str:
    return (settings.PUBLIC_API_URL or settings.URI_GATEWAY_BASE_API_URL).rstrip("/")


def _sign(draft_id: str, index: int, exp: int) -> str:
    msg = f"{draft_id}:{index}:{exp}".encode()
    return hmac.new(settings.AUTHJWT_SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def verify(draft_id: str, index: int, exp: int, sig: str) -> bool:
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_sign(draft_id, index, exp), sig or "")


def draft_image_sources(draft: Dict[str, Any]) -> List[str]:
    """The raw image reference for each picture a draft would post: one per
    carousel slide, or the single image."""
    if draft.get("post_type") == "carousel":
        sources = [s.get("image_url") or "" for s in (draft.get("slides") or [])]
    else:
        sources = [draft.get("image_url") or ""]
    return [s for s in sources if s]


def signed_urls(draft_id: str, count: int) -> List[str]:
    exp = int(time.time()) + _LINK_TTL_SECONDS
    base = public_base()
    return [
        f"{base}{MEDIA_PATH}{draft_id}/{i}.jpg?exp={exp}&sig={_sign(draft_id, i, exp)}"
        for i in range(count)
    ]


async def _load_bytes(source: str) -> bytes:
    if source.startswith("data:"):
        return base64.b64decode(source.split(",", 1)[1])
    if source.startswith("/"):
        source = f"{public_base()}{source}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(source)
        resp.raise_for_status()
    return resp.content


def _to_tiktok_jpeg(raw: bytes) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    if max(img.size) > _MAX_DIMENSION:
        scale = _MAX_DIMENSION / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    img = img.convert("RGB")
    for quality in (90, 70):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= _MAX_BYTES:
            break
    return buf.getvalue()


async def render_jpeg(draft: Dict[str, Any], index: int) -> Optional[bytes]:
    """The index-th image of the draft as a TikTok-compliant JPEG, or None if the
    draft has no such image."""
    sources = draft_image_sources(draft)
    if index < 0 or index >= len(sources):
        return None
    raw = await _load_bytes(sources[index])
    return await asyncio.to_thread(_to_tiktok_jpeg, raw)
