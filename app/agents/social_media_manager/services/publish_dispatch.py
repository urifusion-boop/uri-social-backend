"""
Sending a draft to a platform through the per-account gate — shared by the immediate Approve path
and the scheduled-publish cron (approval_workflow_service), kept out of that very large file so it
can be read and tested on its own.

What it does around the actual publish call:

  * one post at a time per connected account, with a minimum gap before the next (rate_limit.AccountGate);
  * if the account is busy, spaced out, or paused after a rate limit, the draft is DEFERRED — put back to
    its scheduled status with `next_attempt_at` — rather than failed, and the scheduler picks it up once
    that time has passed. Nothing here sleeps for long: the Approve request waits a few seconds at most,
    and the cron tick (bounded to 300s) does not wait at all;
  * if the platform answers with a temporary rate limit / throttle window, the account is paused and the
    draft deferred too; a daily or app-wide CAP is not deferred (retrying cannot help) — the failure keeps
    its clear message and is marked so the scheduler's retry-reset leaves it alone.

A deferral never counts as a failure: no `publish_retry_count`, no `total_publish_errors`, no
"consecutive failures" toward telling the user to reconnect.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from app.agents.social_media_manager.services import rate_limit as rl

# A cap is retried by nobody: the scheduler's retry-reset skips drafts carrying one of these.
CAP_KINDS = ("daily_cap", "app_cap")
# How many scheduler retries a capped draft is treated as having used (the reset retries below 3).
EXHAUSTED_RETRIES = 3


def scheduled_status() -> str:
    """The status a draft waits in until the cron picks it up — the same environment split
    approve_content and publish_scheduled_content use (staging must not be claimed by the prod cron)."""
    return "staging_scheduled" if os.getenv("APP_ENV", "production") != "production" else "scheduled"


def _naive_utc(value: datetime) -> datetime:
    """Datetimes in content_drafts are naive UTC throughout this codebase."""
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


@dataclass
class Dispatch:
    result: Optional[Dict[str, Any]] = None     # what _publish_to_platform returned, when it ran
    deferred: bool = False
    resume_at: Optional[datetime] = None
    info: Optional["rl.RateLimitInfo"] = None
    message: str = ""


def limit_info_of(result: Optional[Dict[str, Any]], platform: str) -> Optional["rl.RateLimitInfo"]:
    """The rate limit a FAILED publish result describes, or None. Prefers the typed info a platform
    call attached; falls back to reading the error text, which is all Outstand gives us."""
    if not result or result.get("success"):
        return None
    info = result.get("rate_limit")
    if isinstance(info, rl.RateLimitInfo):
        return info
    return rl.classify_text(platform, result.get("error"))


def queued_message(info: Optional["rl.RateLimitInfo"], resume_at: Optional[datetime], reason: str = "") -> str:
    when = f" around {resume_at.astimezone(timezone.utc).strftime('%H:%M UTC')}" if resume_at else " shortly"
    name = rl._label(info.platform) if info else "the platform"
    if reason in ("busy", "spaced"):
        return f"Queued: another post to this account is going out first, so this will post{when}."
    return f"Queued: {name} is limiting how fast we can post, so this will post{when}."


async def dispatch(db, platform: str, connection: Optional[Dict[str, Any]],
                   publish: Callable[[], Awaitable[Dict[str, Any]]], *, wait_max_s: float = 0.0,
                   wait_through_pause: bool = False) -> Dispatch:
    """Run publish() as the only post in flight for this account. See the module docstring."""
    key, gap, network = rl.gate_target_for_connection(platform, connection)
    if not key:
        result = await publish()
        return Dispatch(result=result, info=limit_info_of(result, platform))

    cfg = rl.get_settings()
    owner = rl.new_owner()
    decision = await rl.wait_for(db, key, owner, max_wait_s=wait_max_s, lease_s=cfg.lease_s,
                                 wait_through_pause=wait_through_pause)
    if not decision.granted:
        paused = decision.reason == "paused"
        info = rl.RateLimitInfo(kind=rl.LimitKind.WINDOW if paused else rl.LimitKind.TEMPORARY, platform=network,
                                code=decision.pause_code or decision.reason, resume_at=decision.retry_at)
        return Dispatch(deferred=True, resume_at=decision.retry_at, info=info,
                        message=queued_message(info, decision.retry_at, decision.reason or ""))

    try:
        result = await publish()
    finally:
        await rl.release(db, key, owner, min_gap_s=gap)

    info = limit_info_of(result, platform)
    if result and result.get("success"):
        await rl.clear_backoff(db, key)
    elif info and info.retryable:
        resume = await rl.pause(db, key, until=info.resume_at or (
            rl.utcnow() + timedelta(seconds=info.retry_after_s) if info.retry_after_s else None),
            code=info.code, reason=str(result.get("error") or "")[:300])
        return Dispatch(result=result, deferred=True, resume_at=resume, info=info,
                        message=queued_message(info, resume))
    return Dispatch(result=result, info=info)


def deferral_fields(draft: Dict[str, Any], resume_at: datetime, info: Optional["rl.RateLimitInfo"], message: str,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    """The draft's `$set` for a deferral: back to its scheduled status, due now, but not to be picked
    up before `next_attempt_at`. Deliberately leaves `publish_retry_count` alone."""
    now = _naive_utc(now or datetime.utcnow())
    existing = draft.get("scheduled_date")
    due = existing if isinstance(existing, datetime) and _naive_utc(existing) <= now else now
    return {
        "status": scheduled_status(),
        "scheduled_date": due,
        "next_attempt_at": _naive_utc(resume_at),
        "publish_wait": {
            "reason": "rate_limited" if info and info.retryable and info.code not in ("busy", "spaced") else "account_busy",
            "code": info.code if info else None,
            "resume_at": _naive_utc(resume_at),
            "message": message,
        },
        "updated_at": now,
    }


# `$unset` for a draft that has now gone out (or failed for good): it is no longer waiting for anything.
CLEAR_WAIT = {"next_attempt_at": "", "publish_wait": ""}


def cap_fields(info: Optional["rl.RateLimitInfo"]) -> Dict[str, Any]:
    """Extra `$set` for a draft that failed on a daily / app-wide cap: tags it, and marks its retries
    spent so the scheduler's retry-reset never re-sends it (retrying a cap only burns the limit)."""
    if info and not info.retryable:
        return {"publish_error_kind": info.error_code, "publish_retry_count": EXHAUSTED_RETRIES}
    return {}


