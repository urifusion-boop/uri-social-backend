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

Hand-built against TikTok's public Content Posting API / Login Kit v2 docs.
Partially verified against a live account: OAuth connect/token exchange,
creator_info query, and video/init all confirmed working against real TikTok
responses. The chunked upload PUT and status-poll loop remain unverified —
every live test so far has been correctly rejected by TikTok before reaching
them, either for a Sandbox app posting to a public account
(unaudited_client_can_only_post_to_private_accounts) or a Production app
whose redirect_uri hadn't cleared TikTok's review yet. Should get a fully
"verified end-to-end" note once a publish actually reaches PUBLISH_COMPLETE.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.core.config import settings
from app.agents.social_media_manager.services import rate_limit as rl

CONNECTIONS = "social_connections"
_TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"
_API_BASE = "https://open.tiktokapis.com/v2"

# What a direct TikTok connection asks the user to grant. user.info.stats (follower / like /
# video totals) and video.list (per-video views, likes, comments, shares) feed the Performance
# page; both must be approved on the TikTok developer app or TikTok refuses the whole consent
# screen with scope_not_authorized.
TIKTOK_SCOPES = "user.info.basic,user.info.stats,video.list,video.publish"
# The two read scopes a connection made before they were added is missing.
ANALYTICS_SCOPES = ("user.info.stats", "video.list")

# TikTok access tokens live 2h (24h before 1 Dec 2026) and the response's expires_in says
# which — refresh a bit early so a publish never races a token that expires mid-flight.
_TOKEN_REFRESH_MARGIN_SECONDS = 300

# Every refresh REPLACES the refresh token and invalidates the old one, so two refreshes
# with the same token cannot both succeed, and a new token that is not saved is a
# connection the user must redo. These bound the three ways that goes wrong.
_REFRESH_LOCK_SECONDS = 30          # a refresh that dies mid-flight frees its claim after this
_REFRESH_WAIT_SECONDS = 15          # how long a second caller waits on the first one's refresh
_REFRESH_POLL_SECONDS = 0.4
_SAVE_ATTEMPTS = 3
_SAVE_BACKOFF_SECONDS = 0.3

# TikTok's documented per-chunk cap (the final chunk may go up to 128MB) — used
# here as the single-chunk ceiling too, since most short-form video is well
# under this.
_MAX_SINGLE_CHUNK_BYTES = 64 * 1024 * 1024

# How long to back off when a status check is rate limited and TikTok gave no Retry-After.
# Status fetches are capped at 30/min per user, so a short pause is enough.
_STATUS_LIMIT_WAIT_S = 10.0


class TikTokDirectAPIError(Exception):
    """A TikTok API call failed, or this module is misconfigured. Never
    swallowed silently by callers — a publish either succeeds or raises."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class TikTokStillProcessingError(TikTokDirectAPIError):
    """TikTok accepted the post but hadn't finished (or failed) it when we stopped
    polling. Not a failure and not a success — callers must not report it as
    published, since success can only be shown once TikTok confirms it."""


class TikTokReconnectRequired(TikTokDirectAPIError):
    """The connection's refresh token is dead — TikTok's `invalid_grant`: invalid, expired
    (180 days), revoked, or already replaced. Nothing we retry fixes it; only the user
    connecting TikTok again does, so the connection is flagged and the user is told."""


class TikTokRateLimitError(TikTokDirectAPIError, rl.RateLimitedError):
    """TikTok refused a call because of a rate limit: `.rate_limit` says whether it is a temporary
    per-minute limit (retry) or a daily / app-wide cap (never retry). Still a TikTokDirectAPIError,
    so existing handlers keep working, and `.code` keeps TikTok's own code."""

    def __init__(self, message: str, info: "rl.RateLimitInfo", code: Optional[str] = None) -> None:
        # Not super().__init__: the two bases take different arguments.
        Exception.__init__(self, message)
        self.code = code or info.code
        self.rate_limit = info


