"""Posts the owner already published on their own Instagram or Facebook Page, offered as
ad creative. Read with the tokens they granted when they connected the account — no
extra Meta permission is involved."""
from __future__ import annotations

from typing import Optional

import httpx

from app.core.config import settings
from .ads_connection import CONNECTIONS, _brand_scope

LIMIT = 24
_IG_FIELDS = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp"
_FB_FIELDS = "id,message,full_picture,permalink_url,created_time,attachments{media_type}"


class PostsError(Exception):
    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail, self.status = detail, status


def _graph() -> str:
    return f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"


def instagram_post(raw: dict) -> dict:
    kind = (raw.get("media_type") or "").upper()
    is_video = kind == "VIDEO"
    return {
        "platform": "instagram",
        "post_id": str(raw.get("id") or ""),
        "text": raw.get("caption") or "",
        "image_url": (raw.get("thumbnail_url") if is_video else raw.get("media_url")) or "",
        "is_video": is_video,
        "permalink": raw.get("permalink") or "",
        "created_at": raw.get("timestamp") or "",
    }


def facebook_post(raw: dict) -> dict:
    media = ((raw.get("attachments") or {}).get("data") or [{}])[0].get("media_type") or ""
    return {
        "platform": "facebook",
        "post_id": str(raw.get("id") or ""),
        "text": raw.get("message") or "",
        "image_url": raw.get("full_picture") or "",
        "is_video": media == "video",
        "permalink": raw.get("permalink_url") or "",
        "created_at": raw.get("created_time") or "",
    }


async def _connection(db, user_id: Optional[str], brand_id: Optional[str], platform: str) -> dict:
    conn = await db[CONNECTIONS].find_one(
        {"platform": platform, "connection_status": "active", **_brand_scope(user_id, brand_id)},
        sort=[("connected_at", -1)])
    if not conn:
        raise PostsError(f"Connect your {platform.capitalize()} account first.", 404)
    return conn


async def list_posts(db, user_id: Optional[str], brand_id: Optional[str], platform: str) -> list[dict]:
    if platform not in ("instagram", "facebook"):
        raise PostsError("Platform must be instagram or facebook.")
    conn = await _connection(db, user_id, brand_id, platform)
    token = conn.get("page_access_token") or conn.get("access_token") or ""
    if platform == "instagram":
        owner, edge, fields, shape = conn.get("ig_user_id"), "media", _IG_FIELDS, instagram_post
    else:
        owner, edge, fields, shape = conn.get("page_id"), "published_posts", _FB_FIELDS, facebook_post
    if not (owner and token):
        raise PostsError(f"Reconnect your {platform.capitalize()} account to list its posts.")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{_graph()}/{owner}/{edge}",
                                params={"fields": fields, "limit": LIMIT, "access_token": token})
    data = resp.json()
    if data.get("error"):
        raise PostsError("Meta wouldn't list your posts. Reconnect the account and try again.", 502)
    return [p for p in (shape(r) for r in data.get("data") or []) if p["post_id"] and p["image_url"]]
