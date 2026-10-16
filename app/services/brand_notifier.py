"""
Brand Playbook notifications: who gets told what, where.

The Playbook (and onboarding) save three things:
- notification_events   which alerts the brand wants ("Post published", ...)
- notification_channel  the one channel alerts go to ("Email", "Slack", ...)
- approval_channels     where "post ready for review" goes ("Dashboard", ...)

notify() reads those and sends through notification_channels. Every alert
also lands in the in-app notifications panel. A brand that never set any of
these keeps the old behaviour: email for "post ready" and "post published",
nothing else.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services import notification_channels as channels

# Playbook label -> event key (also the in-app notification type).
EVENTS = {
    "Post ready for review": "post_ready",
    "Post published": "post_published",
    "Weekly performance report": "weekly_report",
    "Trending topic in your niche": "trending_topic",
    "Content calendar needs refill": "calendar_refill",
    "Engagement spike detected": "engagement_spike",
}
EVENT_LABELS = {v: k for k, v in EVENTS.items()}

# Playbook label -> channel key.
CHANNELS = {
    "Email": "email",
    "WhatsApp": "whatsapp",
    "SMS": "sms",
    "Push Notification": "push",
    "Slack": "slack",
    "Telegram": "telegram",
    "Dashboard": "dashboard",
}

# In-app panel types the frontend already knows how to show.
_PANEL_TYPE = {"post_ready": "content_created", "post_published": "content_posted", "post_failed": "post_failed"}

# The alerts every brand has always received. They stay on until the brand
# picks its own list of alerts — choosing only a channel (the normal state
# after onboarding) must not silence them.
_DEFAULT_EVENTS = {"post_ready", "post_published"}

# Never optional: an owner must hear that a post didn't go out, whatever
# alerts they picked.
_ALWAYS_EVENTS = {"post_failed"}


def preferences(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The brand's notification choices, as keys."""
    profile = profile or {}
    events = {EVENTS[e] for e in (profile.get("notification_events") or []) if e in EVENTS}
    channel = CHANNELS.get(profile.get("notification_channel") or "")
    approval = [CHANNELS[c] for c in (profile.get("approval_channels") or []) if c in CHANNELS]
    configured = bool(events or channel or approval)
    return {
        "configured": configured,
        "events": events,
        # What's actually switched on: the brand's own list, or the defaults if it never made one.
        "enabled": events or set(_DEFAULT_EVENTS),
        "channel": channel,
        "approval_channels": approval,
    }


def is_customised(prefs: Dict[str, Any]) -> bool:
    """True when the brand's choices differ from what every brand has always
    had (the two post alerts, by email, shown in the app). Only then do post
    alerts leave the original email path — a brand that merely has "Email"
    saved keeps its branded email and the original limits unchanged."""
    if prefs["events"]:
        return True
    if prefs["channel"] not in (None, "email"):
        return True
    return any(c not in ("dashboard", "email") for c in prefs["approval_channels"])


def channels_for(event: str, prefs: Dict[str, Any]) -> List[str]:
    """Which external channels an event goes to (in-app is always added)."""
    if event not in prefs["enabled"] and event not in _ALWAYS_EVENTS:
        return []
    out: List[str] = []
    if prefs["channel"]:
        out.append(prefs["channel"])
    elif event in _DEFAULT_EVENTS or event in _ALWAYS_EVENTS:
        out.append("email")  # no channel chosen: these two have always gone by email
    if event == "post_ready":
        out += prefs["approval_channels"]
    seen: List[str] = []
    for c in out:
        if c not in seen and c != "dashboard":
            seen.append(c)
    return seen


async def _profile(db: AsyncIOMotorDatabase, user_id: str, brand_id: Optional[str]) -> Dict[str, Any]:
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService

    res = await BrandProfileService.get(user_id, db, brand_id=brand_id)
    return (res.get("responseData") or {}) if res.get("status") else {}


async def _recently_sent(db, user_id: str, event: str, dedupe_key: str, hours: float) -> bool:
    since = datetime.utcnow() - timedelta(hours=hours)
    return bool(await db["notifications"].find_one({
        "user_id": user_id, "metadata.event": event, "metadata.dedupe_key": dedupe_key,
        "created_at": {"$gte": since},
    }))