RECONNECT_MESSAGE = "Your TikTok connection has expired. Reconnect TikTok in Connected Accounts to keep posting."


def _raise_for_error(data: dict, context: str, status: Optional[int] = None, headers=None) -> None:
    # TikTok's envelope: {"data": {...}, "error": {"code": "ok", "message": "...", "log_id": "..."}}
    # — a present-but-"ok" error object is success, not a top-level "error" key
    # the way Meta/Google shape their errors.
    err = (data or {}).get("error") or {}
    code = err.get("code")
    if code and code != "ok":
        # The code (invalid_param, url_ownership_unverified, scope_not_authorized,
        # unaudited_client_can_only_post_to_private_accounts, ...) is what says WHICH
        # rule failed — TikTok's message is often the same generic "please review our
        # integration guidelines" for several of them — so it, and the log_id TikTok
        # support asks for, always go into the error shown to the user and the logs.
        log_id = err.get("log_id")
        suffix = f" [{code}{f', log_id={log_id}' if log_id else ''}]"
        message = f"{context}: {err.get('message') or code}{suffix}"
        info = rl.classify_tiktok(code, err.get("message") or "", status, headers)
        if info:
            raise TikTokRateLimitError(message, info, code=code)
        raise TikTokDirectAPIError(message, code=code)


def _checked(resp, context: str) -> dict:
    """A TikTok response's JSON, raised on as a TikTok error. Checks the HTTP status too: a 429 (or
    5xx) often comes back as a bare page, not TikTok's JSON envelope, and used to crash with a
    JSONDecodeError instead of being recognised as a rate limit."""
    status = getattr(resp, "status_code", None)
    headers = getattr(resp, "headers", None)
    try:
        data = resp.json()
    except ValueError:
        info = rl.classify_tiktok(None, status=status, headers=headers)
        if info:
            raise TikTokRateLimitError(f"{context}: TikTok is rate limiting us (HTTP {status})", info, code=info.code)
        raise TikTokDirectAPIError(f"{context}: unexpected response from TikTok (HTTP {status})",
                                   code=f"http_{status}" if status else None)
    _raise_for_error(data, context, status, headers)
    return data


def _classify(exc: BaseException) -> Optional["rl.RateLimitInfo"]:
    return rl.classify_exception(exc, "tiktok")


