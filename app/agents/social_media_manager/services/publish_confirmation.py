"""
Plain-language publish failures, and flagging a connection for reconnecting.

When a network refuses a post, failed_on_submit turns its error into words the owner
understands (explain / specific_message), flags the connection when its login has lapsed,
and tells the owner. (Outstand, which used to need a separate "confirming" step, is retired.)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from pymongo import ReturnDocument

from app.agents.social_media_manager.services import rate_limit as _rate_limit

CONFIRMING = "confirming"
SLOW_AFTER = timedelta(minutes=30)
GIVE_UP_AFTER = timedelta(hours=24)

_NETWORK_NAMES = {
    "facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn", "twitter": "X",
    "x": "X", "threads": "Threads", "pinterest": "Pinterest", "youtube": "YouTube", "tiktok": "TikTok",
}

# The network's own words that mean the login it gave URI no longer works.
_RECONNECT_MARKERS = (
    "access token", "session has been invalidated", "session is invalid", "token has expired",
    "token expired", "changed their password", "error validating", "has not authorized",
    "not authorized", "permissions", "permission", "reauthor", "re-authorize", "oauthexception",
    "invalid oauth", "expired",
)

# Signatures with their own precise, accurate explanation — checked before
# the generic _RECONNECT_MARKERS catch-all, so a known-specific cause never
# gets a message that tells the owner to do the wrong thing.
_HIDE_RAW_MARKERS = ("this account needs to be reconnected", "does not have an active subscription",
                     "confirm your identity")


def _identity_check_message(name: str) -> str:
    """Meta's identity checkpoint (Graph error 368 / subcode 4854002) sits on the personal
    Facebook account that manages the Page, and it blocks Instagram posting through that Page
    too — so the fix is always in the Facebook app, whichever network the post was for.
    Reconnecting does not clear it; owners kept trying that."""
    where = " to Instagram" if name == "Instagram" else ""
    return (
        f"Facebook is asking you to confirm your identity before you can post{where} from this Page. "
        "This is a security check on Facebook's side, not a problem with your connection here, so "
        "reconnecting won't fix it. Open the Facebook app on your phone (signed in as the person who "
        "connected this Page) and follow the \u201cConfirm your identity\u201d prompt; it may also be "
        "in your notifications. Once Facebook has confirmed you, publish this post again."
    )

_SPECIFIC_CAUSES = [
    (
        # The account can no longer publish through its old connection (the provider was
        # switched off, or its own subscription lapsed): it needs a direct reconnect.
        # Owners are never told which provider — just what to do.
        "this account needs to be reconnected",
        lambda name: (
            f"Your {name} account needs to be reconnected. Go to Connected Accounts, tap Reconnect next to "
            f"{name}, then publish this post again."
        ),
        True,
    ),
    (
        "does not have an active subscription",
        lambda name: (
            f"Your {name} account needs to be reconnected. Go to Connected Accounts, tap Reconnect next to "
            f"{name}, then publish this post again."
        ),
        True,
    ),
    (
        "confirm your identity",
        _identity_check_message,
        False,  # resolves on its own, not a reconnect-worthy signature
    ),
    (
        # Live-reported: "MEEQAT TRAVELS AND TOURS LTD"'s Facebook post failed
        # with "400 - The user must be an administrator, editor, or moderator
        # of the page in order to impersonate it." The connected account lost
        # its admin/editor/moderator role on the Page itself (removed, role
        # changed, Page ownership changed) — the token can still be
        # technically valid, so this is NOT a dead session, and the generic
        # "reconnect" message is actively misleading here: reconnecting with
        # the same now-demoted account hits this identical error again.
        "administrator, editor, or moderator of the page",
        lambda name: (
            f"{name} says the connected account is no longer an admin, editor or moderator of this Page, so "
            f"it can't post there. Ask whoever manages the Page to restore that access, or go to Connected "
            f"Accounts and reconnect {name} using an account that already has it."
        ),
        True,  # does need reconnecting (or a role fix), just not the generic wording
    ),
]


def specific_message(platform: str, raw: str) -> Optional[str]:
    """The friendly message for an error that has its own precise explanation (identity check,
    lost Page role, ...), or None. For callers that otherwise show the network's own text and
    shouldn't have the generic reconnect wording forced on them."""
    info = explain(platform, raw)
    low = " ".join(str(raw or "").split()).lower()
    if any(marker in low for marker, _, _ in _SPECIFIC_CAUSES):
        return info["message"]
    return None


