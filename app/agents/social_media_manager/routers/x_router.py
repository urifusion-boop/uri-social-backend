"""
X (Twitter) router  —  via Outstand
-------------------------------------
POST   /x/connect          Start X OAuth flow, returns auth_url  (JWT required)
DELETE /x/connect          Disconnect X account                   (JWT required)
GET    /x/status           Check if user has X connected          (JWT required)
POST   /x/publish          Publish (or schedule) a post to X      (JWT required)
POST   /x/daily-push       Daily content push to all X users      (cron secret)
POST   /x/webhook          Outstand post-event webhook            (Outstand sig)

Connection flow (3 steps, handled by the existing social-media router):
  1. POST /x/connect                → returns auth_url
  2. User authorises → Outstand redirects to
     GET /social-media/connect/callback/outstand?sessionToken=...
     Frontend then calls:
  3. GET  /social-media/connect/pending/{sessionToken}
  4. POST /social-media/connect/finalize
"""

import hashlib
import hmac
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.agents.social_media_manager.services.outstand_service import OutstandService
from app.agents.social_media_manager.services.social_account_service import SocialAccountService
from app.core.auth_bearer import JWTBearer
from app.core.config import settings
from app.dependencies import get_db_dependency

router = APIRouter(prefix="/x", tags=["X (Twitter)"])


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


async def _get_x_connection(user_id: str, db: AsyncIOMotorDatabase) -> Optional[dict]:
    """Return the user's active X connection doc from social_connections, or None."""
    return await db["social_connections"].find_one(
        {"user_id": user_id, "platform": "x", "connection_status": "active"}
    )


# ── Connect / OAuth ────────────────────────────────────────────────────────


@router.post("/connect")
async def connect_x(
    token: dict = Depends(JWTBearer()),
):
    """
    Step 1: start the X OAuth flow via Outstand.
    Returns an auth_url — redirect the user's browser there to authorise X.
    After authorisation the user lands on /social-media/connect/callback/outstand;
    complete the flow with GET /social-media/connect/pending/{sessionToken} then
    POST /social-media/connect/finalize.
    """
    if not settings.OUTSTAND_API_KEY:
        raise HTTPException(status_code=503, detail="Outstand integration not configured.")

    user_id = _extract_user_id(token)
    result = await SocialAccountService.initiate_connection_flow(
        user_id=user_id,
        platforms=["x"],
    )
    return result


# ── Disconnect ──────────────────────────────────────────────────────────────


