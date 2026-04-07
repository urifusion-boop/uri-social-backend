"""
X (Twitter) router — OAuth 1.0a direct posting (no Outstand required)
----------------------------------------------------------------------
POST   /x/connect          Start X OAuth 1.0a flow, returns auth_url    (JWT required)
GET    /x/callback         OAuth 1.0a callback — stores tokens, redirects frontend
DELETE /x/connect          Disconnect X account                          (JWT required)
GET    /x/status           Check if user has X connected                 (JWT required)
POST   /x/publish          Publish (or thread) to X directly             (JWT required)
POST   /x/daily-push       Daily content push to all X users             (cron secret)

Connection flow:
  1. POST /x/connect                → returns auth_url
  2. Frontend opens auth_url in a popup / redirect
  3. User authorises on X → X redirects to GET /x/callback?oauth_token=...&oauth_verifier=...
  4. Backend stores tokens, redirects browser to frontend with ?connected=true&platform=x
"""

import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.agents.social_media_manager.services.x_direct_service import XDirectService
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
    """Return the user's active X connection doc, or None."""
    return await db["social_connections"].find_one(
        {"user_id": user_id, "platform": "x", "connection_status": "active"}
    )


def _x_service() -> XDirectService:
    if not settings.X_API_KEY or not settings.X_API_SECRET:
        raise HTTPException(status_code=503, detail="X API credentials not configured.")
    return XDirectService()


# ── Connect — Step 1 ────────────────────────────────────────────────────────


@router.post("/connect")
async def connect_x(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Start the X OAuth 1.0a flow.
    Returns auth_url — open this in a browser so the user can authorise.
    After authorisation X will redirect to GET /x/callback.
    """
    if not settings.X_OAUTH_CALLBACK_URL:
        raise HTTPException(status_code=503, detail="X_OAUTH_CALLBACK_URL not configured.")

    user_id = _extract_user_id(token)
    svc = _x_service()

    oauth_token, oauth_token_secret, auth_url = await svc.get_request_token(
        callback_url=settings.X_OAUTH_CALLBACK_URL
    )

    # Store the request token temporarily (10-minute TTL via expires_at)
    await db["x_oauth_pending"].update_one(
        {"oauth_token": oauth_token},
        {
            "$set": {
                "oauth_token": oauth_token,
                "oauth_token_secret": oauth_token_secret,
                "user_id": user_id,
                "expires_at": datetime.utcnow() + timedelta(minutes=10),
            }
        },
        upsert=True,
    )

    return {
        "status": True,
        "responseCode": 200,
        "responseData": {"auth_url": auth_url},
    }


# ── Connect — Step 2 (OAuth callback, no JWT) ───────────────────────────────


@router.get("/callback")
async def x_oauth_callback(
    oauth_token: Optional[str] = Query(None),
    oauth_verifier: Optional[str] = Query(None),
    denied: Optional[str] = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    X redirects the user's browser here after they authorise (or deny) the app.
    No JWT — the user is identified by the oauth_token stored during /connect.
    Stores per-user OAuth 1.0a tokens then redirects to the frontend.
    """
    web_app_url = settings.WEB_APP_URL

    if denied or not oauth_token or not oauth_verifier:
        return RedirectResponse(
            f"{web_app_url}/social-media/brand-setup?connected=false&platform=x&error=access_denied"
        )

    # Look up the pending auth doc
    pending = await db["x_oauth_pending"].find_one({"oauth_token": oauth_token})
    if not pending:
        return RedirectResponse(
            f"{web_app_url}/social-media/brand-setup?connected=false&platform=x&error=session_expired"
        )
    if pending.get("expires_at") and datetime.utcnow() > pending["expires_at"]:
        await db["x_oauth_pending"].delete_one({"oauth_token": oauth_token})
        return RedirectResponse(
            f"{web_app_url}/social-media/brand-setup?connected=false&platform=x&error=session_expired"
        )

    user_id = pending["user_id"]
    oauth_token_secret = pending["oauth_token_secret"]

    try:
        svc = _x_service()
        result = await svc.get_access_token(oauth_token, oauth_token_secret, oauth_verifier)
    except Exception as e:
        return RedirectResponse(
            f"{web_app_url}/social-media/brand-setup"
            f"?connected=false&platform=x&error={urllib.parse.quote(str(e))}"
        )
    finally:
        await db["x_oauth_pending"].delete_one({"oauth_token": oauth_token})

    now = datetime.utcnow()
    screen_name = result.get("screen_name", "")
    x_user_id = result.get("user_id", "")

    await db["social_connections"].update_one(
        {"user_id": user_id, "platform": "x"},
        {
            "$set": {
                "user_id": user_id,
                "platform": "x",
                "username": screen_name,
                "account_name": screen_name,
                "network_unique_id": x_user_id,
                "connection_status": "active",
                "connected_via": "x_direct",
                "x_oauth_token": result["access_token"],
                "x_oauth_token_secret": result["access_token_secret"],
                "connected_at": now,
                "updated_at": now,
            }
        },
        upsert=True,
    )

    return RedirectResponse(
        f"{web_app_url}/social-media/brand-setup"
        f"?connected=true&platform=x&username={urllib.parse.quote(screen_name)}"
    )


# ── Disconnect ───────────────────────────────────────────────────────────────


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

    await db["social_connections"].delete_one({"_id": conn["_id"]})

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "X account disconnected.",
    }