def network_name(platform: str) -> str:
    return _NETWORK_NAMES.get((platform or "").lower(), (platform or "the network").title())


def explain(platform: str, raw: str, limit: Optional["_rate_limit.RateLimitInfo"] = None) -> Dict[str, Any]:
    """The network's error in words an owner can act on, whether the account
    needs reconnecting right now, and which category it falls in:
    "known" (a catalogued signature, always escalates immediately),
    "content" (a one-off issue with this post, e.g. a bad image — never
    escalates), or "unknown" (not catalogued at all — does not escalate on a
    single occurrence, but see _should_flag_connection for why it isn't
    ignored forever either). Every category gets a plain-language message
    immediately, on the first occurrence — only whether the whole connection
    gets flagged for reconnect is more cautious."""
    name = network_name(platform)
    text = " ".join(str(raw or "").split())
    low = text.lower()
    # A rate limit or cap first, before anything else can claim it. It is NOT a broken connection:
    # reconnect stays False, and "rate_limit" / "daily_cap" are neither "known" (which flags the
    # connection) nor "unknown" (which counts toward the repeated-failure threshold that eventually
    # tells the owner to reconnect) — so no number of limits can ever send someone to Reconnect.
    # `limit` is the typed rate limit the platform call attached, when it did; otherwise read the text.
    limit = limit or _rate_limit.classify_text(platform, text)
    if limit:
        return {"reconnect": False, "category": "rate_limit" if limit.retryable else "daily_cap",
                "message": f"{limit.user_message} ({name} said: {text[:220]})"}
    for marker, build_message, reconnect in _SPECIFIC_CAUSES:
        if marker in low:
            # These two come from the old publishing provider, not from the network
            # itself — show only the friendly sentence, never the raw provider text.
            hide_raw = marker in _HIDE_RAW_MARKERS
            return {"reconnect": reconnect, "category": "known" if reconnect else "content",
                     "message": build_message(name) if hide_raw
                     else f"{build_message(name)} ({name} said: {text[:220]})"}
    if any(m in low for m in _RECONNECT_MARKERS):
        return {"reconnect": True, "category": "known", "message": (
            f"Your {name} connection has expired — {name} ended URI's access (this happens after a password "
            f"change or a security check). Reconnect {name} in Connected Accounts, then publish this post again."
        )}
    if any(m in low for m in ("image", "photo", "media", "picture")) and "error publishing" in low:
        return {"reconnect": False, "category": "content", "message": (
            f"{name} couldn't use the image for this post. Try another image, "
            f"then publish again. ({name} said: {text[:220]})"
        )}
    return {"reconnect": False, "category": "unknown", "message": (
        f"This post couldn't be published to {name}: {text[:260]}"
        if text else f"This post couldn't be published to {name}. Try publishing it again."
    )}


def _reconnect_match(draft: Dict[str, Any]) -> Dict[str, Any]:
    # The brand's own connection for that network (the card the owner sees).
    query: Dict[str, Any] = {"platform": draft.get("platform"), "user_id": draft.get("user_id")}
    if draft.get("brand_id"):
        query["brand_id"] = draft["brand_id"]
    return query


async def _flag_reconnect(db, draft: Dict[str, Any], message: str) -> None:
    # Reconnecting replaces these docs, which clears it.
    await db["social_connections"].update_many(_reconnect_match(draft), {"$set": {
        "needs_reconnect": True, "reconnect_reason": message, "needs_reconnect_since": datetime.utcnow(),
    }})