@router.delete("/connect")
async def disconnect_x(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Disconnect the authenticated user's X account."""
    user_id = _extract_user_id(token)

    conn = await _get_x_connection(user_id, db)
    if not conn:
        raise HTTPException(status_code=400, detail="No X account connected.")

    outstand_account_id = conn["outstand_account_id"]

    outstand = OutstandService()
    try:
        await outstand.delete_account(outstand_account_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to disconnect from Outstand: {e}")

    await db["social_connections"].delete_one({"_id": conn["_id"]})

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "X account disconnected.",
    }


# ── Status ─────────────────────────────────────────────────────────────────


@router.get("/status")
async def x_status(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Returns whether the authenticated user has an active X account connected."""
    user_id = _extract_user_id(token)
    conn = await _get_x_connection(user_id, db)

    linked = conn is not None
    return {
        "status": True,
        "responseCode": 200,
        "responseData": {
            "linked": linked,
            "username": conn.get("username") if linked else None,
            "account_name": conn.get("account_name") if linked else None,
            "connected_at": conn.get("connected_at") if linked else None,
        },
    }


# ── Publish / Schedule ─────────────────────────────────────────────────────


class PublishRequest(BaseModel):
    content: str
    scheduled_at: Optional[str] = None   # ISO 8601 e.g. "2025-09-20T14:00:00Z"
    tweets: Optional[list] = None         # For threads: list of tweet strings


@router.post("/publish")
async def publish_to_x(
    body: PublishRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Publish (or schedule) a post to the authenticated user's X account.
    Pass scheduled_at to schedule up to 30 days ahead (ISO 8601).
    Pass tweets as a list to publish a thread.
    """
    user_id = _extract_user_id(token)

    conn = await _get_x_connection(user_id, db)
    if not conn:
        raise HTTPException(
            status_code=400, detail="No X account connected. Call POST /x/connect first."
        )

    outstand = OutstandService()
    try:
        result = await outstand.publish_post(
            outstand_account_ids=[conn["outstand_account_id"]],
            content=body.content,
            scheduled_at=body.scheduled_at,
            tweets=body.tweets,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to publish to X: {e}")

    post = result.get("post", {})
    action = "scheduled" if body.scheduled_at else "published"

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": f"Post {action} to X successfully.",
        "responseData": {
            "post_id": post.get("id"),
            "scheduled_at": post.get("scheduledAt"),
            "published_at": post.get("publishedAt"),
            "content": body.content,
        },
    }


# ── Daily push (cron) ──────────────────────────────────────────────────────


async def _send_x_daily_push(db: AsyncIOMotorDatabase) -> dict:
    """
    Background task: for every user with an active X connection, generate one
    tweet and publish it via Outstand.
    """
    from app.agents.social_media_manager.services.whatsapp_session_service import (
        WhatsAppSessionService,
    )
    from app.services.AIService import AIService
    from app.domain.models.chat_model import ChatModel, ChatMessage

    cursor = db["social_connections"].find(
        {"platform": "x", "connection_status": "active"},
        {"user_id": 1, "outstand_account_id": 1, "username": 1},
    )
    connections = await cursor.to_list(length=None)

    sent = 0
    failed = 0
    outstand = OutstandService()

    for conn in connections:
        user_id = conn.get("user_id")
        outstand_account_id = conn.get("outstand_account_id")
        try:
            brand = await WhatsAppSessionService.get_brand_profile(user_id, db)
            brand_name = (brand or {}).get("brand_name", "your brand")
            industry = (brand or {}).get("industry", "your industry")

            messages = [
                ChatMessage(
                    role="user",
                    content=(
                        f"Write 1 engaging tweet (under 280 characters) for {brand_name} "
                        f"in the {industry} space. Be punchy, no hashtags."
                    ),
                )
            ]
            request = ChatModel(model="gpt-5.4-mini", messages=messages, temperature=0.9)
            result = await AIService.chat_completion(request)

            if isinstance(result, dict) and result.get("error"):
                raise Exception(result["error"])

            tweet_text = result.choices[0].message.content.strip()

            await outstand.publish_post(
                outstand_account_ids=[outstand_account_id],
                content=tweet_text,
            )
            sent += 1
        except Exception as e:
            print(f"X daily push failed for user {user_id}: {e}")
            failed += 1

    return {"sent": sent, "failed": failed, "total": len(connections)}


@router.post("/daily-push")
async def x_daily_push(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Trigger daily X content push for all connected users.
    Protected by X-Cron-Secret header.
    """
    cron_secret = request.headers.get("X-Cron-Secret", "")
    expected = getattr(settings, "CRON_SECRET", "")
    if expected and cron_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden.")

    background_tasks.add_task(_send_x_daily_push, db)
    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "X daily push queued.",
    }


# ── Outstand webhook ────────────────────────────────────────────────────────


@router.post("/webhook")
async def outstand_webhook(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Receives post-event webhooks from Outstand (post.published / post.error).
    Configure this URL in the Outstand dashboard under Settings → Webhooks.
    Validates X-Outstand-Signature if OUTSTAND_WEBHOOK_SECRET is set.
    """
    body_bytes = await request.body()

    if settings.OUTSTAND_WEBHOOK_SECRET:
        sig_header = request.headers.get("X-Outstand-Signature", "")
        expected_sig = "sha256=" + hmac.new(
            settings.OUTSTAND_WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected_sig):
            raise HTTPException(status_code=403, detail="Invalid webhook signature.")

    try:
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    event = payload.get("event")
    data = payload.get("data", {})
    post_id = data.get("postId")
    org_id = data.get("orgId")

    if event == "post.published":
        print(f"✅ Outstand: post {post_id} published (org={org_id})")
        # Extend here: update post status in DB, send notification, etc.

    elif event == "post.error":
        print(f"❌ Outstand: post {post_id} failed (org={org_id})")
        # Extend here: alert user, retry logic, etc.

    return {"status": "ok"}