async def _log(db, user_id: str, event: str, title: str, message: str,
               metadata: Dict[str, Any], deliveries: Dict[str, Any]) -> None:
    """One record per alert — it's what the in-app panel shows, so one alert
    must not appear once per channel. Per-channel results live inside it."""
    now = datetime.utcnow()
    external = [c for c in deliveries if c != "dashboard"]
    failed = [c for c in external if not deliveries[c]["sent"]]
    await db["notifications"].insert_one({
        "notification_id": str(uuid.uuid4()),
        "user_id": user_id,
        "type": _PANEL_TYPE.get(event, event),
        "channel": external[0] if external else "dashboard",
        "status": "failed" if external and len(failed) == len(external) else "sent",
        "subject": title,
        "metadata": {**metadata, "event": event, "message": message, "deliveries": deliveries},
        "retry_count": 0,
        "created_at": now,
        "sent_at": now,
        "error": "; ".join(f"{c}: {deliveries[c]['error']}" for c in failed) or None,
        "read": False,
    })


async def _send(db, channel: str, user: Dict[str, Any], profile: Dict[str, Any], user_id: str,
                title: str, message: str, link: Optional[str]):
    if channel == "email":
        return await channels.send_email(user.get("email", ""), title, message, link)
    if channel == "whatsapp":
        return await channels.send_whatsapp(user.get("whatsapp_phone") or "", title, message, link)
    if channel == "sms":
        return await channels.send_sms(user.get("phone") or user.get("whatsapp_phone") or "", title, message, link)
    if channel == "slack":
        return await channels.send_slack(profile.get("slack_webhook_url") or "", title, message, link)
    if channel == "telegram":
        return await channels.send_telegram(str(profile.get("telegram_chat_id") or ""), title, message, link)
    if channel == "push":
        subs = await db["push_subscriptions"].find({"user_id": user_id}).to_list(length=20)
        result, gone = await channels.send_push(subs, title, message, link)
        if gone:
            await db["push_subscriptions"].delete_many({"endpoint": {"$in": gone}})
        return result
    return False, f"Unknown channel {channel}"


async def notify(
    db: AsyncIOMotorDatabase,
    user_id: str,
    brand_id: Optional[str],
    event: str,
    title: str,
    message: str,
    link: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    dedupe_key: Optional[str] = None,
    dedupe_hours: float = 0,
    profile: Optional[Dict[str, Any]] = None,
    only_if_configured: bool = False,
) -> Dict[str, Any]:
    """Send `event` to the brand's chosen channels and the in-app panel.

    only_if_configured: skip brands that haven't set notification preferences
    (used by the new alerts, which were never on by default)."""
    profile = profile if profile is not None else await _profile(db, user_id, brand_id)
    prefs = preferences(profile)
    if only_if_configured and event not in prefs["events"]:
        return {"skipped": "not enabled"}   # the newer alerts are opt-in: only if explicitly ticked
    if event not in prefs["enabled"] and event not in _ALWAYS_EVENTS:
        return {"skipped": "not enabled"}
    if dedupe_key and dedupe_hours and await _recently_sent(db, user_id, event, dedupe_key, dedupe_hours):
        return {"skipped": "already sent"}

    user = await db["users"].find_one({"userId": user_id}) or {}
    if user.get("notification_opt_out"):
        return {"skipped": "user opted out"}

    meta = {**(metadata or {}), "brand_id": brand_id, "dedupe_key": dedupe_key, "link": link}
    results: Dict[str, Any] = {}
    for channel in channels_for(event, prefs):
        try:
            ok, error = await _send(db, channel, user, profile, user_id, title, message, link)
        except Exception as exc:  # a channel must never break the caller
            ok, error = False, str(exc)
        results[channel] = {"sent": ok, "error": error}
        if not ok:
            print(f"[Notify] {event} via {channel} for user={user_id}: {error}")
    # Always visible in the app's notifications panel.
    results["dashboard"] = {"sent": True, "error": None}
    await _log(db, user_id, event, title, message, meta, results)
    return results
