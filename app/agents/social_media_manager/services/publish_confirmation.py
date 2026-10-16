"""
Confirming that a post published through Outstand actually reached the network.

Outstand answers "accepted, queued" straight away and publishes a moment later.
Drafts used to be marked "published" on that first answer, so a post Facebook
rejected (e.g. "Error validating access token: the session has been invalidated
because the user changed their password") still showed as published in URI and
the customer never knew (seen in production, October 2026).

Now an accepted post is "confirming" until Outstand says what happened:
  • live                → published
  • failed, with reason → publish_failed, with the reason in plain words; a
                          lapsed login also flags the connection for reconnecting
  • no answer yet       → stays confirming; after 30 minutes it is shown as
                          taking longer than usual; after 24 hours with still no
                          answer it is marked failed, saying it couldn't be
                          confirmed (Outstand settles posts long before that)
Nothing is ever called failed just because the answer hasn't come yet, and a
late "live" always wins while the post is being confirmed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

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


def network_name(platform: str) -> str:
    return _NETWORK_NAMES.get((platform or "").lower(), (platform or "the network").title())


def explain(platform: str, raw: str) -> Dict[str, Any]:
    """The network's error in words an owner can act on, and whether the
    account needs reconnecting."""
    name = network_name(platform)
    text = " ".join(str(raw or "").split())
    low = text.lower()
    if any(m in low for m in _RECONNECT_MARKERS):
        return {"reconnect": True, "message": (
            f"Your {name} connection has expired — {name} ended URI's access (this happens after a password "
            f"change or a security check). Reconnect {name} in Connected Accounts, then publish this post again."
        )}
    if any(m in low for m in ("image", "photo", "media", "picture")) and "error publishing" in low:
        return {"reconnect": False, "message": f"{name} couldn't use the image for this post. Try another image, "
                                               f"then publish again. ({name} said: {text[:220]})"}
    return {"reconnect": False, "message": f"This post couldn't be published to {name}: {text[:260]}"
            if text else f"This post couldn't be published to {name}. Try publishing it again."}


def read_outcome(payload: Dict[str, Any]) -> Dict[str, Any]:
    """{"state": "published"|"failed"|"pending", "error": str} from Outstand's
    GET /v1/posts/{id}. Per-account results win: that is where a network's
    rejection is reported (socialAccounts[].status/error)."""
    post = (payload or {}).get("post") or (payload or {}).get("data") or {}
    if isinstance(post, list):
        post = post[0] if post else {}
    accounts = [a for a in (post.get("socialAccounts") or []) if isinstance(a, dict)]
    failed = next((a for a in accounts if str(a.get("status", "")).lower() == "failed"), None)
    if failed:
        return {"state": "failed", "error": failed.get("error") or failed.get("errorMessage") or ""}
    if str(post.get("status", "")).lower() == "failed":
        return {"state": "failed", "error": post.get("error") or ""}
    done = {"published", "success", "posted", "completed"}
    if post.get("publishedAt") and not post.get("isDraft"):
        if not accounts or all(str(a.get("status", "published")).lower() in done for a in accounts):
            return {"state": "published", "error": ""}
    if accounts and all(str(a.get("status", "")).lower() in done for a in accounts):
        return {"state": "published", "error": ""}
    return {"state": "pending", "error": ""}


async def _flag_reconnect(db, draft: Dict[str, Any], message: str) -> None:
    # The account that failed, and the brand's own connection for that network
    # (the card the owner sees). Reconnecting replaces these docs, which clears it.
    account_id = draft.get("outstand_account_id")
    brand_id = draft.get("brand_id")
    which = ([{"outstand_account_id": account_id}] if account_id else []) + ([{"brand_id": brand_id}] if brand_id else [])
    query: Dict[str, Any] = {"platform": draft.get("platform"), "user_id": draft.get("user_id")}
    if which:
        query["$or"] = which
    await db["social_connections"].update_many(query, {"$set": {
        "needs_reconnect": True, "reconnect_reason": message, "needs_reconnect_since": datetime.utcnow(),
    }})


async def _clear_reconnect(db, draft: Dict[str, Any]) -> None:
    account_id = draft.get("outstand_account_id")
    if account_id:
        await db["social_connections"].update_many(
            {"outstand_account_id": account_id, "needs_reconnect": True},
            {"$unset": {"needs_reconnect": "", "reconnect_reason": "", "needs_reconnect_since": ""}},
        )


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


async def confirm(db, draft: Dict[str, Any], outstand=None, now: Optional[datetime] = None) -> str:
    """Check one confirming draft with Outstand and apply the answer.
    Returns the draft's state afterwards."""
    now = now or datetime.utcnow()
    post_id = draft.get("platform_post_id")
    if not post_id:
        return "pending"
    if outstand is None:
        from app.agents.social_media_manager.services.outstand_service import OutstandService
        outstand = OutstandService()
    try:
        outcome = read_outcome(await outstand.get_post(post_id))
    except Exception as e:
        if "404" in str(e):
            outcome = {"state": "failed", "error": "", "missing": True}
        else:
            print(f"⚠️ confirm {draft.get('id')}: Outstand unreachable, will retry: {e}")
            outcome = {"state": "pending", "error": ""}

    if outcome["state"] == "published":
        await db["content_drafts"].update_one({"id": draft["id"], "status": CONFIRMING}, {
            "$set": {"status": "published", "published_date": now, "updated_at": now, "error_message": None},
            "$unset": {"publish_slow": ""},
        })
        await _clear_reconnect(db, draft)
        await _notify(db, draft, published=True)
        return "published"

    if outcome["state"] == "failed":
        if outcome.get("missing"):
            message = (f"The publishing service no longer has this post, so it can't be confirmed on "
                       f"{network_name(draft.get('platform', ''))}. Check your page, and publish it again if it isn't there.")
            reconnect = False
        else:
            info = explain(draft.get("platform", ""), outcome["error"])
            message, reconnect = info["message"], info["reconnect"]
        await db["content_drafts"].update_one({"id": draft["id"], "status": CONFIRMING}, {
            "$set": {"status": "publish_failed", "error_message": message, "updated_at": now,
                     "network_error": outcome["error"][:1000] if outcome["error"] else None},
            "$unset": {"publish_slow": ""},
        })
        if reconnect:
            await _flag_reconnect(db, draft, message)
        await _notify(db, draft, published=False, message=message)
        return "failed"

    submitted = draft.get("publish_submitted_at") or draft.get("updated_at") or now
    age = now - submitted
    if age >= GIVE_UP_AFTER:
        message = (f"We couldn't confirm this post reached {network_name(draft.get('platform', ''))} after 24 hours. "
                   f"Check your page — if it isn't there, publish it again.")
        await db["content_drafts"].update_one({"id": draft["id"], "status": CONFIRMING}, {
            "$set": {"status": "publish_failed", "error_message": message, "updated_at": now},
            "$unset": {"publish_slow": ""},
        })
        await _notify(db, draft, published=False, message=message)
        return "failed"
    if age >= SLOW_AFTER and not draft.get("publish_slow"):
        await db["content_drafts"].update_one({"id": draft["id"], "status": CONFIRMING},
                                              {"$set": {"publish_slow": True}})
    return "pending"


