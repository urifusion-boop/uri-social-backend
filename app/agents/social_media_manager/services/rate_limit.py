"""
Rate limits for posting to TikTok and Meta (Instagram / Facebook), in one place.

Three small pieces, none of which imports platform code:

1. Classification — turn a platform's error (TikTok error code, Meta error code/headers, or
   just text from Outstand) into a RateLimitInfo: is this a *temporary* limit worth retrying,
   a *window* we should pause an account for until it resets, or a *cap* (daily / app-wide)
   that must never be retried? Everything user-facing about a limit comes from here, so the
   wording is consistent and a limit is never mistaken for "reconnect your account".

2. AccountGate — a Mongo-backed gate per connected account: one post at a time, a minimum gap
   between posts, and a "paused until" time after a limit. It has to live in Mongo, not in
   memory, because the API runs several uvicorn workers plus a scheduler thread.

3. with_backoff — retry only temporary errors, honouring Retry-After, within a time budget.
   It is for calls inside a request or background job. It is never used inside the scheduler
   tick (that tick is bounded to 300s, see notification_scheduler._run_async); there a wait is
   recorded as a timestamp and the draft is picked up again later.

Verified limits this is built around (TikTok docs / Outstand's published TikTok limits, and
Meta's Graph API rate-limiting page):
  TikTok: post-init 6/min per user token, status fetch 30/min per user, creator-info 20/min per
          token; `rate_limit_exceeded` (HTTP 429) is temporary; `spam_risk_too_many_posts` is the
          account's daily cap and `reached_active_user_cap` the app-wide creator cap — neither is
          worth retrying.
  Meta:   error 4 (app), 17 (user), 32 (Pages, user token), 613 (custom; subcode 1996 =
          inconsistent volume), 80001 (Pages, page/system token), 80002 (Instagram). The
          X-Business-Use-Case-Usage header carries estimated_time_to_regain_access in MINUTES.
          Meta says to stop calling once limited — more calls extend the lockout — and gives no
          retry schedule.

NOT verified, so deliberately not encoded as numbers here: Instagram's posts-per-24h publishing
quota (check Meta's content_publishing_limit docs before adding a pre-check), and the exact
strings Outstand uses to report a limit (classify_text matches loosely and the raw text is kept).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

GATE_COLLECTION = "publish_account_gate"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: Any) -> Optional[datetime]:
    """A datetime (naive ones are UTC, which is what Mongo hands back) or an ISO string, as an
    aware UTC datetime; None when there is nothing usable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


# ── Settings ──────────────────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class RateLimitSettings:
    """Every number here is a tunable, not a platform fact. tiktok_min_gap_s follows from TikTok's
    verified 6 post-inits per minute; meta_min_gap_s has no verified basis, it is an ops choice."""
    tiktok_min_gap_s: float
    meta_min_gap_s: float
    # How long one post may hold an account. It must outlast a whole publish: a TikTok upload plus
    # up to ~2.5 min of status polling, or an Outstand post polled for up to ~12 min.
    lease_s: float
    # Longest we will sleep inside one background job before deferring instead.
    inline_retry_budget_s: float
    # Longest the Approve request waits for a busy account before answering "queued".
    immediate_wait_max_s: float
    # Longest a background publish job waits for its account (a busy lease, the gap, or a pause)
    # before it gives up and tells the user when to try again.
    job_wait_max_s: float


def get_settings() -> RateLimitSettings:
    # Read on every call so a changed env var (or a test) takes effect without a restart dance.
    return RateLimitSettings(
        tiktok_min_gap_s=_env_float("TIKTOK_MIN_GAP_S", 12.0),
        meta_min_gap_s=_env_float("META_MIN_GAP_S", 5.0),
        lease_s=_env_float("PUBLISH_GATE_LEASE_S", 900.0),
        inline_retry_budget_s=_env_float("PUBLISH_INLINE_RETRY_BUDGET_S", 60.0),
        immediate_wait_max_s=_env_float("PUBLISH_IMMEDIATE_WAIT_MAX_S", 20.0),
        job_wait_max_s=_env_float("PUBLISH_JOB_WAIT_MAX_S", 600.0),
    )


