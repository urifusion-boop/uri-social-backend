"""
Brand Playbook notification channels — connecting them and checking them.

- GET  /notification-channels/status          what's connected for the active brand
- POST /notification-channels/test            send a test alert through the brand's choices
- GET  /notification-channels/push/public-key Web Push VAPID public key
- POST /notification-channels/push/subscribe  save this browser's push subscription
- POST /notification-channels/push/unsubscribe
- POST /notification-channels/telegram/link   one-time "Connect Telegram" link
- POST /notification-channels/telegram/disconnect
- POST /notification-channels/telegram/webhook  called by Telegram (secret-checked)
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.config import settings
from app.dependencies import get_active_brand_context, get_db_dependency
from app.services import brand_notifier
from app.services import notification_channels as channels

router = APIRouter(prefix="/notification-channels", tags=["Notification channels"])

TELEGRAM_LINK_TTL = timedelta(minutes=30)


def _profile_filter(profile: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """The exact brand_profiles document a profile came from (older personal
    profiles have no brand_id at all)."""
    if profile.get("brand_id"):
        return {"brand_id": profile["brand_id"]}
    return {"user_id": user_id, "brand_id": {"$exists": False}}


async def _profile(db, ctx: dict) -> Dict[str, Any]:
    return await brand_notifier._profile(db, ctx["user_id"], ctx["brand_id"])


@router.get("/status")
async def channel_status(
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    profile = await _profile(db, ctx)
    user = await db["users"].find_one({"userId": ctx["user_id"]}) or {}
    push_count = await db["push_subscriptions"].count_documents({"user_id": ctx["user_id"]})
    has_twilio = bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN)
    status = {
        # available = the server can send on this channel; connected = this brand/user can receive.
        "email": {"available": True, "connected": bool(user.get("email")), "detail": user.get("email", "")},
        "dashboard": {"available": True, "connected": True, "detail": ""},
        "whatsapp": {"available": has_twilio and bool(settings.TWILIO_WHATSAPP_FROM),
                     "connected": bool(user.get("whatsapp_phone")), "detail": user.get("whatsapp_phone") or ""},
        "sms": {"available": has_twilio and bool(settings.TWILIO_SMS_FROM),
                "connected": bool(user.get("phone") or user.get("whatsapp_phone")),
                "detail": user.get("phone") or user.get("whatsapp_phone") or ""},
        "slack": {"available": True, "connected": channels.is_slack_webhook(profile.get("slack_webhook_url") or ""),
                  "detail": ""},
        "telegram": {"available": bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_BOT_USERNAME),
                     "connected": bool(profile.get("telegram_chat_id")), "detail": profile.get("telegram_username") or ""},
        "push": {"available": bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY),
                 "connected": push_count > 0, "detail": f"{push_count} browser(s)" if push_count else ""},
    }
    return {"status": True, "responseCode": 200, "responseMessage": "Channel status", "responseData": status}


class TestRequest(BaseModel):
    event: Optional[str] = "post_published"


@router.post("/test")
async def send_test(
    body: TestRequest,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Send a test alert exactly as a real one would go (the brand's own
    channel choices), so the user can check their setup."""
    profile = await _profile(db, ctx)
    prefs = brand_notifier.preferences(profile)
    event = body.event if body.event in brand_notifier.EVENT_LABELS else "post_published"
    # A test should reach the channels even if this alert type is switched off.
    test_profile = {**profile, "notification_events": list(profile.get("notification_events") or []) + [brand_notifier.EVENT_LABELS[event]]}
    if not prefs["channel"] and not prefs["approval_channels"]:
        raise HTTPException(status_code=400, detail="Choose a notification channel in Brand Playbook → Notifications first.")
    results = await brand_notifier.notify(
        db, ctx["user_id"], ctx["brand_id"], event,
        title="Test notification from URI Social",
        message="If you can read this, Jane can reach you here.",
        profile=test_profile,
    )
    return {"status": True, "responseCode": 200, "responseMessage": "Test sent", "responseData": results}


