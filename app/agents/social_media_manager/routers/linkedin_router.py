"""
LinkedIn router — OAuth 2.0 direct posting
------------------------------------------
POST   /linkedin/connect      Start LinkedIn OAuth 2.0 flow, returns auth_url    (JWT required)
GET    /linkedin/callback     OAuth 2.0 callback — stores tokens, redirects frontend
DELETE /linkedin/connect      Disconnect LinkedIn account                         (JWT required)
GET    /linkedin/status       Check if user has LinkedIn connected                (JWT required)
POST   /linkedin/publish      Publish a post to LinkedIn                          (JWT required)
POST   /linkedin/daily-push   Daily content push to all LinkedIn users            (cron secret)

Connection flow:
  1. POST /linkedin/connect               → returns auth_url
  2. Frontend opens auth_url in popup / redirect
  3. User authorises → LinkedIn redirects to GET /linkedin/callback?code=...&state=...
  4. Backend stores tokens, redirects browser to frontend with ?connected=true&platform=linkedin
"""

import secrets
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.agents.social_media_manager.services.linkedin_direct_service import LinkedInDirectService
from app.core.config import settings
from app.dependencies import get_db_dependency, get_active_brand_context

router = APIRouter(prefix="/linkedin", tags=["LinkedIn"])


async def _get_linkedin_connection(user_id: str, db: AsyncIOMotorDatabase, brand_id: Optional[str] = None) -> Optional[dict]:
    """Return the active LinkedIn connection doc for the given brand, or None."""
    from app.models.brand_account import BrandAccount
    personal_bid = BrandAccount.personal_brand_id(user_id)
    is_personal = (not brand_id) or brand_id == personal_bid
    if is_personal:
        query = {
            "user_id": user_id,
            "platform": "linkedin",
            "connection_status": "active",
            "$or": [
                {"brand_id": {"$exists": False}},
                {"brand_id": None},
                {"brand_id": personal_bid},
            ],
        }
    else:
        query = {"brand_id": brand_id, "platform": "linkedin", "connection_status": "active"}
    return await db["social_connections"].find_one(query)


def _linkedin_service() -> LinkedInDirectService:
    if not settings.LINKEDIN_CLIENT_ID or not settings.LINKEDIN_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="LinkedIn credentials not configured.")
    return LinkedInDirectService()


# ── Connect — Step 1 ────────────────────────────────────────────────────────


@router.post("/connect")
async def connect_linkedin(
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Start the LinkedIn OAuth 2.0 flow.
    Returns auth_url — open this in a browser so the user can authorise.
    After authorisation LinkedIn will redirect to GET /linkedin/callback.
    """
    if not settings.LINKEDIN_OAUTH_CALLBACK_URL:
        raise HTTPException(status_code=503, detail="LINKEDIN_OAUTH_CALLBACK_URL not configured.")

    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    svc = _linkedin_service()
    state = secrets.token_urlsafe(16)

    await db["linkedin_oauth_pending"].update_one(
        {"state": state},
        {
            "$set": {
                "state": state,
                "user_id": user_id,
                "brand_id": brand_id,
                "expires_at": datetime.utcnow() + timedelta(minutes=10),
            }
        },
        upsert=True,
    )

    auth_url = svc.get_authorization_url(state, settings.LINKEDIN_OAUTH_CALLBACK_URL)

    return {
        "status": True,
        "responseCode": 200,
        "responseData": {"auth_url": auth_url},
    }


# ── Connect — Step 2 (OAuth callback, no JWT) ───────────────────────────────


@router.get("/callback")
async def linkedin_oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    LinkedIn redirects the user's browser here after they authorise (or deny) the app.
    No JWT — the user is identified by the state token stored during /connect.
    Stores per-brand access token then redirects to the frontend.
    """
    web_app_url = settings.WEB_APP_URL.strip("'\"")

    if error or not code or not state:
        return RedirectResponse(
            f"{web_app_url}/settings/social-accounts?connected=false&platform=linkedin&error=access_denied"
        )

    pending = await db["linkedin_oauth_pending"].find_one({"state": state})
    if not pending:
        return RedirectResponse(
            f"{web_app_url}/settings/social-accounts?connected=false&platform=linkedin&error=session_expired"
        )
    if pending.get("expires_at") and datetime.utcnow() > pending["expires_at"]:
        await db["linkedin_oauth_pending"].delete_one({"state": state})
        return RedirectResponse(
            f"{web_app_url}/settings/social-accounts?connected=false&platform=linkedin&error=session_expired"
        )

    user_id = pending["user_id"]
    brand_id = pending.get("brand_id")

    try:
        svc = _linkedin_service()
        token_data = await svc.exchange_code(code, settings.LINKEDIN_OAUTH_CALLBACK_URL)
        access_token = token_data["access_token"]
        profile = await svc.get_profile(access_token)
        pages = await svc.get_admin_pages(access_token)
    except Exception as e:
        return RedirectResponse(
            f"{web_app_url}/settings/social-accounts"
            f"?connected=false&platform=linkedin&error={urllib.parse.quote(str(e))}"
        )
    finally:
        await db["linkedin_oauth_pending"].delete_one({"state": state})

    from app.models.brand_account import BrandAccount
    is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)

    sub = profile.get("sub", "")
    person_urn = f"urn:li:person:{sub}"
    name = profile.get("name", "")
    email = profile.get("email", "")

    now = datetime.utcnow()
    expires_in_seconds = token_data.get("expires_in", 5184000)
    token_expires_at = now + timedelta(seconds=int(expires_in_seconds))

    doc_id = f"linkedin_{user_id}" if is_personal else f"linkedin_{brand_id}"

    set_fields = {
        "id": doc_id,
        "user_id": user_id,
        "platform": "linkedin",
        "username": email or name,
        "account_name": name,
        "network_unique_id": sub,
        "person_urn": person_urn,
        "active_author_urn": person_urn,
        "pages": pages,
        "connection_status": "active",
        "connected_via": "linkedin_direct",
        "linkedin_access_token": access_token,
        "token_expires_at": token_expires_at,
        "connected_at": now,
        "created_at": now,
        "updated_at": now,
    }
    if not is_personal:
        set_fields["brand_id"] = brand_id

    try:
        await db["social_connections"].update_one(
            {"id": doc_id},
            {"$set": set_fields},
            upsert=True,
        )
    except Exception as db_err:
        import traceback as _tb
        print(f"[linkedin/callback] DB upsert error: {db_err}\n{_tb.format_exc()}")
        return RedirectResponse(
            f"{web_app_url}/settings/social-accounts"
            f"?connected=false&platform=linkedin&error={urllib.parse.quote(str(db_err))}"
        )

    return RedirectResponse(
        f"{web_app_url}/settings/social-accounts"
        f"?connected=true&platform=linkedin&username={urllib.parse.quote(name)}"
    )