# ── Classification ────────────────────────────────────────────────────────────

class LimitKind(str, Enum):
    TEMPORARY = "temporary"   # a per-minute style limit: wait a moment and retry
    WINDOW = "window"         # a longer throttle window (Meta): pause the account until it resets
    DAILY_CAP = "daily_cap"   # the account's daily cap: never retry
    APP_CAP = "app_cap"       # an app-wide cap (TikTok's distinct-creator limit): never retry


@dataclass(frozen=True)
class RateLimitInfo:
    kind: LimitKind
    platform: str
    code: Optional[str] = None
    retry_after_s: Optional[float] = None
    resume_at: Optional[datetime] = None
    user_message: str = ""

    @property
    def retryable(self) -> bool:
        return self.kind in (LimitKind.TEMPORARY, LimitKind.WINDOW)

    @property
    def error_code(self) -> str:
        """The short code the frontend switches on (stored on jobs and drafts)."""
        return {
            LimitKind.TEMPORARY: "rate_limited",
            LimitKind.WINDOW: "rate_limited",
            LimitKind.DAILY_CAP: "daily_cap",
            LimitKind.APP_CAP: "app_cap",
        }[self.kind]


class RateLimitedError(Exception):
    """A platform call was refused because of a rate limit. `.rate_limit` says which kind."""

    def __init__(self, message: str, info: RateLimitInfo) -> None:
        super().__init__(message)
        self.rate_limit = info


_PLATFORM_LABEL = {"tiktok": "TikTok", "instagram": "Instagram", "facebook": "Facebook", "meta": "Meta"}

# Meta error codes that mean "throttled" (Meta's rate-limiting doc). Instagram-specific codes beyond
# these are not verified, so they are not listed — extend this set once checked.
META_RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80001, 80002})

# Where to wait when Meta gives no time: grows with each consecutive limit on the account.
DEFAULT_PAUSE_MINUTES = (5, 15, 60)

_MARGIN_S = 60.0  # a little past Meta's estimate, so we don't land on the exact second it reopens


def _label(platform: str) -> str:
    return _PLATFORM_LABEL.get((platform or "").lower(), (platform or "The network").title())


def _clock(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M UTC")


def friendly_message(info: RateLimitInfo, now: Optional[datetime] = None) -> str:
    """What to tell the user. Never promises a reset time the platform didn't give."""
    name = _label(info.platform)
    if info.kind == LimitKind.DAILY_CAP:
        return (f"This {name} account has reached its daily posting limit. Try again later — "
                f"{name} doesn't say exactly when it resets.")
    if info.kind == LimitKind.APP_CAP:
        return (f"{name} limits how many accounts URI can post for in a day, and that limit has been reached "
                f"for now. Please try again tomorrow.")
    when = ""
    if info.resume_at:
        when = f" We'll try again around {_clock(info.resume_at)}."
    elif info.retry_after_s:
        when = f" We'll try again in about {max(1, round(info.retry_after_s))} seconds."
    return f"{name} is limiting how fast we can post right now.{when}"


def give_up_message(info: RateLimitInfo, now: Optional[datetime] = None) -> str:
    """For a job that stopped waiting: what happened and when to try again. Caps keep their own
    wording (there is no time to give); a limit that outlasted our patience names the time."""
    if not info.retryable:
        return info.user_message or friendly_message(info, now)
    name = _label(info.platform)
    when = f" Please try again after {_clock(info.resume_at)}." if info.resume_at else " Please try again in a few minutes."
    return f"{name} is limiting how fast we can post for this account right now.{when}"


def parse_retry_after(headers: Any) -> Optional[float]:
    """Seconds from a Retry-After header (a number of seconds, or an HTTP date); None if absent."""
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:  # noqa: BLE001 - not a mapping
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, (as_utc(when) - utcnow()).total_seconds())


