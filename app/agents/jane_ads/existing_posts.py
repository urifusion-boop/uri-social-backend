"""Posts the owner already published on their own Instagram or Facebook Page, offered as
ad creative. Read with the tokens they granted when they connected the account — no
extra Meta permission is involved."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import httpx

from app.core.config import settings
from .ads_connection import CONNECTIONS, _brand_scope

LIMIT = 24
MADE_IN_URI_WINDOW = timedelta(minutes=30)
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


async def _connections(db, user_id: Optional[str], brand_id: Optional[str], platform: str) -> list[dict]:
    rows = await db[CONNECTIONS].find(
        {"platform": platform, "connection_status": "active", **_brand_scope(user_id, brand_id)},
        sort=[("connected_at", -1)]).to_list(20)
    if not rows:
        raise PostsError(f"Connect your {platform.capitalize()} account first.", 404)
    return rows


async def _posts_of(client: httpx.AsyncClient, conn: dict, platform: str) -> list[dict]:
    token = conn.get("page_access_token") or conn.get("access_token") or ""
    if platform == "instagram":
        owner, edge, fields, shape = conn.get("ig_user_id"), "media", _IG_FIELDS, instagram_post
    else:
        owner, edge, fields, shape = conn.get("page_id"), "published_posts", _FB_FIELDS, facebook_post
    if not (owner and token):
        raise PostsError("Reconnect this account to list its posts.")
    resp = await client.get(f"{_graph()}/{owner}/{edge}",
                            params={"fields": fields, "limit": LIMIT, "access_token": token})
    data = resp.json()
    if data.get("error"):
        raise PostsError("Meta wouldn't list this account's posts.", 502)
    name = conn.get("username") or conn.get("page_name") or conn.get("account_name") or ""
    return [{**p, "account": name} for p in (shape(r) for r in data.get("data") or [])
            if p["post_id"] and p["image_url"]]


def _moment(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


async def _made_in_uri(db, user_id: Optional[str], brand_id: Optional[str], platform: str) -> tuple[set[str], list[datetime]]:
    """Ids and publish times of posts URI published for this workspace. Outstand posts
    keep their own id rather than Meta's, so the publish time is the second signal."""
    rows = await db["content_drafts"].find(
        {**_brand_scope(user_id, brand_id), "status": "published", "platform": platform},
        {"_id": 0, "platform_post_id": 1, "published_date": 1}).to_list(500)
    ids = {str(r["platform_post_id"]) for r in rows if r.get("platform_post_id")}
    times = [t for t in (_moment(r.get("published_date")) for r in rows) if t]
    return ids, times


def _by_hand(post: dict, ids: set[str], times: list[datetime]) -> bool:
    if post["post_id"] in ids:
        return False
    at = _moment(post.get("created_at"))
    return not (at and any(abs(at - t) <= MADE_IN_URI_WINDOW for t in times))


async def list_posts(db, user_id: Optional[str], brand_id: Optional[str], platform: str) -> list[dict]:
    """Posts made by hand on every active account of this platform, newest account first.
    Posts URI published are left out. An
    account Meta refuses (say, its token lost a permission) is skipped, so one stale
    connection doesn't hide the others."""
    if platform not in ("instagram", "facebook"):
        raise PostsError("Platform must be instagram or facebook.")
    out: list[dict] = []
    failure: Optional[PostsError] = None
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=20) as client:
        for conn in await _connections(db, user_id, brand_id, platform):
            key = str(conn.get("ig_user_id") if platform == "instagram" else conn.get("page_id"))
            if key in seen:
                continue
            seen.add(key)
            try:
                out += await _posts_of(client, conn, platform)
            except PostsError as e:
                failure = failure or e
    ids, times = await _made_in_uri(db, user_id, brand_id, platform)
    out = [p for p in out if _by_hand(p, ids, times)]
    if not out and failure:
        raise PostsError("Meta wouldn't list your posts. Reconnect the account and try again.", 502)
    return out