# ── Disconnect ───────────────────────────────────────────────────────────────


@router.delete("/connect")
async def disconnect_linkedin(
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Disconnect the authenticated user's LinkedIn account for the active brand."""
    conn = await _get_linkedin_connection(ctx["user_id"], db, ctx["brand_id"])
    if not conn:
        raise HTTPException(status_code=400, detail="No LinkedIn account connected.")

    await db["social_connections"].delete_one({"_id": conn["_id"]})

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "LinkedIn account disconnected.",
    }


# ── Status ───────────────────────────────────────────────────────────────────


@router.get("/status")
async def linkedin_status(
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Returns whether the active brand has an active LinkedIn account connected."""
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    conn = await _get_linkedin_connection(user_id, db, brand_id)

    from app.models.brand_account import BrandAccount
    is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)
    outstand_filter = {"platform": "linkedin", "connected_via": "outstand", "connection_status": "active"}
    outstand_filter.update({"user_id": user_id} if is_personal else {"brand_id": brand_id})
    outstand_conn = await db["social_connections"].find_one(outstand_filter)

    linked = conn is not None or outstand_conn is not None
    active = outstand_conn or conn

    return {
        "status": True,
        "responseCode": 200,
        "responseData": {
            "linked": linked,
            "username": active.get("username") if active else None,
            "account_name": active.get("account_name") if active else None,
            "connected_at": active.get("connected_at") if active else None,
            "connected_via": active.get("connected_via") if active else None,
            "active_author_urn": conn.get("active_author_urn") if conn else None,
            "pages": conn.get("pages", []) if conn else [],
        },
    }


# ── Publish ───────────────────────────────────────────────────────────────────


class PublishRequest(BaseModel):
    content: str


@router.post("/publish")
async def publish_to_linkedin(
    body: PublishRequest,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Publish a post to LinkedIn for the active brand.
    Prefers Outstand connection (supports org/company pages) over direct connection.
    """
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]

    from app.models.brand_account import BrandAccount
    is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)
    outstand_filter = {"platform": "linkedin", "connected_via": "outstand", "connection_status": "active"}
    outstand_filter.update({"user_id": user_id} if is_personal else {"brand_id": brand_id})

    outstand_conn = await db["social_connections"].find_one(outstand_filter)
    if outstand_conn:
        from app.agents.social_media_manager.services.outstand_service import OutstandService
        try:
            outstand = OutstandService()
            result = await outstand.publish_post(
                outstand_account_ids=[outstand_conn["outstand_account_id"]],
                content=body.content,
            )
            post_id = result.get("data", {}).get("id") or result.get("id", "")
            return {
                "status": True,
                "responseCode": 200,
                "responseMessage": "Post published to LinkedIn successfully.",
                "responseData": {"post_id": post_id, "content": body.content},
            }
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to post to LinkedIn: {e}")

    conn = await _get_linkedin_connection(user_id, db, brand_id)
    if not conn:
        raise HTTPException(
            status_code=400,
            detail="No LinkedIn account connected. Use POST /social-media/connect with platform=linkedin (supports company pages) or POST /linkedin/connect.",
        )

    access_token = conn.get("linkedin_access_token")
    author_urn = conn.get("active_author_urn") or conn.get("person_urn")
    if not access_token or not author_urn:
        raise HTTPException(
            status_code=400,
            detail="LinkedIn connection is missing tokens. Please reconnect your account.",
        )

    svc = _linkedin_service()

    try:
        result = await svc.create_post(access_token, author_urn, body.content)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to post to LinkedIn: {e}")

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Post published to LinkedIn successfully.",
        "responseData": {
            "post_id": result.get("post_id"),
            "content": body.content,
        },
    }