def parse_estimated_regain_minutes(headers: Any) -> Optional[float]:
    """The longest estimated_time_to_regain_access (minutes) in Meta's X-Business-Use-Case-Usage
    header, or None. The header is JSON keyed by business id, each value a list of usage objects."""
    if not headers:
        return None
    try:
        raw = headers.get("X-Business-Use-Case-Usage") or headers.get("x-business-use-case-usage")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    best: Optional[float] = None
    if isinstance(parsed, dict):
        for usages in parsed.values():
            for usage in usages if isinstance(usages, list) else []:
                eta = usage.get("estimated_time_to_regain_access") if isinstance(usage, dict) else None
                if isinstance(eta, (int, float)) and eta > 0 and (best is None or eta > best):
                    best = float(eta)
    return best


def _info(kind: LimitKind, platform: str, code: Optional[str], *, retry_after_s: Optional[float] = None,
          resume_at: Optional[datetime] = None, now: Optional[datetime] = None) -> RateLimitInfo:
    info = RateLimitInfo(kind=kind, platform=platform, code=code, retry_after_s=retry_after_s, resume_at=resume_at)
    return RateLimitInfo(kind=kind, platform=platform, code=code, retry_after_s=retry_after_s,
                         resume_at=resume_at, user_message=friendly_message(info, now))


def classify_tiktok(code: Optional[str], message: str = "", status: Optional[int] = None,
                    headers: Any = None) -> Optional[RateLimitInfo]:
    """A TikTok API error as a rate limit, or None when it is not one."""
    code = (code or "").strip()
    if code == "spam_risk_too_many_posts":
        return _info(LimitKind.DAILY_CAP, "tiktok", code)
    if code == "reached_active_user_cap":
        return _info(LimitKind.APP_CAP, "tiktok", code)
    if code == "rate_limit_exceeded" or (status == 429 and not code):
        retry_after = parse_retry_after(headers)
        return _info(LimitKind.TEMPORARY, "tiktok", code or "rate_limit_exceeded", retry_after_s=retry_after)
    return None


def classify_meta(status: Optional[int], error_body: Optional[Dict[str, Any]] = None, headers: Any = None,
                  platform: str = "meta", now: Optional[datetime] = None) -> Optional[RateLimitInfo]:
    """A Meta Graph API error as a rate limit, or None. `error_body` is the `error` object."""
    now = now or utcnow()
    body = error_body or {}
    try:
        code = int(body.get("code")) if body.get("code") is not None else None
    except (TypeError, ValueError):
        code = None
    if code not in META_RATE_LIMIT_CODES and status != 429:
        return None
    eta_min = parse_estimated_regain_minutes(headers)
    retry_after = parse_retry_after(headers)
    if eta_min is not None:
        wait_s = eta_min * 60.0 + _MARGIN_S
    elif retry_after is not None:
        wait_s = retry_after
    else:
        wait_s = None  # the gate picks a default that grows with consecutive limits
    return _info(LimitKind.WINDOW, platform, str(code) if code is not None else "429",
                 retry_after_s=wait_s, resume_at=(now + timedelta(seconds=wait_s)) if wait_s else None, now=now)


def meta_limit_fields(resp: Any, data: Any, platform: str = "instagram") -> Dict[str, Any]:
    """`{"rate_limit": RateLimitInfo}` when a Graph API response (its HTTP response and parsed JSON)
    is a rate limit, else `{}` — spread into a failure result so the caller can defer or explain
    instead of showing a raw Meta error. Reads the error code and, for the wait, the usage headers."""
    error = data.get("error") if isinstance(data, dict) else None
    info = classify_meta(getattr(resp, "status_code", None), error if isinstance(error, dict) else None,
                         getattr(resp, "headers", None), platform=platform)
    return {"rate_limit": info} if info else {}


_TIKTOK_TEXT_CODES = ("spam_risk_too_many_posts", "reached_active_user_cap", "rate_limit_exceeded")
_META_TEXT_PATTERNS = (
    "application request limit reached", "user request limit reached", "request limit reached",
    "calls to this api have exceeded the rate limit", "too many calls", "(#4)", "(#17)", "(#32)",
    "(#613)", "(#80001)", "(#80002)",
)


