"""
WhatsApp router
---------------
POST /whatsapp/webhook       — Twilio webhook (no auth, validated by Twilio sig)
POST /whatsapp/connect       — Link a phone number to the authed user (JWT required)
DELETE /whatsapp/connect     — Unlink WhatsApp from the authed user (JWT required)
GET  /whatsapp/status        — Check if the authed user has WhatsApp linked (JWT required)
POST /whatsapp/daily-push    — Trigger daily content push to all linked users (internal)
"""

import pymongo.errors
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.auth_bearer import JWTBearer
from app.core.config import settings
from app.dependencies import get_db_dependency
from app.agents.social_media_manager.services.whatsapp_session_service import (
    WhatsAppSessionService,
)
from app.agents.social_media_manager.services.whatsapp_flow_service import (
    WhatsAppFlowService,
    _send,
)

router = APIRouter(prefix="/whatsapp", tags=["WhatsApp"])


def _extract_user_id(token: dict) -> str:
    claims = token.get("claims", {}) if isinstance(token, dict) else {}
    uid = (
        token.get("userId")
        or token.get("user_id")
        or claims.get("userId")
        or claims.get("user_id")
    )
    if not uid:
        raise HTTPException(status_code=401, detail="Could not resolve user ID from token.")
    return str(uid)


# ── Twilio webhook ─────────────────────────────────────────────────────────


@router.post("/webhook")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Receives incoming WhatsApp messages from Twilio (form-encoded body).
    Validates the Twilio request signature before processing.
    Responds with 200 immediately; processing happens in a background task
    so Twilio doesn't time out.
    """
    # ── Parse request body ─────────────────────────────────────────────────
    import urllib.parse
    body_bytes = await request.body()
    params = dict(urllib.parse.parse_qsl(body_bytes.decode("utf-8")))

    # ── Twilio signature validation ────────────────────────────────────────
    if settings.TWILIO_AUTH_TOKEN:
        from twilio.request_validator import RequestValidator  # type: ignore

        validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)
        twilio_sig = request.headers.get("X-Twilio-Signature", "")
        _base = str(settings.PUBLIC_API_URL).rstrip("/")
        url = f"{_base}/whatsapp/webhook"

        valid = validator.validate(url, params, twilio_sig)
        print(f"[WhatsApp] webhook url={url!r} from={params.get('From')!r} body={params.get('Body')!r} sig_valid={valid}")

    raw_from: str = params.get("From", "")
    # Button quick-reply taps may arrive with empty Body — fall back to ButtonText
    body: str = params.get("Body", "") or params.get("ButtonText", "")

    if not raw_from:
        return {"status": "ignored"}

    background_tasks.add_task(WhatsAppFlowService.handle, raw_from, body, db)
    return {"status": "queued"}


# ── Connect / disconnect ───────────────────────────────────────────────────


class ConnectRequest(BaseModel):
    phone: str  # E.164 format e.g. +2348012345678


@router.post("/connect")
async def connect_whatsapp(
    body: ConnectRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Links a phone number to the authenticated user's account.
    Called from the dashboard after the user enters their WhatsApp number.
    """
    user_id = _extract_user_id(token)

    # Ensure the phone isn't already linked to another account
    try:
        existing = await WhatsAppSessionService.get_user_by_phone(body.phone, db)
        if existing and existing.get("userId") != user_id:
            raise HTTPException(
                status_code=409,
                detail="This phone number is already linked to a different account.",
            )
    except pymongo.errors.NetworkTimeout:
        pass  # Can't check duplicate — proceed optimistically

    try:
        await WhatsAppSessionService.link_phone_to_user(user_id, body.phone, db)
    except pymongo.errors.NetworkTimeout:
        # Verify whether the write actually landed.
        # Compare against the normalized phone (same transformation link_phone_to_user applies).
        from app.agents.social_media_manager.services.whatsapp_session_service import (
            WhatsAppSessionService as _WSS,
        )
        normalized = _WSS._normalize_phone(body.phone)
        try:
            check = await db["users"].find_one(
                {"userId": user_id}, {"whatsapp_phone": 1}
            )
            if not check or check.get("whatsapp_phone") != normalized:
                raise HTTPException(
                    status_code=503,
                    detail="Database is slow — your number may not have been saved. Please try again.",
                )
        except pymongo.errors.NetworkTimeout:
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. Please try again in a moment.",
            )

    try:
        await _send(
            body.phone,
            "",
            content_sid="HXccf1a2bb34e7ed257c136c842982f5b3",
        )
    except Exception as e:
        print(f"[WhatsApp] connect greeting failed: {e}")

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "WhatsApp linked successfully.",
        "responseData": {"phone": body.phone},
    }


@router.delete("/connect")
async def disconnect_whatsapp(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Removes the WhatsApp link for the authenticated user."""
    user_id = _extract_user_id(token)

    try:
        result = await db["users"].update_one(
            {"userId": user_id},
            {"$unset": {"whatsapp_phone": "", "whatsapp_linked_at": ""}},
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="User not found.")
    except pymongo.errors.NetworkTimeout:
        # Verify whether the unset actually landed
        try:
            check = await db["users"].find_one({"userId": user_id}, {"whatsapp_phone": 1})
            if check and check.get("whatsapp_phone"):
                raise HTTPException(
                    status_code=503,
                    detail="Database is slow — your number may still be linked. Please try again.",
                )
        except pymongo.errors.NetworkTimeout:
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. Please try again in a moment.",
            )

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "WhatsApp disconnected.",
    }


# ── Status check ───────────────────────────────────────────────────────────


@router.get("/status")
async def whatsapp_status(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Returns whether the authenticated user has WhatsApp linked."""
    user_id = _extract_user_id(token)
    try:
        user = await db["users"].find_one(
            {"userId": user_id}, {"whatsapp_phone": 1, "whatsapp_linked_at": 1}
        )
    except pymongo.errors.NetworkTimeout:
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable. Please try again in a moment.",
        )

    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")

    linked = bool(user.get("whatsapp_phone"))
    return {
        "status": True,
        "responseCode": 200,
        "responseData": {
            "linked": linked,
            "phone": user.get("whatsapp_phone") if linked else None,
            "linked_at": user.get("whatsapp_linked_at"),
        },
    }


# ── Daily push (internal / cron) ──────────────────────────────────────────


@router.post("/daily-push")
async def daily_push(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Trigger daily content push to all WhatsApp-linked users.
    Protect this endpoint with a shared secret header (X-Cron-Secret)
    in production.
    """
    from app.core.config import settings

    cron_secret = request.headers.get("X-Cron-Secret", "")
    expected = getattr(settings, "CRON_SECRET", "")
    if expected and cron_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden.")

    result = await WhatsAppFlowService.send_daily_push(db)
    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Daily push complete.",
        "responseData": result,
    }