async def run(db, limit: int = 200) -> Dict[str, int]:
    """Check every post still being confirmed (the 2-minute background job)."""
    counts = {"published": 0, "failed": 0, "pending": 0}
    from app.agents.social_media_manager.services.outstand_service import OutstandService
    outstand = OutstandService()
    drafts = await db["content_drafts"].find({"status": CONFIRMING}).to_list(length=limit)
    for draft in drafts:
        try:
            counts[await confirm(db, draft, outstand=outstand)] += 1
        except Exception as e:
            print(f"⚠️ confirm {draft.get('id')} failed: {e}")
            counts["pending"] += 1
    return counts


async def failed_on_submit(db, draft: Dict[str, Any], connection: Dict[str, Any], raw_error: str) -> str:
    """A post the network refused outright when it was sent. Returns the message
    to store; flags the connection when its login has lapsed and tells the owner."""
    info = explain(draft.get("platform", ""), raw_error or "")
    try:
        if info["reconnect"]:
            await _flag_reconnect(db, {**draft, "outstand_account_id": (connection or {}).get("outstand_account_id")},
                                  info["message"])
        await _notify(db, draft, published=False, message=info["message"])
    except Exception as e:
        print(f"⚠️ failed_on_submit bookkeeping failed for {draft.get('id')}: {e}")
    return info["message"]


def awaiting_fields(publish_result: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """What a draft gets when Outstand has accepted it but not yet published it."""
    now = now or datetime.utcnow()
    return {
        "status": CONFIRMING,
        "platform_post_id": publish_result.get("post_id"),
        "outstand_post_status": publish_result.get("outstand_status", "queued"),
        "outstand_account_id": publish_result.get("outstand_account_id"),
        "publish_response": publish_result.get("raw_response"),
        "publish_submitted_at": now,
        "published_date": None,
        "updated_at": now,
        "error_message": None,
    }


def needs_confirmation(publish_result: Dict[str, Any]) -> bool:
    """An Outstand post that isn't already confirmed live. TikTok is confirmed
    before it returns (its own poll), so it never needs this."""
    return bool(publish_result.get("via") == "outstand" and not publish_result.get("confirmed"))