def classify_text(platform: str, raw: Optional[str]) -> Optional[RateLimitInfo]:
    """Loose match on error TEXT, for failures that reach us as strings (Outstand, wrapped Meta
    messages). Deliberately forgiving — the exact wording Outstand uses is not verified — and the
    raw text is always kept by the caller."""
    low = " ".join(str(raw or "").split()).lower()
    if not low:
        return None
    platform = (platform or "").lower()
    for code in _TIKTOK_TEXT_CODES:
        if code in low:
            return classify_tiktok(code)
    if platform in ("instagram", "facebook", "meta") and any(p in low for p in _META_TEXT_PATTERNS):
        return _info(LimitKind.WINDOW, platform, None)
    if re.search(r"\b429\b", low) or "rate limit" in low or "too many requests" in low:
        kind = LimitKind.WINDOW if platform in ("instagram", "facebook", "meta") else LimitKind.TEMPORARY
        return _info(kind, platform, "429")
    return None


def classify_exception(exc: BaseException, platform: str) -> Optional[RateLimitInfo]:
    """Whatever this exception says about rate limits: its own `.rate_limit`, else its error
    `.code` (TikTok), else its text."""
    info = getattr(exc, "rate_limit", None)
    if isinstance(info, RateLimitInfo):
        return info
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        found = classify_tiktok(code)
        if found:
            return found
    return classify_text(platform, str(exc))


def http_limit_response(exc: BaseException, platform: str,
                        now: Optional[datetime] = None) -> Optional[tuple]:
    """(status code, message, headers) for an API response when `exc` is a rate limit, else None:
    HTTP 429 with the user-facing message and a Retry-After header when we know the wait."""
    info = classify_exception(exc, platform)
    if not info:
        return None
    now = now or utcnow()
    wait = info.retry_after_s
    if wait is None and info.resume_at:
        wait = max(0.0, (info.resume_at - now).total_seconds())
    headers = {"Retry-After": str(max(1, int(round(wait))))} if wait else {}
    return 429, info.user_message or friendly_message(info, now), headers


# ── Per-account gate ──────────────────────────────────────────────────────────

def account_key(platform: str, identifier: str) -> str:
    """e.g. account_key("tiktok", conn["id"]), ("outstand", outstand_account_id),
    ("instagram", ig_user_id), ("facebook", page_id)."""
    return f"{platform}:{identifier}"


def gate_target_for_connection(platform: str, conn: Optional[Dict[str, Any]]):
    """(gate key, minimum gap in seconds, network) for the account a post goes to, from a draft's
    platform and its social_connections document. The key is None when we can't tell which account
    it is, or for platforms we don't gate (the limits above are TikTok's and Meta's), and then the
    post is simply not gated."""
    cfg = get_settings()
    conn = conn or {}
    p = (platform or "").lower()
    via = conn.get("connected_via") or ""
    if p == "tiktok":
        if via == "tiktok_direct_oauth":
            ident, kind = conn.get("id"), "tiktok"
        else:
            ident, kind = conn.get("outstand_account_id"), "outstand"
        return (account_key(kind, ident) if ident else None), cfg.tiktok_min_gap_s, "tiktok"
    if p == "instagram":
        ident = conn.get("ig_user_id") or conn.get("id")
        return (account_key("instagram", ident) if ident else None), cfg.meta_min_gap_s, "instagram"
    if p == "facebook":
        if via == "outstand" and conn.get("outstand_account_id"):
            ident, kind = conn["outstand_account_id"], "outstand"
        else:
            ident, kind = conn.get("page_id") or conn.get("id"), "facebook"
        return (account_key(kind, ident) if ident else None), cfg.meta_min_gap_s, "facebook"
    return None, 0.0, p


@dataclass(frozen=True)
class GateDecision:
    granted: bool
    reason: Optional[str] = None            # "busy" | "spaced" | "paused"
    retry_at: Optional[datetime] = None
    pause_code: Optional[str] = None
    pause_reason: Optional[str] = None