# ── Company pages ─────────────────────────────────────────────────────────────


@router.get("/pages")
async def get_linkedin_pages(
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Returns the list of LinkedIn company pages the user admins,
    plus the currently active posting target.
    """
    conn = await _get_linkedin_connection(ctx["user_id"], db, ctx["brand_id"])
    if not conn:
        raise HTTPException(status_code=400, detail="No LinkedIn account connected.")

    return {
        "status": True,
        "responseCode": 200,
        "responseData": {
            "personal_profile": {
                "urn": conn.get("person_urn"),
                "name": conn.get("account_name"),
                "type": "personal",
            },
            "pages": conn.get("pages", []),
            "active_author_urn": conn.get("active_author_urn") or conn.get("person_urn"),
        },
    }


class SelectPageRequest(BaseModel):
    author_urn: str  # urn:li:person:xxx  or  urn:li:organization:xxx


@router.post("/pages/select")
async def select_linkedin_page(
    body: SelectPageRequest,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Set the active posting target to either the personal profile or a company page.
    author_urn must be the person_urn or one of the page URNs returned by GET /linkedin/pages.
    """
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    conn = await _get_linkedin_connection(user_id, db, brand_id)
    if not conn:
        raise HTTPException(status_code=400, detail="No LinkedIn account connected.")

    valid_urns = {conn.get("person_urn")} | {p["urn"] for p in conn.get("pages", [])}
    if body.author_urn not in valid_urns:
        raise HTTPException(status_code=400, detail="Invalid author_urn — not linked to this account.")

    from app.models.brand_account import BrandAccount
    is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)
    update_filter = {"user_id": user_id, "platform": "linkedin"} if is_personal else {"brand_id": brand_id, "platform": "linkedin"}

    await db["social_connections"].update_one(
        update_filter,
        {"$set": {"active_author_urn": body.author_urn, "updated_at": datetime.utcnow()}},
    )

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Active posting target updated.",
        "responseData": {"active_author_urn": body.author_urn},
    }


# ── Daily push (cron) ────────────────────────────────────────────────────────


async def _send_linkedin_daily_push(db: AsyncIOMotorDatabase) -> dict:
    """
    Background task: for every user with an active LinkedIn connection, generate one
    post and publish it directly via the LinkedIn API.
    """
    from app.agents.social_media_manager.services.whatsapp_session_service import (
        WhatsAppSessionService,
    )
    from app.domain.models.chat_model import ChatMessage, ChatModel
    from app.services.AIService import AIService

    cursor = db["social_connections"].find(
        {"platform": "linkedin", "connection_status": "active"},
        {"user_id": 1, "linkedin_access_token": 1, "person_urn": 1, "account_name": 1},
    )
    connections = await cursor.to_list(length=None)

    sent = 0
    failed = 0

    for conn in connections:
        user_id = conn.get("user_id")
        access_token = conn.get("linkedin_access_token")
        person_urn = conn.get("person_urn")

        if not access_token or not person_urn:
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
                        f"Write 1 engaging LinkedIn post (under 1300 characters) for {brand_name} "
                        f"in the {industry} space. Be professional yet conversational. No hashtags."
                    ),
                )
            ]
            request = ChatModel(model="gpt-4o-mini", messages=messages, temperature=0.9)
            result = await AIService.chat_completion(request)

            if isinstance(result, dict) and result.get("error"):
                raise Exception(result["error"])

            post_text = result.choices[0].message.content.strip()

            svc = LinkedInDirectService()
            await svc.create_post(access_token, person_urn, post_text)
            sent += 1
        except Exception as e:
            print(f"LinkedIn daily push failed for user {user_id}: {e}")
            failed += 1

    return {"sent": sent, "failed": failed, "total": len(connections)}


@router.post("/daily-push")
async def linkedin_daily_push(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Trigger daily LinkedIn content push for all connected users.
    Protected by X-Cron-Secret header.
    """
    import asyncio

    cron_secret = request.headers.get("X-Cron-Secret", "")
    expected = getattr(settings, "CRON_SECRET", "")
    if expected and cron_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden.")

    asyncio.create_task(_send_linkedin_daily_push(db))
    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "LinkedIn daily push queued.",
    }