def queued_result(d: Dispatch) -> Dict[str, Any]:
    """What an immediate Approve reports for a deferred draft: not a success and not a failure.
    `error` carries the same words so an older app that only knows success/failure still says
    something sensible."""
    return {
        "success": False,
        "queued": True,
        "publish_status": "queued",
        "resume_at": d.resume_at.astimezone(timezone.utc).isoformat() if d.resume_at else None,
        "message": d.message,
        "error": d.message,
    }


def not_waiting(now: datetime) -> Dict[str, Any]:
    """Mongo condition: this draft is not waiting for a later time (no `next_attempt_at`, or it has passed)."""
    return {"$or": [{"next_attempt_at": {"$exists": False}}, {"next_attempt_at": None},
                    {"next_attempt_at": {"$lte": now}}]}


# ── Which Facebook connection a post goes through ─────────────────────────────

def facebook_direct_enabled() -> bool:
    """A Facebook Page connected directly with Meta posts straight to the Page (Graph API). On by
    default; FACEBOOK_POST_DIRECT=false restores the older behaviour of sending those posts through
    Outstand when it has the Page, without a deploy."""
    return os.getenv("FACEBOOK_POST_DIRECT", "true").strip().lower() not in ("0", "false", "no", "off")


def posts_directly(platform: str, connection: Optional[Dict[str, Any]]) -> bool:
    """True when this post should go to Facebook itself: a direct Meta connection that holds the
    Page and its token. The direct result is a plain Graph post id (pageId_postId) with no `via`, so
    nothing polls Outstand for it. That polling was what once produced duplicate posts."""
    c = connection or {}
    return bool(
        facebook_direct_enabled()
        and (platform or "").lower() == "facebook"
        and c.get("connected_via") == "facebook_direct_oauth"
        and c.get("page_id")
        and c.get("page_access_token")
    )


async def preferred_direct_facebook(db, user_id: str, brand_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The user's direct Facebook connection, to use ahead of the newest-connection pick (which would
    otherwise choose a newer Outstand one). A draft that belongs to a brand only ever uses that brand's
    Page, or a connection not tied to any brand; it never borrows another brand's Page."""
    if not facebook_direct_enabled() or db is None or not user_id:
        return None
    base = {"user_id": user_id, "platform": "facebook", "connected_via": "facebook_direct_oauth",
            "connection_status": "active", "page_id": {"$exists": True, "$ne": None},
            "page_access_token": {"$exists": True, "$ne": None}}
    if brand_id:
        found = await db["social_connections"].find_one({**base, "brand_id": brand_id})
        if found:
            return found
        return await db["social_connections"].find_one(
            {**base, "$or": [{"brand_id": {"$exists": False}}, {"brand_id": None}]})
    return await db["social_connections"].find_one(base)