# How many consecutive publish failures on one connection, none of them a
# signature we recognize, before treating it as needing the owner's
# attention anyway. A catalogued signature (explain()'s category "known")
# escalates on the very first occurrence — this is the backstop for
# everything else: without it, a network error we've never
# specifically catalogued gets no fallback action at all, no matter how many
# times in a row it keeps failing, an indefinite game of catching up to
# error text one string at a time instead of a system that degrades
# gracefully to something reasonable for the errors nobody's seen yet.
UNKNOWN_FAILURE_ESCALATION_THRESHOLD = 3


async def _should_flag_connection(db, draft: Dict[str, Any], category: str, message: str) -> bool:
    """Whether an "unknown"-category failure means this connection needs the
    owner's attention right now. A single one-off failure (a bad image, a
    transient network hiccup) must not trigger a "reconnect your account"
    alert on its own — only a real, repeated pattern on the same connection
    does, the same bar a human reading the logs would use before concluding
    "this connection is actually broken". Known and content-issue categories
    are decided entirely by explain() and never reach this function with
    anything to do."""
    # `is None`, not `not db`: a Motor database refuses to be used as a boolean.
    if category != "unknown" or db is None:
        return False
    try:
        updated = await db["social_connections"].find_one_and_update(
            _reconnect_match(draft),
            {"$inc": {"consecutive_publish_failures": 1},
             "$set": {"last_publish_error": message, "updated_at": datetime.utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
    except Exception as e:
        print(f"⚠️ _should_flag_connection: counter update failed: {e}")
        return False
    count = (updated or {}).get("consecutive_publish_failures", 0)
    return count >= UNKNOWN_FAILURE_ESCALATION_THRESHOLD


async def _notify(db, draft: Dict[str, Any], published: bool, message: str = "") -> None:
    try:
        if published:
            from app.services.NotificationService import notification_service
            await notification_service.notify_content_posted(
                user_id=draft.get("user_id"), platform=draft.get("platform", ""),
                content_preview=(draft.get("content") or "")[:120],
                campaign_id=draft.get("campaign_id", ""), brand_id=draft.get("brand_id"),
            )
        else:
            from app.services import brand_notifier
            from app.core.config import settings
            app_url = (settings.WEB_APP_URL or "https://www.urisocial.com").strip("'\"")
            await brand_notifier.notify(
                db, draft.get("user_id"), draft.get("brand_id"), "post_failed",
                title=f"Your {network_name(draft.get('platform', ''))} post didn't publish",
                message=message, link=f"{app_url}/workspace",
                metadata={"draft_id": draft.get("id"), "platform": draft.get("platform"),
                          "reconnect": "Reconnect" in (message or "")},
                dedupe_key=draft.get("id"), dedupe_hours=24,
            )
    except Exception as e:  # a notification must never block the status update
        print(f"⚠️ publish confirmation notify failed for {draft.get('id')}: {e}")


async def failed_on_submit(db, draft: Dict[str, Any], connection: Dict[str, Any], raw_error: str,
                           limit: Optional["_rate_limit.RateLimitInfo"] = None) -> str:
    """A post the network refused outright when it was sent. Returns the message
    to store; flags the connection when its login has lapsed and tells the owner."""
    info = explain(draft.get("platform", ""), raw_error or "", limit)
    try:
        _draft = dict(draft)
        reconnect = info["reconnect"]
        if not reconnect and info.get("category") == "unknown":
            reconnect = await _should_flag_connection(db, _draft, info["category"], info["message"])
        if reconnect:
            await _flag_reconnect(db, _draft, info["message"])
        await _notify(db, draft, published=False, message=info["message"])
    except Exception as e:
        print(f"⚠️ failed_on_submit bookkeeping failed for {draft.get('id')}: {e}")
    return info["message"]