def evaluate_gate(doc: Optional[Dict[str, Any]], now: datetime, owner: Optional[str] = None) -> GateDecision:
    """Pure decision from a gate document: may `owner` post now? Order matters — a pause beats
    everything, then another owner's live lease, then the minimum gap since the last post."""
    if not doc:
        return GateDecision(True)
    paused_until = as_utc(doc.get("paused_until"))
    if paused_until and paused_until > now:
        return GateDecision(False, "paused", paused_until, doc.get("pause_code"), doc.get("pause_reason"))
    lease_until = as_utc(doc.get("lease_expires_at"))
    if lease_until and lease_until > now and doc.get("lease_owner") != owner:
        return GateDecision(False, "busy", lease_until)
    next_allowed = as_utc(doc.get("next_allowed_at"))
    if next_allowed and next_allowed > now:
        return GateDecision(False, "spaced", next_allowed)
    return GateDecision(True)


def _not_after(field: str, now: datetime) -> Dict[str, Any]:
    return {"$or": [{field: {"$exists": False}}, {field: None}, {field: {"$lte": now}}]}


def new_owner() -> str:
    return uuid.uuid4().hex


async def acquire(db, key: str, owner: str, *, lease_s: Optional[float] = None,
                  now: Optional[datetime] = None) -> GateDecision:
    """Take the account for one post, atomically across every worker. One upsert whose filter only
    matches when the account is free; if the document exists but the filter fails, Mongo's insert
    half of the upsert hits a duplicate _id — which is the "not free" signal (the same idiom as
    notification_scheduler._try_claim_job_run), and we then read the document to say why."""
    now = now or utcnow()
    lease = timedelta(seconds=lease_s if lease_s is not None else get_settings().lease_s)
    free = {
        "_id": key,
        "$and": [
            {"$or": [{"lease_expires_at": {"$exists": False}}, {"lease_expires_at": None},
                     {"lease_expires_at": {"$lte": now}}, {"lease_owner": owner}]},
            _not_after("next_allowed_at", now),
            _not_after("paused_until", now),
        ],
    }
    update = {"$set": {
        "lease_owner": owner,
        "lease_expires_at": now + lease,
        "updated_at": now,
        "expires_at": now + lease + timedelta(days=1),
    }}
    coll = db[GATE_COLLECTION]
    try:
        await coll.find_one_and_update(free, update, upsert=True)
        return GateDecision(True)
    except DuplicateKeyError:
        doc = await coll.find_one({"_id": key})
        decision = evaluate_gate(doc, now, owner)
        # Lost a race a moment ago, but the document now looks free: say "try again in a second"
        # rather than pretend we hold it.
        return decision if not decision.granted else GateDecision(False, "busy", now + timedelta(seconds=1))


async def release(db, key: str, owner: str, *, min_gap_s: float = 0.0, now: Optional[datetime] = None) -> None:
    """Let go of the account and start the minimum gap before the next post. Best-effort: the
    lease expires by itself if this fails."""
    now = now or utcnow()
    gap = timedelta(seconds=max(0.0, min_gap_s))
    try:
        await db[GATE_COLLECTION].update_one(
            {"_id": key, "lease_owner": owner},
            {"$set": {"next_allowed_at": now + gap, "updated_at": now, "expires_at": now + gap + timedelta(days=1)},
             "$unset": {"lease_owner": "", "lease_expires_at": ""}},
        )
    except Exception as e:  # noqa: BLE001 - the lease expires on its own
        print(f"[RateLimit] could not release {key}: {type(e).__name__}")


def default_pause_until(level: int, now: datetime) -> datetime:
    minutes = DEFAULT_PAUSE_MINUTES[min(max(level, 1), len(DEFAULT_PAUSE_MINUTES)) - 1]
    return now + timedelta(minutes=minutes)