# ── Status ───────────────────────────────────────────────────────────────────


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


# ── Publish / Thread ─────────────────────────────────────────────────────────


class PublishRequest(BaseModel):
    content: str
    tweets: Optional[list] = None  # For threads: list of tweet strings


@router.post("/publish")
async def publish_to_x(
    body: PublishRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Publish a tweet (or thread) directly to the authenticated user's X account
    via OAuth 1.0a — no Outstand, no paid X API tier required.
    Pass tweets as a list to publish a thread.
    """
    user_id = _extract_user_id(token)

    conn = await _get_x_connection(user_id, db)
    if not conn:
        raise HTTPException(
            status_code=400, detail="No X account connected. Call POST /x/connect first."
        )

    access_token = conn.get("x_oauth_token")
    access_token_secret = conn.get("x_oauth_token_secret")
    if not access_token or not access_token_secret:
        raise HTTPException(
            status_code=400,
            detail="X connection is missing OAuth tokens. Please reconnect your account.",
        )

    svc = _x_service()

    try:
        if body.tweets and len(body.tweets) > 1:
            results = await svc.post_thread(access_token, access_token_secret, body.tweets)
            tweet_id = (results[0].get("data") or {}).get("id") if results else None
        else:
            result = await svc.post_tweet(access_token, access_token_secret, body.content)
            tweet_id = (result.get("data") or {}).get("id")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to post to X: {e}")

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Post published to X successfully.",
        "responseData": {
            "tweet_id": tweet_id,
            "content": body.content,
        },
    }


# ── Daily push (cron) ────────────────────────────────────────────────────────


async def _send_x_daily_push(db: AsyncIOMotorDatabase) -> dict:
    """
    Background task: for every user with an active X connection, generate one
    tweet and publish it directly via OAuth 1.0a.
    """
    from app.agents.social_media_manager.services.whatsapp_session_service import (
        WhatsAppSessionService,
    )
    from app.domain.models.chat_model import ChatMessage, ChatModel
    from app.services.AIService import AIService

    cursor = db["social_connections"].find(
        {"platform": "x", "connection_status": "active"},
        {"user_id": 1, "x_oauth_token": 1, "x_oauth_token_secret": 1, "username": 1},
    )
    connections = await cursor.to_list(length=None)

    sent = 0
    failed = 0

    for conn in connections:
        user_id = conn.get("user_id")
        access_token = conn.get("x_oauth_token")
        access_token_secret = conn.get("x_oauth_token_secret")

        if not access_token or not access_token_secret:
            failed += 1
            continue

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

            svc = XDirectService()
            await svc.post_tweet(access_token, access_token_secret, tweet_text)
            sent += 1
        except Exception as e:
            print(f"X daily push failed for user {user_id}: {e}")
            failed += 1

    return {"sent": sent, "failed": failed, "total": len(connections)}


@router.post("/daily-push")
async def x_daily_push(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Trigger daily X content push for all connected users.
    Protected by X-Cron-Secret header.
    """
    import asyncio

    cron_secret = request.headers.get("X-Cron-Secret", "")
    expected = getattr(settings, "CRON_SECRET", "")
    if expected and cron_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden.")

    asyncio.create_task(_send_x_daily_push(db))
    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "X daily push queued.",
    }