# ── Web Push ─────────────────────────────────────────────────────────────────

@router.get("/push/public-key")
async def push_public_key():
    if not settings.VAPID_PUBLIC_KEY:
        raise HTTPException(status_code=404, detail="Push notifications aren't configured")
    return {"status": True, "responseCode": 200, "responseMessage": "ok",
            "responseData": {"public_key": settings.VAPID_PUBLIC_KEY}}


class PushSubscription(BaseModel):
    endpoint: str
    keys: Dict[str, str]


@router.post("/push/subscribe")
async def push_subscribe(
    sub: PushSubscription,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    if not sub.endpoint.startswith("https://") or not sub.keys.get("p256dh") or not sub.keys.get("auth"):
        raise HTTPException(status_code=400, detail="Invalid push subscription")
    await db["push_subscriptions"].update_one(
        {"endpoint": sub.endpoint},
        {"$set": {"user_id": ctx["user_id"], "keys": sub.keys, "updated_at": datetime.utcnow()},
         "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
    )
    return {"status": True, "responseCode": 200, "responseMessage": "Push enabled", "responseData": {}}


class PushUnsubscribe(BaseModel):
    endpoint: str


@router.post("/push/unsubscribe")
async def push_unsubscribe(
    body: PushUnsubscribe,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    await db["push_subscriptions"].delete_one({"endpoint": body.endpoint, "user_id": ctx["user_id"]})
    return {"status": True, "responseCode": 200, "responseMessage": "Push disabled", "responseData": {}}


# ── Telegram ─────────────────────────────────────────────────────────────────

@router.post("/telegram/link")
async def telegram_link(
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """A one-time link: opening it in Telegram and pressing Start connects
    this brand's alerts to that chat."""
    if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_BOT_USERNAME):
        raise HTTPException(status_code=404, detail="Telegram isn't configured")
    code = secrets.token_urlsafe(16)
    await db["telegram_link_codes"].insert_one({
        "code": code, "user_id": ctx["user_id"], "brand_id": ctx["brand_id"], "created_at": datetime.utcnow(),
    })
    url = f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={code}"
    return {"status": True, "responseCode": 200, "responseMessage": "ok", "responseData": {"url": url}}


@router.post("/telegram/disconnect")
async def telegram_disconnect(
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    profile = await _profile(db, ctx)
    if profile:
        await db["brand_profiles"].update_one(
            _profile_filter(profile, ctx["user_id"]),
            {"$unset": {"telegram_chat_id": "", "telegram_username": ""}},
        )
    return {"status": True, "responseCode": 200, "responseMessage": "Telegram disconnected", "responseData": {}}


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    """Telegram calls this for every message to the bot. Only "/start <code>"
    matters: it links the chat to the brand that created the code."""
    if not settings.TELEGRAM_WEBHOOK_SECRET or not secrets.compare_digest(
        x_telegram_bot_api_secret_token or "", settings.TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    update = await request.json()
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    if not text.startswith("/start ") or not chat.get("id"):
        return {"ok": True}
    code = text.split(" ", 1)[1].strip()
    link = await db["telegram_link_codes"].find_one_and_delete({"code": code})
    if not link or datetime.utcnow() - link["created_at"] > TELEGRAM_LINK_TTL:
        await channels.send_telegram(str(chat["id"]), "Link expired",
                                     "Open Brand Playbook → Notifications and press Connect Telegram again.", None)
        return {"ok": True}
    profile = await brand_notifier._profile(db, link["user_id"], link.get("brand_id"))
    if profile:
        await db["brand_profiles"].update_one(
            _profile_filter(profile, link["user_id"]),
            {"$set": {"telegram_chat_id": str(chat["id"]), "telegram_username": chat.get("username") or chat.get("title") or ""}},
        )
        await channels.send_telegram(str(chat["id"]), "Connected to URI Social",
                                     f"You'll get {profile.get('brand_name') or 'your brand'}'s alerts here.", None)
    return {"ok": True}