async def pause(db, key: str, *, until: Optional[datetime] = None, code: Optional[str] = None,
                reason: str = "", now: Optional[datetime] = None) -> datetime:
    """Stop posting for this account until `until` (or a default that grows with consecutive limits).
    Returns when it resumes."""
    now = now or utcnow()
    coll = db[GATE_COLLECTION]
    doc = await coll.find_one_and_update(
        {"_id": key}, {"$inc": {"backoff_level": 1}, "$set": {"updated_at": now}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )
    level = int((doc or {}).get("backoff_level") or 1)
    resume = until or default_pause_until(level, now)
    await coll.update_one(
        {"_id": key},
        {"$set": {"paused_until": resume, "pause_code": code, "pause_reason": reason,
                  "expires_at": resume + timedelta(days=1)}},
    )
    return resume


async def clear_backoff(db, key: str) -> None:
    """After a post goes through the account is healthy again: the next limit starts small."""
    try:
        await db[GATE_COLLECTION].update_one({"_id": key}, {"$set": {"backoff_level": 0}})
    except Exception as e:  # noqa: BLE001
        print(f"[RateLimit] could not clear backoff for {key}: {type(e).__name__}")


async def wait_for(db, key: str, owner: str, *, max_wait_s: float, lease_s: Optional[float] = None,
                   sleep: Optional[Callable[[float], Awaitable[Any]]] = None,
                   now_fn: Optional[Callable[[], datetime]] = None, wait_through_pause: bool = False,
                   on_wait: Optional[Callable[[GateDecision], Awaitable[Any]]] = None) -> GateDecision:
    """Try to take the account, waiting up to max_wait_s for it to free up or its gap to pass.
    Returns the granted decision, or the last refusal (with retry_at) once waiting would run past
    the limit. A pause is not waited out unless wait_through_pause is set (background jobs set it;
    a request should not sit through a rate-limit window). on_wait is told whenever the expected
    wait changes, so a job can show "waiting until HH:MM"."""
    sleep = sleep or asyncio.sleep   # looked up now, not at import, so it can be patched
    now_fn = now_fn or utcnow
    deadline = now_fn() + timedelta(seconds=max(0.0, max_wait_s))
    reported: Optional[datetime] = None
    while True:
        decision = await acquire(db, key, owner, lease_s=lease_s, now=now_fn())
        if decision.granted:
            return decision
        if decision.reason == "paused" and not wait_through_pause:
            return decision
        now = now_fn()
        if not decision.retry_at or decision.retry_at > deadline or now >= deadline:
            return decision
        if on_wait and decision.retry_at != reported:
            reported = decision.retry_at
            await on_wait(decision)
        await sleep(max(0.2, min((decision.retry_at - now).total_seconds(), 2.0)))


# ── Retry with backoff ────────────────────────────────────────────────────────

async def with_backoff(fn: Callable[[], Awaitable[Any]], *, classify: Callable[[BaseException], Optional[RateLimitInfo]],
                       max_attempts: int = 4, base_s: float = 2.0, cap_s: float = 30.0, jitter: float = 0.25,
                       budget_s: Optional[float] = None,
                       sleep: Optional[Callable[[float], Awaitable[Any]]] = None,
                       rand: Optional[Callable[[], float]] = None,
                       on_retry: Optional[Callable[[int, float, RateLimitInfo], Awaitable[Any]]] = None) -> Any:
    """Await fn(), retrying only TEMPORARY rate limits: honours Retry-After, else exponential
    backoff with jitter, and stops when the total wait would pass budget_s. Anything else — a cap,
    a window, a permanent error — is raised straight away, as a RateLimitedError when it is a limit.

    Only wrap calls that were refused BEFORE doing anything (creator-info, a post's init, a status
    fetch, a chunk upload of the same bytes). Wrapping a whole publish would double-post."""
    budget = get_settings().inline_retry_budget_s if budget_s is None else budget_s
    sleep = sleep or asyncio.sleep   # looked up now, not at import, so it can be patched
    rand = rand or random.random
    spent = 0.0
    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 - classified below, re-raised if not a temporary limit
            info = classify(e)
            if info is None:
                raise
            give_up = info.kind != LimitKind.TEMPORARY or attempt >= max_attempts
            wait = 0.0
            if not give_up:
                wait = info.retry_after_s if info.retry_after_s else min(cap_s, base_s * (2 ** (attempt - 1)))
                wait = max(0.0, wait * (1.0 + jitter * (2.0 * rand() - 1.0)))
                give_up = spent + wait > budget
            if give_up:
                if isinstance(e, RateLimitedError):
                    raise
                raise RateLimitedError(str(e), info) from e
            if on_retry:
                await on_retry(attempt, wait, info)
            await sleep(wait)
            spent += wait
