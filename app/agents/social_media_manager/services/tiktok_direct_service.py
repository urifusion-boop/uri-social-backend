"""
Direct TikTok posting — Content Posting API via FILE_UPLOAD, bypassing Outstand.

Why this exists: TikTok's Content Posting API requires the video source domain to
be verified in the Developer Portal for PULL_FROM_URL mode. Our videos live on
Cloudinary (res.cloudinary.com), a domain we don't own and can't verify — that's
why TikTok posting has gone through Outstand's already-verified domain until now.
FILE_UPLOAD sidesteps that entirely: we stream the video's raw bytes to TikTok
instead of handing them a URL to fetch, so no domain verification is needed at
all. This module is additive — the existing Outstand path (video_publish_service.py
/ approval_workflow_service.py) stays as the fallback for brands who haven't
reconnected via the new /connect/tiktok-direct/* flow.

Hand-built against TikTok's public Content Posting API / Login Kit v2 docs — not
yet verified against a live account (no real connection has been made through this
flow yet). Same honesty as this session's other new third-party integrations
(the Google Ads and TikTok Ads adapters): should get a "verified end-to-end"
header update once real credentials/a real connection exist.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.core.config import settings

CONNECTIONS = "social_connections"
_TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"
_API_BASE = "https://open.tiktokapis.com/v2"

# TikTok access tokens live 24h (expires_in in the token response) — refresh a
# bit early so a publish never races a token that expires mid-flight.
_TOKEN_REFRESH_MARGIN_SECONDS = 300

# TikTok's documented per-chunk cap (the final chunk may go up to 128MB) — used
# here as the single-chunk ceiling too, since most short-form video is well
# under this.
_MAX_SINGLE_CHUNK_BYTES = 64 * 1024 * 1024


class TikTokDirectAPIError(Exception):
    """A TikTok API call failed, or this module is misconfigured. Never
    swallowed silently by callers — a publish either succeeds or raises."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


def _raise_for_error(data: dict, context: str) -> None:
    # TikTok's envelope: {"data": {...}, "error": {"code": "ok", "message": "...", "log_id": "..."}}
    # — a present-but-"ok" error object is success, not a top-level "error" key
    # the way Meta/Google shape their errors.
    err = (data or {}).get("error") or {}
    code = err.get("code")
    if code and code != "ok":
        raise TikTokDirectAPIError(f"{context}: {err.get('message') or code}", code=code)


async def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """One-shot code->tokens exchange, right after the OAuth consent redirect."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            _TOKEN_ENDPOINT,
            data={
                "client_key": settings.TIKTOK_APP_CLIENT_KEY,
                "client_secret": settings.TIKTOK_APP_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    data = resp.json()
    if "access_token" not in data:
        raise TikTokDirectAPIError(f"code exchange failed: {data}")
    return data


async def fetch_user_info(access_token: str) -> dict:
    """display_name/avatar for the connected TikTok account — same purpose as
    Facebook's page-name/picture fetch in the direct-OAuth callback."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{_API_BASE}/user/info/",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "open_id,display_name,avatar_url"},
        )
    data = resp.json()
    _raise_for_error(data, "user info fetch")
    return (data.get("data") or {}).get("user") or {}


async def _refresh_tokens(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            _TOKEN_ENDPOINT,
            data={
                "client_key": settings.TIKTOK_APP_CLIENT_KEY,
                "client_secret": settings.TIKTOK_APP_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    data = resp.json()
    if "access_token" not in data:
        raise TikTokDirectAPIError(f"token refresh failed: {data}")
    return data


async def get_valid_tiktok_access_token(db, connection_doc: dict) -> str:
    """Returns a still-valid access_token, refreshing first if it's expired or
    close to it — same shape as google_ads_connection.py's
    get_valid_access_token. Persists the refreshed tokens back onto the
    connection doc; callers never read connection_doc["access_token"] after a
    refresh without re-fetching."""
    expires_at = connection_doc.get("token_expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            expires_at = None
    if isinstance(expires_at, datetime):
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
        if remaining > _TOKEN_REFRESH_MARGIN_SECONDS:
            return connection_doc["access_token"]

    refresh_token = connection_doc.get("refresh_token")
    if not refresh_token:
        raise TikTokDirectAPIError("no refresh_token stored on this TikTok connection — reconnect required")

    data = await _refresh_tokens(refresh_token)
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 86400)))
    await db[CONNECTIONS].update_one(
        {"id": connection_doc["id"]},
        {"$set": {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "token_expires_at": new_expires_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return data["access_token"]


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}


async def publish_tiktok_direct(access_token: str, video_url: str, caption: str) -> tuple[str, str]:
    """Downloads the video, uploads it to TikTok via FILE_UPLOAD, and polls
    until the post finishes. Returns (publish_id, status). Raises
    TikTokDirectAPIError on any failure — a publish either succeeds or the
    caller finds out why, never a silent no-op."""
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        video_resp = await client.get(video_url)
    if video_resp.status_code != 200:
        raise TikTokDirectAPIError(f"video download failed: HTTP {video_resp.status_code}")
    video_bytes = video_resp.content
    video_size = len(video_bytes)

    # Single chunk for the common case (short-form video, well under the cap);
    # real multi-chunk splitting only kicks in for larger files.
    if video_size <= _MAX_SINGLE_CHUNK_BYTES:
        chunk_size = video_size
        total_chunks = 1
    else:
        chunk_size = _MAX_SINGLE_CHUNK_BYTES
        total_chunks = (video_size + chunk_size - 1) // chunk_size

    async with httpx.AsyncClient(timeout=120) as client:
        init_resp = await client.post(
            f"{_API_BASE}/post/publish/video/init/",
            headers=_headers(access_token),
            json={
                "post_info": {
                    "title": caption or "",
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunks,
                },
            },
        )
    init_data = init_resp.json()
    _raise_for_error(init_data, "video init")
    inner = init_data.get("data") or {}
    upload_url = inner.get("upload_url")
    publish_id = inner.get("publish_id")
    if not upload_url or not publish_id:
        raise TikTokDirectAPIError(f"init did not return upload_url/publish_id: {init_data}")

    # Sequential chunked PUT, per TikTok's documented Content-Range contract —
    # 206 for intermediate chunks, 201 for the final one.
    async with httpx.AsyncClient(timeout=300) as client:
        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, video_size) - 1
            chunk = video_bytes[start:end + 1]
            put_resp = await client.put(
                upload_url,
                content=chunk,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{video_size}",
                },
            )
            if put_resp.status_code not in (200, 201, 206):
                raise TikTokDirectAPIError(
                    f"chunk {i + 1}/{total_chunks} upload failed: HTTP {put_resp.status_code} {put_resp.text[:300]}"
                )

    # Poll publish status until it settles. Endpoint path per TikTok's Content
    # Posting API reference — third-party sources disagree on the exact path,
    # so this needs confirming against a live account before this is fully
    # trusted (see module docstring).
    status = "PROCESSING_DOWNLOAD"
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(30):  # ~2.5 min max at 5s intervals
            status_resp = await client.post(
                f"{_API_BASE}/post/publish/status/fetch/",
                headers=_headers(access_token),
                json={"publish_id": publish_id},
            )
            status_data = status_resp.json()
            _raise_for_error(status_data, "publish status")
            status = (status_data.get("data") or {}).get("status", status)
            if status in ("PUBLISH_COMPLETE", "FAILED"):
                break
            await asyncio.sleep(5)

    if status == "FAILED":
        raise TikTokDirectAPIError(f"TikTok reported publish failure for publish_id={publish_id}")

    return publish_id, status