def build_authorize_url(client_key: str, redirect_uri: str, state: str) -> str:
    """The TikTok login/consent URL a user is sent to when connecting an account.

    disable_auto_auth=1 makes TikTok always show its authorization page. Without it TikTok
    silently approves whichever account the browser is already logged into, so someone who wants
    to connect a different TikTok account never gets the chance to switch.
    """
    import urllib.parse

    params = {
        "client_key": client_key,
        "redirect_uri": redirect_uri,
        "scope": TIKTOK_SCOPES,
        "response_type": "code",
        "state": state,
        "disable_auto_auth": 1,
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(params)


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


def has_analytics_scopes(connection_doc: dict) -> bool:
    """Whether this connection's user granted the read scopes the Performance page needs. A
    connection made before they were requested has no record of them and must reconnect once."""
    granted = {x.strip() for x in str(connection_doc.get("granted_scopes") or "").split(",") if x.strip()}
    return all(sc in granted for sc in ANALYTICS_SCOPES)


async def fetch_user_stats(access_token: str) -> dict:
    """Profile totals — follower_count, following_count, likes_count, video_count — for the
    connected account. Needs the user.info.stats scope."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{_API_BASE}/user/info/",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "open_id,display_name,avatar_url,follower_count,following_count,likes_count,video_count"},
        )
    data = _checked(resp, "user stats fetch")
    return (data.get("data") or {}).get("user") or {}


_VIDEO_FIELDS = (
    "id,create_time,cover_image_url,share_url,video_description,title,duration,"
    "view_count,like_count,comment_count,share_count"
)


async def list_videos(access_token: str, max_videos: int = 60) -> list[dict]:
    """The account's most recent public videos, newest first, with their lifetime view / like /
    comment / share counts. Needs the video.list scope. TikTok pages 20 at a time."""
    videos: list[dict] = []
    cursor: Optional[int] = None
    async with httpx.AsyncClient(timeout=30) as client:
        while len(videos) < max_videos:
            body: dict = {"max_count": 20}
            if cursor is not None:
                body["cursor"] = cursor
            resp = await client.post(
                f"{_API_BASE}/video/list/",
                params={"fields": _VIDEO_FIELDS},
                headers=_headers(access_token),
                json=body,
            )
            inner = _checked(resp, "video list").get("data") or {}
            videos.extend(inner.get("videos") or [])
            if not inner.get("has_more"):
                break
            cursor = inner.get("cursor")
            if cursor is None:
                break
    return videos[:max_videos]


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
    try:
        data = resp.json()
    except ValueError:
        raise TikTokDirectAPIError(
            f"token refresh failed: TikTok returned an unreadable response (HTTP {resp.status_code})"
        ) from None
    if "access_token" not in data:
        # TikTok's OAuth errors are flat: {"error": "invalid_grant", "error_description": ..., "log_id": ...}.
        # Only invalid_grant (refresh token invalid, expired, revoked or already replaced) means the
        # user must connect again. server_error / temporarily_unavailable are TikTok's own trouble and
        # invalid_client is OUR credentials — none of those may mark a healthy connection as broken.
        error = data.get("error")
        error = (error.get("code") if isinstance(error, dict) else error) or ""
        detail = f"token refresh failed: {error or 'no access token returned'}"
        if data.get("error_description"):
            detail += f" — {data['error_description']}"
        if data.get("log_id"):
            detail += f" [log_id={data['log_id']}]"
        if error == "invalid_grant":
            raise TikTokReconnectRequired(detail, code=error)
        raise TikTokDirectAPIError(detail, code=error or None)
    return data


# New tokens TikTok has already issued but we could not write to the database, by connection id.
# TikTok has invalidated the old refresh token by then, so these are the only copy: they are
# written out on the next call, and used in the meantime. Lost only if the process dies first.
_unsaved_tokens: dict = {}


def _as_utc(value) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _still_valid(doc: dict) -> bool:
    expires_at = _as_utc((doc or {}).get("token_expires_at"))
    if not expires_at or not (doc or {}).get("access_token"):
        return False
    return (expires_at - datetime.now(timezone.utc)).total_seconds() > _TOKEN_REFRESH_MARGIN_SECONDS


def _token_fields(data: dict, previous_refresh_token: str) -> dict:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 7200)))
    fields = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", previous_refresh_token),
        "token_expires_at": expires_at.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if data.get("refresh_expires_in") is not None:
        refresh_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data["refresh_expires_in"]))
        fields["refresh_token_expires_at"] = refresh_expires_at.isoformat()
    return fields


async def _save_tokens(db, connection_id: str, fields: dict, owner: Optional[str] = None) -> bool:
    """Write refreshed tokens, retrying, and release the refresh claim in the same write.
    Returns whether it was saved. A rotated refresh token that is not saved is a lost
    connection, so a failed write is retried and then kept in memory, never dropped."""
    query = {"id": connection_id}
    if owner:
        query["refresh_lock_owner"] = owner
    update = {"$set": fields, "$unset": {"refresh_lock_owner": "", "refresh_lock_until": ""}}
    for attempt in range(_SAVE_ATTEMPTS):
        try:
            await db[CONNECTIONS].update_one(query, update)
            return True
        except Exception as e:  # noqa: BLE001 - any write failure must be retried
            print(f"[TikTokDirect] saving refreshed tokens failed (attempt {attempt + 1}): {type(e).__name__}")
            if attempt + 1 < _SAVE_ATTEMPTS:
                await asyncio.sleep(_SAVE_BACKOFF_SECONDS * (attempt + 1))
    return False


async def _claim_refresh(db, connection_id: str, refresh_token: str) -> Optional[str]:
    """Take the right to refresh this connection, or None when someone else holds it or has
    already moved the refresh token on. Conditional on the refresh token being the one we read,
    so a caller holding a stale token cannot start a refresh that TikTok will refuse."""
    now = datetime.now(timezone.utc)
    owner = uuid.uuid4().hex
    result = await db[CONNECTIONS].update_one(
        {
            "id": connection_id,
            "refresh_token": refresh_token,
            "$or": [
                {"refresh_lock_until": {"$exists": False}},
                {"refresh_lock_until": {"$lte": now.isoformat()}},
            ],
        },
        {"$set": {
            "refresh_lock_owner": owner,
            "refresh_lock_until": (now + timedelta(seconds=_REFRESH_LOCK_SECONDS)).isoformat(),
        }},
    )
    return owner if getattr(result, "modified_count", 0) == 1 else None


async def _release_refresh(db, connection_id: str, owner: str) -> None:
    try:
        await db[CONNECTIONS].update_one(
            {"id": connection_id, "refresh_lock_owner": owner},
            {"$unset": {"refresh_lock_owner": "", "refresh_lock_until": ""}},
        )
    except Exception as e:  # noqa: BLE001 - the claim expires on its own
        print(f"[TikTokDirect] could not release the refresh claim: {type(e).__name__}")


async def _flag_reconnect_required(db, connection_id: str, refresh_token_used: str) -> bool:
    """Mark the connection as needing the user to reconnect — the same flags the Connected
    Accounts card already reads. Conditional on the refresh token we failed with, so a connection
    someone has since reconnected or refreshed is never flagged by a stale failure; returns True
    only for the call that actually set it, which is what makes the notification go out once."""
    result = await db[CONNECTIONS].update_one(
        {"id": connection_id, "refresh_token": refresh_token_used, "needs_reconnect": {"$ne": True}},
        {"$set": {
            "needs_reconnect": True,
            "reconnect_reason": RECONNECT_MESSAGE,
            "needs_reconnect_since": datetime.now(timezone.utc),
        }},
    )
    return getattr(result, "modified_count", 0) == 1


async def _notify_reconnect_required(connection: dict) -> None:
    """Tell the owner once, through the same in-app notifications the bell reads. Best effort:
    failing to notify must never hide the real error from the caller."""
    user_id = (connection or {}).get("user_id")
    if not user_id:
        return
    try:
        from app.services.NotificationService import notification_service
        await notification_service._log_notification(
            user_id=user_id,
            notification_type="connection_disconnected",
            channel="email",
            subject="Reconnect TikTok",
            status="sent",
            metadata={
                "platform": "tiktok",
                "brand_id": connection.get("brand_id"),
                "account_name": connection.get("account_name"),
                "message": RECONNECT_MESSAGE,
            },
        )
    except Exception as e:  # noqa: BLE001
        print(f"[TikTokDirect] could not send the reconnect notification: {type(e).__name__}")


async def get_valid_tiktok_access_token(db, connection_doc: dict) -> str:
    """Returns a still-valid access_token, refreshing first if it's expired or close to it.

    TikTok replaces the refresh token on every refresh and invalidates the old one, so:
      * only one caller refreshes a connection at a time, across workers — the claim is a
        conditional write on the refresh token we read, and everyone else waits and then
        uses the token that caller saved;
      * the connection is re-read from the database first, because the document the caller
        holds may predate another worker's refresh;
      * the new tokens are written with retries, and kept in memory if the database stays
        unreachable, so a refresh TikTok accepted is not lost.
    Callers never read connection_doc["access_token"] after a refresh without re-fetching."""
    connection_id = connection_doc["id"]
    deadline = datetime.now(timezone.utc) + timedelta(seconds=_REFRESH_WAIT_SECONDS)

    while True:
        pending = _unsaved_tokens.get(connection_id)
        if pending:
            # Tokens TikTok already issued that never reached the database: the database copy is
            # older than these and its refresh token is dead. Try to save them, and use them.
            if await _save_tokens(db, connection_id, pending):
                _unsaved_tokens.pop(connection_id, None)
            current = {**connection_doc, **pending}
        else:
            fresh = await db[CONNECTIONS].find_one({"id": connection_id}, {"_id": 0})
            current = fresh or connection_doc

        if _still_valid(current):
            return current["access_token"]

        if current.get("needs_reconnect"):
            # Already known dead: don't spend a refresh on a token TikTok will refuse again.
            raise TikTokReconnectRequired(RECONNECT_MESSAGE)

        refresh_token = current.get("refresh_token")
        if not refresh_token:
            raise TikTokDirectAPIError("no refresh_token stored on this TikTok connection — reconnect required")

        owner = await _claim_refresh(db, connection_id, refresh_token)
        if owner is None:
            # Someone else is refreshing this connection (or already did): wait for their result.
            if datetime.now(timezone.utc) >= deadline:
                raise TikTokDirectAPIError("TikTok is still refreshing this connection — please try again")
            await asyncio.sleep(_REFRESH_POLL_SECONDS)
            continue

        try:
            data = await _refresh_tokens(refresh_token)
        except TikTokReconnectRequired as dead:
            await _release_refresh(db, connection_id, owner)
            print(f"[TikTokDirect] refresh token rejected ({dead.code}); connection needs reconnecting")
            if await _flag_reconnect_required(db, connection_id, refresh_token):
                await _notify_reconnect_required(current)
            raise TikTokReconnectRequired(RECONNECT_MESSAGE, code=dead.code) from None
        except Exception:
            await _release_refresh(db, connection_id, owner)
            raise
        fields = _token_fields(data, refresh_token)
        if not await _save_tokens(db, connection_id, fields, owner=owner):
            _unsaved_tokens[connection_id] = fields
            print("[TikTokDirect] refreshed tokens are held in memory until the database accepts them")
        return fields["access_token"]


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}


async def _fetch_creator_info(access_token: str) -> dict:
    """TikTok requires this be queried immediately before every publish
    attempt — the app must never hardcode privacy_level, it must pick one of
    the values this endpoint returns for the specific creator (accounts
    belonging to an unaudited/Sandbox app only ever get SELF_ONLY back here;
    a fully audited Production app gets the full set including
    PUBLIC_TO_EVERYONE). Skipping this and guessing a value is exactly what
    trips TikTok's "please review our integration guidelines" catch-all
    rejection on video/init."""
    async def call() -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_API_BASE}/post/publish/creator_info/query/",
                headers=_headers(access_token),
            )
        return _checked(resp, "creator info query")

    # Refused before anything happened, so safe to retry a temporary limit (creator-info is capped
    # at 20/min per token, and the Post to TikTok screen calls it too).
    data = await rl.with_backoff(call, classify=_classify)
    return data.get("data") or {}


async def resolve_tiktok_connection(db, user_id: str):
    """Which TikTok connection a post should use, from what the user has connected: a
    direct TikTok-login connection if there is one (posted with the Content Posting API
    directly), otherwise an Outstand-connected account (posted through Outstand).
    Returns ("direct" | "outstand" | None, connection document | None)."""
    direct = await db[CONNECTIONS].find_one(
        {"user_id": user_id, "platform": "tiktok", "connected_via": "tiktok_direct_oauth", "connection_status": "active"},
        {"_id": 0},
    )
    if direct:
        return "direct", direct
    via_outstand = await db[CONNECTIONS].find_one(
        {"user_id": user_id, "platform": "tiktok", "connected_via": "outstand"},
        {"_id": 0},
    )
    if via_outstand and via_outstand.get("outstand_account_id"):
        return "outstand", via_outstand
    return None, None


async def fetch_creator_info(access_token: str) -> dict:
    """Creator info for the "Post to TikTok" screen. TikTok's Content Sharing
    Guidelines require it be fetched fresh every time that screen renders, so
    the user sees which account they're posting to and only the privacy
    levels / interactions that account actually allows."""
    return await _fetch_creator_info(access_token)


# TikTok's cap on post_info.title for video posts.
_MAX_TITLE_LENGTH = 2200
# Photo posts have tighter text limits than video: title 90, description 4000.
_MAX_PHOTO_TITLE_LENGTH = 90
_MAX_PHOTO_DESCRIPTION_LENGTH = 4000


@dataclass
class TikTokPostSettings:
    """What the user chose on the Post to TikTok screen. Per TikTok's Content
    Sharing Guidelines none of this may be defaulted on the user's behalf —
    privacy_level has no default, and every interaction starts disabled."""
    title: str
    privacy_level: str
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    # Commercial content disclosure. brand_organic = "Your brand" (labelled
    # Promotional content); brand_content = "Branded content" (labelled Paid
    # partnership). Both off means no disclosure.
    brand_organic: bool = False
    brand_content: bool = False
    # Photo posts only: the caption shown under the photo(s). Video posts carry
    # their text in `title`.
    description: str = ""


def _validate_settings(settings_: TikTokPostSettings, creator_info: dict, photo: bool = False) -> None:
    """Server-side re-check of what the UI already enforces, so a crafted
    request can't post with a privacy level the creator doesn't offer or
    turn on an interaction the creator disabled in their TikTok settings.
    photo=True applies the photo-post rules: shorter title, a description
    limit, and no Duet/Stitch (they don't apply to photos)."""
    options = creator_info.get("privacy_level_options") or []
    if not options:
        raise TikTokDirectAPIError(
            "This TikTok account can't be posted to right now. Please try again later."
        )
    if settings_.privacy_level not in options:
        raise TikTokDirectAPIError(
            f"Privacy level {settings_.privacy_level!r} isn't available for this TikTok account."
        )
    # Only "Branded content" (Paid partnership) is barred from private; "Your
    # brand" (Promotional content) on its own may still be Only me.
    if settings_.brand_content and settings_.privacy_level == "SELF_ONLY":
        raise TikTokDirectAPIError("Branded content visibility cannot be set to private.")
    max_title = _MAX_PHOTO_TITLE_LENGTH if photo else _MAX_TITLE_LENGTH
    if len(settings_.title or "") > max_title:
        raise TikTokDirectAPIError(f"Title is too long (max {max_title} characters).")
    if photo and len(settings_.description or "") > _MAX_PHOTO_DESCRIPTION_LENGTH:
        raise TikTokDirectAPIError(
            f"Description is too long (max {_MAX_PHOTO_DESCRIPTION_LENGTH} characters)."
        )
    if photo and (settings_.allow_duet or settings_.allow_stitch):
        raise TikTokDirectAPIError("Duet and Stitch don't apply to photo posts.")
    if settings_.allow_comment and creator_info.get("comment_disabled"):
        raise TikTokDirectAPIError("Comments are disabled in this creator's TikTok settings.")
    if settings_.allow_duet and creator_info.get("duet_disabled"):
        raise TikTokDirectAPIError("Duet is disabled in this creator's TikTok settings.")
    if settings_.allow_stitch and creator_info.get("stitch_disabled"):
        raise TikTokDirectAPIError("Stitch is disabled in this creator's TikTok settings.")


def schedule_problem(settings: Optional[dict], image_count: int) -> Optional[str]:
    """Why a TikTok photo post can't be scheduled with these choices, or None. Checks only what is
    known when scheduling: whether this account still offers the chosen privacy level is checked again
    when the post goes out (_validate_settings), against creator_info fetched then."""
    if image_count == 0:
        return "This post has no image to publish."
    if not settings or not settings.get("privacy_level"):
        # TikTok: no default, the user must choose. A schedule with no choice can't be published later.
        return "Choose who can view this TikTok post before scheduling it."
    if settings.get("brand_content") and settings["privacy_level"] == "SELF_ONLY":
        return "Branded content visibility cannot be set to private."
    if len(settings.get("title") or "") > _MAX_PHOTO_TITLE_LENGTH:
        return f"Title is too long (max {_MAX_PHOTO_TITLE_LENGTH} characters)."
    if len(settings.get("description") or "") > _MAX_PHOTO_DESCRIPTION_LENGTH:
        return f"Description is too long (max {_MAX_PHOTO_DESCRIPTION_LENGTH} characters)."
    return None


async def publish_tiktok_direct(
    access_token: str,
    video_url: str,
    caption: str,
    settings_: Optional[TikTokPostSettings] = None,
) -> tuple[str, str]:
    """Downloads the video, uploads it to TikTok via FILE_UPLOAD, and polls
    until the post finishes. Returns (publish_id, status). Raises
    TikTokDirectAPIError on any failure — a publish either succeeds or the
    caller finds out why, never a silent no-op.

    settings_ carries the user's choices from the Post to TikTok screen. When
    it's omitted (callers that don't have that screen yet, e.g. the draft
    approval path) the legacy behaviour applies: public if allowed, otherwise
    the first level creator_info offers."""
    creator_info = await _fetch_creator_info(access_token)
    if settings_ is not None:
        _validate_settings(settings_, creator_info)
        post_info = {
            "title": settings_.title or "",
            "privacy_level": settings_.privacy_level,
            "disable_comment": not settings_.allow_comment,
            "disable_duet": not settings_.allow_duet,
            "disable_stitch": not settings_.allow_stitch,
        }
        if settings_.brand_organic or settings_.brand_content:
            post_info["brand_organic_toggle"] = settings_.brand_organic
            post_info["brand_content_toggle"] = settings_.brand_content
    else:
        privacy_options = creator_info.get("privacy_level_options") or []
        if not privacy_options:
            raise TikTokDirectAPIError(f"creator_info returned no privacy_level_options: {creator_info}")
        # Prefer public — this is an organic post, not a private test upload —
        # but fall back to whatever the account/app combo actually allows
        # (e.g. Sandbox and not-yet-audited Production apps only get SELF_ONLY,
        # and unaudited clients of any kind are further restricted to posting
        # only to accounts whose TikTok profile is itself set to Private).
        privacy_level = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in privacy_options else privacy_options[0]
        post_info = {
            "title": caption or "",
            "privacy_level": privacy_level,
            "disable_duet": bool(creator_info.get("duet_disabled")),
            "disable_comment": bool(creator_info.get("comment_disabled")),
            "disable_stitch": bool(creator_info.get("stitch_disabled")),
        }

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

    async def init_call() -> dict:
        async with httpx.AsyncClient(timeout=120) as client:
            init_resp = await client.post(
                f"{_API_BASE}/post/publish/video/init/",
                headers=_headers(access_token),
                json={
                    "post_info": post_info,
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": video_size,
                        "chunk_size": chunk_size,
                        "total_chunk_count": total_chunks,
                    },
                },
            )
        return _checked(init_resp, "video init")

    # A temporary limit on init means TikTok refused it before creating anything, so retrying
    # cannot double-post. 6 inits/min per user token is TikTok's documented cap.
    init_data = await rl.with_backoff(init_call, classify=_classify)
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

            async def put_chunk(chunk=chunk, start=start, end=end, i=i) -> None:
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
                    message = (f"chunk {i + 1}/{total_chunks} upload failed: HTTP {put_resp.status_code} "
                               f"{put_resp.text[:300]}")
                    info = rl.classify_tiktok(None, status=put_resp.status_code,
                                              headers=getattr(put_resp, "headers", None))
                    if info:
                        raise TikTokRateLimitError(message, info, code=info.code)
                    raise TikTokDirectAPIError(message, code=f"http_{put_resp.status_code}")

            # The same bytes to the same range, so a refused (rate-limited) attempt is safe to repeat.
            await rl.with_backoff(put_chunk, classify=_classify)

    return await _wait_for_publish(access_token, publish_id)


async def _wait_for_publish(access_token: str, publish_id: str) -> tuple[str, str]:
    """Poll publish status until it settles. Endpoint path per TikTok's Content
    Posting API reference — third-party sources disagree on the exact path,
    so this needs confirming against a live account before this is fully
    trusted (see module docstring). TikTok's own guidelines ask that users be
    able to see the status of their post, which the caller surfaces."""
    status = "PROCESSING_DOWNLOAD"
    fail_reason = ""
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(30):  # ~2.5 min max at 5s intervals
            status_resp = await client.post(
                f"{_API_BASE}/post/publish/status/fetch/",
                headers=_headers(access_token),
                json={"publish_id": publish_id},
            )
            try:
                status_data = _checked(status_resp, "publish status")
            except TikTokRateLimitError as e:
                # The post was already accepted: being told to slow down on a status check is not a
                # failure of the post. Wait out the limit (counts as a missed poll) and keep going.
                await asyncio.sleep(min(e.rate_limit.retry_after_s or _STATUS_LIMIT_WAIT_S, 60.0))
                continue
            inner = status_data.get("data") or {}
            status = inner.get("status", status)
            fail_reason = inner.get("fail_reason") or fail_reason
            if status in ("PUBLISH_COMPLETE", "FAILED"):
                break
            await asyncio.sleep(5)

    if status == "FAILED":
        detail = f": {fail_reason}" if fail_reason else ""
        raise TikTokDirectAPIError(f"TikTok reported publish failure for publish_id={publish_id}{detail}")

    if status != "PUBLISH_COMPLETE":
        still = TikTokStillProcessingError(
            "TikTok is still processing this post. It can take a few minutes — check your TikTok profile shortly."
        )
        # TikTok already has the post. A caller that can't show "processing" (the scheduler) needs
        # this id to record it, because publishing again would post it twice.
        still.publish_id = publish_id
        raise still

    return publish_id, status


async def publish_tiktok_photo_direct(
    access_token: str,
    image_urls: list[str],
    settings_: TikTokPostSettings,
    auto_add_music: bool = False,
) -> tuple[str, str]:
    """Direct-posts a photo (single image or carousel) via the Content Posting
    API's photo endpoint. Unlike video there is no file upload for photos —
    TikTok pulls each image from a URL, and that URL must sit under a prefix
    verified in the Developer Portal (otherwise url_ownership_unverified), which
    is why callers hand this signed URLs from our own API, not Cloudinary's.

    Returns (publish_id, status); raises TikTokDirectAPIError on any failure."""
    if not image_urls:
        raise TikTokDirectAPIError("No images to post.")
    creator_info = await _fetch_creator_info(access_token)
    _validate_settings(settings_, creator_info, photo=True)

    post_info = {
        "title": settings_.title or "",
        "description": settings_.description or "",
        "privacy_level": settings_.privacy_level,
        "disable_comment": not settings_.allow_comment,
        "auto_add_music": bool(auto_add_music),
    }
    if settings_.brand_organic or settings_.brand_content:
        post_info["brand_organic_toggle"] = settings_.brand_organic
        post_info["brand_content_toggle"] = settings_.brand_content

    async def init_call() -> dict:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{_API_BASE}/post/publish/content/init/",
                headers=_headers(access_token),
                json={
                    "media_type": "PHOTO",
                    "post_mode": "DIRECT_POST",
                    "post_info": post_info,
                    "source_info": {
                        "source": "PULL_FROM_URL",
                        "photo_cover_index": 0,
                        "photo_images": image_urls,
                    },
                },
            )
        return _checked(resp, "photo init")

    data = await rl.with_backoff(init_call, classify=_classify)
    publish_id = (data.get("data") or {}).get("publish_id")
    if not publish_id:
        raise TikTokDirectAPIError(f"photo init did not return a publish_id: {data}")

    return await _wait_for_publish(access_token, publish_id)
