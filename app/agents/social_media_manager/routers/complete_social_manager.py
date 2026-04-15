# app/agents/social_media_manager/routers/complete_social_manager.py

import asyncio
import json
import traceback
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Request, UploadFile, File
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, AsyncGenerator
from datetime import datetime

from app.dependencies import get_db_dependency
from app.core.auth_bearer import JWTBearer
from app.core.config import settings
from app.domain.responses.uri_response import UriResponse

from ..services.content_generation_service import ContentGenerationService
from ..services.image_content_service import ImageContentService
from ..services.social_account_service import SocialAccountService
from ..services.approval_workflow_service import ApprovalWorkflowService
from ..services.auto_content_service import AutoContentService
from ..services.brand_profile_service import BrandProfileService
from ..services.outstand_service import OutstandService
from ..services import content_calendar_service as cal_svc

router = APIRouter(tags=["Social Media Manager"])


def _get_user_id(token: dict) -> str | None:
    """
    Extract user_id from JWT payload.
    Supports common shapes:
      - {"user_id": "..."} or {"userId": "..."} or {"id": "..."} or {"sub": "..."}
      - {"claims": {"userId": "..."}} or {"claims": {"user_id": "..."}}
    """
    if not isinstance(token, dict):
        return None

    # 1) flat keys (top-level)
    for k in ("user_id", "userId", "id", "sub"):
        v = token.get(k)
        if v:
            return str(v)

    # 2) nested claims
    claims = token.get("claims") or {}
    if isinstance(claims, dict):
        for k in ("userId", "user_id", "id", "sub"):
            v = claims.get(k)
            if v:
                return str(v)

    return None

# Pydantic models for requests
class BrandContextRequest(BaseModel):
    brand_name: Optional[str] = None
    brand_colors: Optional[List[str]] = None
    brand_voice: Optional[str] = None
    target_audience: Optional[str] = None
    business_description: Optional[str] = None
    tagline: Optional[str] = None
    industry: Optional[str] = None
    key_products_services: Optional[List[str]] = None

class ContentGenerationRequest(BaseModel):
    seed_content: str = Field(..., min_length=10, max_length=5000)
    platforms: List[str] = Field(..., min_items=1, max_items=5)
    seed_type: str = "text"
    include_images: bool = False
    brand_context: Optional[BrandContextRequest] = None
    reference_image: Optional[str] = None  # base64 data URL uploaded by user for contextual reference
    post_type: str = "feed"   # feed | carousel | story
    num_slides: int = 3        # carousel only (2–5)

class SocialConnectionRequest(BaseModel):
    platforms: List[str] = Field(..., min_items=1, max_items=10)
    source: str = "onboarding"  # "onboarding" | "settings"

class FinalizeConnectionRequest(BaseModel):
    session_token: str
    selected_page_ids: List[str] = Field(..., min_items=1)

class ApprovalRequest(BaseModel):
    draft_ids: List[str] = Field(..., min_items=1)
    schedule_option: str = "save_draft"  # immediate, schedule, save_draft
    scheduled_datetime: Optional[datetime] = None
    approval_notes: Optional[str] = None

class DenialRequest(BaseModel):
    draft_ids: List[str] = Field(..., min_items=1)
    denial_reason: str
    request_regeneration: bool = False

class RefinementRequest(BaseModel):
    draft_id: str
    refinements: Dict[str, Any]

class AutoGenerateSettingsRequest(BaseModel):
    enabled: bool
    platforms: List[str] = ["facebook", "instagram"]
    frequency: str = "daily"
    include_images: bool = False
    brand_context: Optional[BrandContextRequest] = None

class ConnectInsightsRequest(BaseModel):
    influencer_id: str
    platform: str                         # "instagram" | "facebook" | "linkedin"
    social_user_id: Optional[str] = None
    insights: Dict[str, Any]              # full AiMediaReportDto payload

class SchedulingRequest(BaseModel):
    draft_ids: List[str] = Field(..., min_items=1)
    scheduled_datetime: datetime
    timezone: str = "UTC"

class GuardrailsRequest(BaseModel):
    avoid_topics: Optional[str] = None
    banned_words: Optional[str] = None
    emoji_usage: Optional[str] = None
    max_hashtags: Optional[str] = None
    compliance_notes: Optional[str] = None

class KeyDateRequest(BaseModel):
    date: str
    label: str

class TeamMemberRequest(BaseModel):
    email: str
    role: str = "Editor"

class BrandProfileRequest(BaseModel):
    # Basics
    brand_name: Optional[str] = None
    industry: Optional[str] = None
    website: Optional[str] = None
    tagline: Optional[str] = None
    product_description: Optional[str] = None
    key_products_services: Optional[List[str]] = None
    # Identity
    logo_url: Optional[str] = None
    brand_colors: Optional[List[str]] = None
    sample_template_urls: Optional[List[str]] = None
    # Personality
    personality_quiz: Optional[Dict[str, str]] = None
    derived_voice: Optional[str] = None
    voice_sample: Optional[str] = None
    platform_tones: Optional[Dict[str, str]] = None
    same_tone_everywhere: Optional[bool] = True
    # Content strategy
    content_pillars: Optional[List[str]] = None
    preferred_formats: Optional[List[str]] = None
    guardrails: Optional[GuardrailsRequest] = None
    cta_styles: Optional[List[str]] = None
    default_link: Optional[str] = None
    # Audience
    audience_age_range: Optional[str] = None
    target_platforms: Optional[List[str]] = None
    primary_goal: Optional[str] = None
    # Competitors
    competitor_handles: Optional[List[str]] = None
    # Scheduling
    key_dates: Optional[List[KeyDateRequest]] = None
    posting_cadence: Optional[str] = None
    posting_time_mode: Optional[str] = None
    posting_time_prefs: Optional[Dict[str, str]] = None
    # Approval
    approval_workflow: Optional[str] = None
    approval_channels: Optional[List[str]] = None
    notification_events: Optional[List[str]] = None
    notification_channel: Optional[str] = None
    # Team
    team_members: Optional[List[TeamMemberRequest]] = None
    # Localisation
    languages: Optional[List[str]] = None
    region: Optional[str] = None
    # Meta
    onboarding_completed: Optional[bool] = False

# ==============================================================================
# CONTENT GENERATION ENDPOINTS
# ==============================================================================

@router.post("/generate-content")
async def generate_content(
    request: ContentGenerationRequest,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Generate AI-powered social media content with optional images.

    Text content is always returned immediately.
    When include_images=True, images are generated in the background and
    saved to the draft — the frontend can pick them up via GET /content-calendar.

    PRD Credit System:
    - Deducts 1 credit per campaign generation
    - Blocks if credits = 0 (PRD 8: Credit Exhaustion Behavior)
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # ==================== PRD 7.2 & 8: Credit Check ====================
        # Check trial credits first, then paid credits
        from app.services.CreditService import credit_service
        from app.services.TrialService import trial_service

        is_trial_user = await trial_service.has_active_trial(user_id)

        if not is_trial_user:
            # Paid user path — check subscription/bonus credits
            has_credits = await credit_service.check_sufficient_credits(user_id)
            if not has_credits:
                # PRD 8: "You've run out of credits. Upgrade to continue."
                return JSONResponse(
                    status_code=402,
                    content={
                        "status": False,
                        "responseCode": 402,
                        "responseMessage": "You've run out of credits. Upgrade to continue.",
                        "responseData": {
                            "credits_remaining": 0,
                            "upgrade_url": "/pricing"
                        }
                    }
                )

        # Load brand profile from onboarding (source of truth).
        profile_result = await BrandProfileService.get(user_id, db)
        profile_data = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        brand_context_dict = BrandProfileService.to_brand_context(profile_data) if profile_data else {}

        # Allow explicit overrides from the request (legacy / power-user path)
        if request.brand_context:
            overrides = request.brand_context.dict(exclude_none=True)
            brand_context_dict = {**brand_context_dict, **overrides}

        post_type = request.post_type or "feed"
        num_slides = max(2, min(5, request.num_slides or 3))

        if post_type == "carousel":
            from ..services.carousel_generation_service import CarouselGenerationService
            result = await CarouselGenerationService.generate_multi_platform(
                user_id=user_id,
                seed_content=request.seed_content,
                platforms=request.platforms,
                brand_context=brand_context_dict,
                num_slides=num_slides,
                db=db,
            )
        else:
            # story uses standard text gen but we tag it after; feed is unchanged
            result = await ContentGenerationService.generate_multi_platform_content(
                user_id=user_id,
                seed_content=request.seed_content,
                platforms=request.platforms,
                seed_type=request.seed_type,
                brand_context=brand_context_dict,
                db=db,
            )

        # Tag post_type on all resulting drafts in DB
        if result.get("status") and post_type != "feed":
            drafts = result.get("responseData", {}).get("drafts", [])
            draft_ids = [d["id"] for d in drafts if d.get("id")]
            if draft_ids:
                await db["content_drafts"].update_many(
                    {"id": {"$in": draft_ids}},
                    {"$set": {"post_type": post_type}},
                )
                for d in drafts:
                    d["post_type"] = post_type

        # ==================== PRD 7.2: Credit Deduction ====================
        # Deduct 1 credit after successful generation
        # PRD 3.1: First campaign generation = 1 credit
        if result.get("status"):
            request_id = result.get("responseData", {}).get("request_id")
            if request_id:
                if is_trial_user:
                    await trial_service.deduct_trial_credit(
                        user_id=user_id,
                        campaign_id=request_id,
                        reason="campaign_generation",
                    )
                    print(f"✅ Deducted 1 trial credit from user {user_id} for campaign {request_id}")
                else:
                    await credit_service.deduct_credit(
                        user_id=user_id,
                        campaign_id=request_id,
                        reason="campaign_generation",
                        retry_count=0  # Initial generation (not a retry)
                    )
                    print(f"✅ Deducted 1 credit from user {user_id} for campaign {request_id}")

            # Notification PRD 4.2: Content created notification
            try:
                from app.services.NotificationService import notification_service
                drafts_data = result.get("responseData", {}).get("drafts", [])
                platforms_str = ", ".join(set(d.get("platform", "") for d in drafts_data if d.get("platform")))
                preview = drafts_data[0].get("content", "")[:120] if drafts_data else ""
                background_tasks.add_task(
                    notification_service.notify_content_created,
                    user_id=user_id,
                    content_preview=preview,
                    platforms=platforms_str,
                    campaign_id=request_id or "",
                )
            except Exception as e:
                print(f"⚠️ Content created notification failed: {e}")

        # If images were requested, mark drafts as has_image=True immediately so the
        # frontend shimmer shows right away, then kick off background generation.
        if request.include_images and result.get("status"):
            drafts = result.get("responseData", {}).get("drafts", [])
            draft_ids = [d["id"] for d in drafts if d.get("id")]
            if draft_ids:
                await db["content_drafts"].update_many(
                    {"id": {"$in": draft_ids}},
                    {"$set": {"has_image": True}},
                )
                # Mirror the flag in the response so the frontend sees it immediately
                for d in drafts:
                    d["has_image"] = True

            for draft in drafts:
                if post_type == "carousel":
                    slides = draft.get("slides") or []
                    for slide_index, slide in enumerate(slides):
                        background_tasks.add_task(
                            _generate_image_bg,
                            draft_id=draft["id"],
                            platform=draft["platform"],
                            content=f"{slide.get('headline', '')} {slide.get('body', '')}".strip() or draft["content"],
                            seed_content=request.seed_content,
                            brand_context=brand_context_dict,
                            db=db,
                            reference_image=request.reference_image,
                            post_type=post_type,
                            slide_index=slide_index,
                        )
                else:
                    background_tasks.add_task(
                        _generate_image_bg,
                        draft_id=draft["id"],
                        platform=draft["platform"],
                        content=draft["content"],
                        seed_content=request.seed_content,
                        brand_context=brand_context_dict,
                        db=db,
                        reference_image=request.reference_image,
                        post_type=post_type,
                    )

        return result

    except Exception as e:
        error_detail = str(e) or repr(e)
        print(f"❌ generate_content error for user={user_id}: {error_detail}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=error_detail)

@router.post("/regenerate-content/{draft_id}")
async def regenerate_content(
    draft_id: str,
    feedback: Optional[str] = None,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """Regenerate content with optional feedback"""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    
    try:
        result = await ApprovalWorkflowService.regenerate_content(
            db=db,
            user_id=user_id,
            draft_id=draft_id,
            regeneration_feedback=feedback
        )
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================================================================
# SOCIAL ACCOUNT CONNECTION ENDPOINTS
# ==============================================================================

@router.post("/connect/initiate")
async def initiate_social_connections(
    request: SocialConnectionRequest,
    token: dict = Depends(JWTBearer()),
):
    """
    Step 1 of the social connection flow (onboarding step 2).

    Returns Outstand OAuth URLs for each requested platform.
    The frontend opens each auth_url so the user can authorise.
    After authorisation, Outstand redirects back to /connect/callback/outstand.

    Supported platforms: facebook, instagram, linkedin, x/twitter,
    tiktok, youtube, pinterest, threads, bluesky, google_business
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    return await SocialAccountService.initiate_connection_flow(
        user_id=user_id,
        platforms=request.platforms,
        source=request.source,
    )


@router.get("/connect/instagram-direct/initiate")
async def instagram_direct_initiate(source: Optional[str] = Query("settings")):
    """
    Redirect the user's browser to Facebook's OAuth page to connect Instagram
    via the Instagram API with Facebook Login. On completion, Facebook redirects to
    /connect/instagram-direct/callback where we retrieve the linked Instagram
    Business Account from the user's Facebook Pages.
    """
    import urllib.parse

    app_id = settings.META_APP_ID
    if not app_id:
        raise HTTPException(status_code=500, detail="META_APP_ID not configured")

    _base = (settings.PUBLIC_API_URL or settings.URI_GATEWAY_BASE_API_URL).rstrip("/")
    redirect_uri = f"{_base}/social-media/connect/instagram-direct/callback"

    scopes = [
        "instagram_basic",
        "pages_show_list",
        "pages_read_engagement",
        "instagram_manage_insights",
        "instagram_content_publish",
    ]
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes),
        "response_type": "code",
        "state": source or "settings",
    }
    auth_url = "https://www.facebook.com/v20.0/dialog/oauth?" + urllib.parse.urlencode(params)
    return RedirectResponse(auth_url)


@router.get("/connect/instagram-direct/callback")
async def instagram_direct_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_reason: Optional[str] = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Facebook OAuth callback for Instagram connection.
    Exchanges the auth code for a Facebook user token, retrieves the user's
    Facebook Pages, finds the Instagram Business Account linked to each page,
    and stores the connection as connected_via=instagram_direct_oauth.
    No JWT required — the user arrives here via browser redirect from Facebook.
    """
    import urllib.parse
    import httpx
    from datetime import timezone

    source = state or "settings"
    is_settings = source == "settings"
    web_app_url = settings.WEB_APP_URL
    base_redirect = (
        f"{web_app_url}/settings/social-accounts"
        if is_settings
        else f"{web_app_url}/social-media/brand-setup"
    )

    if error:
        msg = urllib.parse.quote(error_reason or error)
        return RedirectResponse(f"{base_redirect}?connected=false&error={msg}")

    if not code:
        return RedirectResponse(f"{base_redirect}?connected=false&error=missing_code")

    _base = (settings.PUBLIC_API_URL or settings.URI_GATEWAY_BASE_API_URL).rstrip("/")
    redirect_uri = f"{_base}/social-media/connect/instagram-direct/callback"
    app_id = settings.META_APP_ID
    app_secret = settings.META_APP_SECRET

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: exchange code → short-lived Facebook user access token
            token_resp = await client.post(
                "https://graph.facebook.com/v20.0/oauth/access_token",
                data={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
            )
            token_data = token_resp.json()
            print(f"[IGDirectOAuth] short-lived token response: {token_data}")
            if "error" in token_data:
                err = token_data["error"].get("message", "token exchange failed")
                return RedirectResponse(f"{base_redirect}?connected=false&error={urllib.parse.quote(err)}")
            short_token = token_data.get("access_token")

            # Step 2: exchange short-lived → long-lived user token (60 days)
            ll_resp = await client.get(
                "https://graph.facebook.com/v20.0/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "fb_exchange_token": short_token,
                },
            )
            ll_data = ll_resp.json()
            print(f"[IGDirectOAuth] long-lived token response: {ll_data}")
            long_token = ll_data.get("access_token", short_token)

            # Step 3: get the user's Facebook Pages (each has a page access token)
            pages_resp = await client.get(
                "https://graph.facebook.com/v20.0/me/accounts",
                params={"access_token": long_token},
            )
            pages_data = pages_resp.json()
            print(f"[IGDirectOAuth] pages: {pages_data}")
            pages = pages_data.get("data", [])
            if not pages:
                err_msg = "No Facebook Pages found. Link your Instagram Business Account to a Facebook Page first."
                return RedirectResponse(f"{base_redirect}?connected=false&error={urllib.parse.quote(err_msg)}")

            # Step 4: find Instagram Business Account linked to one of the pages
            ig_user_id = None
            username = None
            profile_picture_url = None
            page_token = None

            for page in pages:
                pid = page["id"]
                ptok = page.get("access_token", long_token)
                ig_resp = await client.get(
                    f"https://graph.facebook.com/v20.0/{pid}",
                    params={"fields": "instagram_business_account", "access_token": ptok},
                )
                ig_data = ig_resp.json()
                ig_account = ig_data.get("instagram_business_account")
                if ig_account:
                    ig_user_id = str(ig_account["id"])
                    page_token = ptok
                    # Step 5: fetch Instagram profile
                    profile_resp = await client.get(
                        f"https://graph.facebook.com/v20.0/{ig_user_id}",
                        params={"fields": "id,username,name,profile_picture_url", "access_token": ptok},
                    )
                    profile = profile_resp.json()
                    print(f"[IGDirectOAuth] ig profile: {profile}")
                    username = profile.get("username", ig_user_id)
                    profile_picture_url = profile.get("profile_picture_url")
                    break

            if not ig_user_id:
                err_msg = "No Instagram Business Account found linked to your Facebook Pages."
                return RedirectResponse(f"{base_redirect}?connected=false&error={urllib.parse.quote(err_msg)}")

        # Step 6: store in social_connections
        now = datetime.now(timezone.utc).isoformat()
        conn_doc = {
            "id": ig_user_id,
            "user_id": None,  # matched when user calls /connections after redirect
            "platform": "instagram",
            "connected_via": "instagram_direct_oauth",
            "ig_user_id": ig_user_id,
            "page_access_token": page_token,
            "username": username,
            "account_name": username,
            "profile_picture_url": profile_picture_url,
            "connection_status": "pending_user_match",
            "connected_at": now,
            "updated_at": now,
        }
        await db["social_connections"].update_one(
            {"ig_user_id": ig_user_id, "connected_via": "instagram_direct_oauth"},
            {"$set": conn_doc},
            upsert=True,
        )
        print(f"[IGDirectOAuth] ✅ Stored @{username} (ig_user_id={ig_user_id}) pending user match")

        params_out = (
            f"connected=instagram_direct"
            f"&ig_user_id={urllib.parse.quote(ig_user_id)}"
            f"&username={urllib.parse.quote(username)}"
        )
        return RedirectResponse(f"{base_redirect}?{params_out}")

    except Exception as e:
        print(f"[IGDirectOAuth] ❌ Error: {e}")
        return RedirectResponse(
            f"{base_redirect}?connected=false&error={urllib.parse.quote(str(e))}"
        )


@router.post("/connect/instagram-direct/finalize")
async def instagram_direct_finalize(
    ig_user_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Called by the frontend after the Instagram direct OAuth callback to
    associate the pending connection with the authenticated user.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    result = await db["social_connections"].update_one(
        {"ig_user_id": ig_user_id, "connected_via": "instagram_direct_oauth", "connection_status": "pending_user_match"},
        {"$set": {"user_id": user_id, "connection_status": "active", "updated_at": datetime.utcnow().isoformat()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Pending Instagram connection not found")

    return UriResponse.get_single_data_response("instagram_connected", {"ig_user_id": ig_user_id})


@router.get("/connect/callback/outstand")
async def outstand_oauth_callback(
    sessionToken: Optional[str] = Query(None),
    session_token: Optional[str] = Query(None),
    session: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    network_unique_id: Optional[str] = Query(None),
    network: Optional[str] = Query(None),
    success: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
):
    """
    OAuth callback — Outstand redirects the user's browser here after they
    authorise on the social platform. No JWT required.

    Two possible flows:
    1. Session token flow (Facebook, LinkedIn etc.):
       Outstand sends sessionToken → redirect frontend to pending/finalize.
    2. Direct flow (X/Twitter OAuth 2.0):
       Outstand sends account_id + username directly → redirect frontend
       with account details so it can call POST /x/finalize-direct.

    source: "onboarding" → redirect to brand-setup, "settings" → redirect to settings/social-accounts
    """
    import urllib.parse

    web_app_url = settings.WEB_APP_URL
    is_settings = source == "settings"
    base_redirect = f"{web_app_url}/settings/social-accounts" if is_settings else f"{web_app_url}/social-media/brand-setup"

    if error:
        encoded_error = urllib.parse.quote(error)
        return RedirectResponse(f"{base_redirect}?connected=false&error={encoded_error}")

    # Direct flow — X OAuth 2.0 returns account_id immediately
    if success == "true" and account_id:
        params = f"account_id={urllib.parse.quote(account_id)}&connected=direct"
        if username:
            params += f"&username={urllib.parse.quote(username)}"
        if network_unique_id:
            params += f"&network_unique_id={urllib.parse.quote(network_unique_id)}"
        if network:
            params += f"&network={urllib.parse.quote(network)}"
        return RedirectResponse(f"{base_redirect}?{params}")

    # Session token flow
    token_value = sessionToken or session_token or session
    if not token_value:
        return RedirectResponse(f"{base_redirect}?connected=false&error=missing_session_token")

    return RedirectResponse(
        f"{base_redirect}?sessionToken={urllib.parse.quote(token_value)}&connected=pending"
    )


@router.get("/connect/pending/{session_token}")
async def get_pending_connection(
    session_token: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Step 2 of the social connection flow.

    Returns the list of pages/accounts the user can connect for this session.
    The frontend shows these to the user for selection before finalising.
    Session tokens expire — call this promptly after the OAuth callback.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    return await SocialAccountService.get_pending_connection(session_token, db=db)


@router.post("/connect/finalize")
async def finalize_social_connection(
    request: FinalizeConnectionRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Step 3 of the social connection flow (completes onboarding step 2).

    Finalises the OAuth connection for the selected pages/accounts.
    Stores the connected account IDs locally for publishing.
    Call GET /connections after this to see all connected accounts.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    return await SocialAccountService.finalize_connection(
        db=db,
        user_id=user_id,
        session_token=request.session_token,
        selected_page_ids=request.selected_page_ids,
    )


@router.get("/connections")
async def get_user_connections(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Get all social media accounts connected by the user (live from Outstand)."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    return await SocialAccountService.get_user_connections(db=db, user_id=user_id)


@router.delete("/connections/account/{outstand_account_id}")
async def disconnect_social_account(
    outstand_account_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Permanently disconnect a social account.
    Use the outstand_account_id returned by GET /connections.
    This revokes OAuth tokens — the user must reconnect via /connect/initiate.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    return await SocialAccountService.disconnect_account(
        db=db,
        user_id=user_id,
        outstand_account_id=outstand_account_id,
    )


# ==============================================================================
# ONBOARDING ENDPOINTS
# ==============================================================================

@router.get("/onboarding/status")
async def get_onboarding_status(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Returns the user's onboarding completion state.

    step_1_complete — brand profile saved
    step_2_complete — at least one social account connected
    current_step    — the step the user should be on (1, 2, or null if done)
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    return await SocialAccountService.get_onboarding_status(db=db, user_id=user_id)

# ==============================================================================
# APPROVAL WORKFLOW ENDPOINTS
# ==============================================================================

@router.post("/approve")
async def approve_content(
    request: ApprovalRequest,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Approve content drafts with scheduling options
    
    Options:
    - immediate: Publish right away
    - schedule: Schedule for specific time
    - save_draft: Just approve without publishing
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    
    try:
        result = await ApprovalWorkflowService.approve_content(
            db=db,
            user_id=user_id,
            draft_ids=request.draft_ids,
            schedule_option=request.schedule_option,
            scheduled_datetime=request.scheduled_datetime,
            approval_notes=request.approval_notes
        )

        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/drafts/{draft_id}/regenerate-image")
async def regenerate_draft_image(
    draft_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Regenerate the image for an existing draft using user feedback.
    PRD 3.2 & 4.2: Image Retry Rules
    - First retry: FREE
    - Second retry: 1 credit (requires confirmation)

    Clears the current image immediately (frontend shows shimmer),
    then generates a new image in the background incorporating the feedback.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    body = await request.json()
    feedback = (body.get("feedback") or "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="feedback is required")

    # PRD 3.3: Check if user confirmed second retry (optional - for second retry)
    confirmed = body.get("confirmed", False)

    # Verify the draft belongs to this user and get retry count
    draft = await db["content_drafts"].find_one(
        {"$or": [{"id": draft_id}, {"draft_id": draft_id}], "user_id": user_id},
        {"_id": 0, "id": 1, "image_retry_count": 1, "request_id": 1},
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # PRD 4.3: Track image retry count per campaign
    image_retry_count = draft.get("image_retry_count", 0)

    # PRD 3.2 & 4.2: First retry is FREE, second retry requires 1 credit
    if image_retry_count >= 1:
        # This is the second or later retry - requires credit
        # PRD 3.3: Show confirmation prompt before second retry
        if not confirmed:
            return JSONResponse(
                status_code=200,
                content={
                    "status": True,
                    "responseCode": 200,
                    "responseMessage": "This action will use 1 credit. Continue?",
                    "responseData": {
                        "requires_confirmation": True,
                        "requires_credit": True,
                        "image_retry_count": image_retry_count,
                        "message": "This action will use 1 credit. Continue?"
                    }
                }
            )

        # User confirmed - check and deduct credit
        from app.services.CreditService import credit_service
        from app.services.TrialService import trial_service

        is_trial_user = await trial_service.has_active_trial(user_id)

        if is_trial_user:
            request_id = draft.get("request_id", draft_id)
            deducted = await trial_service.deduct_trial_credit(
                user_id=user_id,
                campaign_id=request_id,
                reason="image_retry",
            )
        else:
            has_credits = await credit_service.check_sufficient_credits(user_id, required=1)
            if not has_credits:
                # PRD 8: Out of credits - block action
                return JSONResponse(
                    status_code=402,
                    content={
                        "status": False,
                        "responseCode": 402,
                        "responseMessage": "You've run out of credits. Upgrade to continue.",
                        "responseData": {
                            "credits_remaining": 0,
                            "upgrade_url": "/pricing"
                        }
                    }
                )

            # Deduct 1 credit for second retry
            request_id = draft.get("request_id", draft_id)
            deducted = await credit_service.deduct_credit(
                user_id=user_id,
                campaign_id=request_id,
                reason="image_retry",
                retry_count=image_retry_count
            )

        if not deducted:
            return JSONResponse(
                status_code=402,
                content={
                    "status": False,
                    "responseCode": 402,
                    "responseMessage": "Failed to deduct credit. Please try again.",
                    "responseData": {}
                }
            )

    # PRD 4.3: Increment image retry count and clear image
    from datetime import datetime as _dt
    await db["content_drafts"].update_one(
        {"$or": [{"id": draft_id}, {"draft_id": draft_id}]},
        {
            "$set": {
                "image_url": None,
                "has_image": True,
                "updated_at": _dt.utcnow()
            },
            "$inc": {"image_retry_count": 1}  # Track retry count
        },
    )

    from app.agents.social_media_manager.services.image_content_service import ImageContentService
    background_tasks.add_task(
        ImageContentService.regenerate_image_for_draft,
        draft_id=draft_id,
        user_id=user_id,
        feedback=feedback,
        db=db,
    )

    from app.domain.responses.uri_response import UriResponse
    return UriResponse.get_single_data_response(
        "regenerate_image",
        {
            "draft_id": draft_id,
            "status": "generating",
            "image_retry_count": image_retry_count + 1,
            "credit_deducted": image_retry_count >= 1  # True if credit was deducted
        }
    )


@router.delete("/drafts/{draft_id}")
async def delete_draft(
    draft_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """Permanently delete a content draft"""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    result = await db["content_drafts"].delete_one({"id": draft_id, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Draft not found")

    from app.domain.responses.uri_response import UriResponse
    return UriResponse.delete_response("draft", is_deleted=True)


@router.post("/deny")
async def deny_content(
    request: DenialRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """Deny content drafts with optional regeneration request"""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    
    try:
        result = await ApprovalWorkflowService.deny_content(
            db=db,
            user_id=user_id,
            draft_ids=request.draft_ids,
            denial_reason=request.denial_reason,
            request_regeneration=request.request_regeneration
        )
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/drafts/{draft_id}/unschedule")
async def unschedule_draft(
    draft_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Move a scheduled draft back to draft status."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    draft = await db["content_drafts"].find_one({"id": draft_id})
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    await db["content_drafts"].update_one(
        {"id": draft_id},
        {"$set": {
            "status": "draft",
            "scheduled_date": None,
            "platform_post_id": None,
            "user_id": user_id,  # ensure user_id is on the doc so fetchDrafts finds it
            "updated_at": datetime.utcnow(),
        }},
    )
    from app.domain.responses.uri_response import UriResponse
    return UriResponse.get_single_data_response("unschedule", {"draft_id": draft_id, "status": "draft"})


@router.put("/refine")
async def refine_content(
    request: RefinementRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Refine/edit content before approval
    
    Allows users to:
    - Edit text content
    - Modify hashtags
    - Add/remove media
    - Add refinement notes
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    
    try:
        result = await ApprovalWorkflowService.refine_content(
            db=db,
            user_id=user_id,
            draft_id=request.draft_id,
            refinements=request.refinements
        )
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================================================================
# SCHEDULING ENDPOINTS
# ==============================================================================

@router.post("/schedule")
async def schedule_content(
    request: SchedulingRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """Schedule approved content for future publishing"""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    
    try:
        result = await ApprovalWorkflowService.schedule_content(
            db=db,
            user_id=user_id,
            draft_ids=request.draft_ids,
            scheduled_datetime=request.scheduled_datetime,
            timezone=request.timezone
        )
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/scheduled")
async def get_scheduled_content(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """Get all scheduled content for the user"""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    
    try:
        # Query scheduled drafts directly by user_id (same pattern as /content-calendar).
        # Also fall back to request_id lookup for older drafts that may not have user_id
        # stored directly on the draft document.
        requests = await db["content_requests"].find({"user_id": user_id}, {"id": 1}).to_list(length=200)
        request_ids = [req["id"] for req in requests if req.get("id")]

        scheduled_drafts = await db["content_drafts"].find({
            "$or": [
                {"user_id": user_id, "status": "scheduled"},
                {"request_id": {"$in": request_ids}, "status": "scheduled"},
            ]
        }).sort("scheduled_date", 1).to_list(length=100)

        # Deduplicate by draft id in case both conditions match the same doc
        seen = set()
        unique_drafts = []
        for draft in scheduled_drafts:
            key = draft.get("id") or str(draft.get("_id", ""))
            if key not in seen:
                seen.add(key)
                draft.pop("_id", None)
                unique_drafts.append(draft)

        return UriResponse.get_single_data_response("scheduled_content", {
            "user_id": user_id,
            "scheduled_drafts": unique_drafts,
            "total_scheduled": len(unique_drafts)
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================================================================
# CONTENT CALENDAR PLAN ENDPOINTS (Issues 5, 6, 7)
# ==============================================================================

class CalendarGenerateRequest(BaseModel):
    platforms: List[str] = ["facebook", "instagram"]
    force_regenerate: bool = False


class CalendarCreateDraftRequest(BaseModel):
    platforms: List[str] = ["facebook", "instagram"]
    include_images: bool = False


@router.get("/content-calendar/plan")
async def get_calendar_plan(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Return the active 7-day plan for this week, or 404 if none exists."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    plan = await cal_svc.get_active_plan(user_id, db)
    if not plan:
        raise HTTPException(status_code=404, detail="No active plan for this week")
    return UriResponse.get_single_data_response("calendar_plan", plan)


@router.post("/content-calendar/plan/generate")
async def generate_calendar_plan(
    request: CalendarGenerateRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Generate (or force-regenerate) the 7-day content plan for this week."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    try:
        profile_result = await BrandProfileService.get(user_id, db)
        brand = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        plan = await cal_svc.generate_plan(
            user_id=user_id,
            platforms=request.platforms,
            brand=brand,
            db=db,
            force=request.force_regenerate,
        )
        return UriResponse.get_single_data_response("calendar_plan", plan)
    except Exception as e:
        import traceback as _tb
        print(_tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/content-calendar/plan/{plan_id}/day/{day_index}/regenerate")
async def regenerate_calendar_day(
    plan_id: str,
    day_index: int,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Regenerate the content idea for a single day."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    try:
        updated_plan = await cal_svc.regenerate_day(plan_id, day_index, user_id, db)
        return UriResponse.get_single_data_response("calendar_plan", updated_plan)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        import traceback as _tb
        print(_tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/content-calendar/plan/{plan_id}/day/{day_index}/create-draft")
async def create_draft_from_calendar_day(
    plan_id: str,
    day_index: int,
    request: CalendarCreateDraftRequest,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Create a full content draft from a calendar day's idea."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    try:
        plan = await db["content_calendar_plans"].find_one(
            {"plan_id": plan_id, "user_id": user_id}, {"_id": 0}
        )
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        day = next((d for d in plan["days"] if d["day_index"] == day_index), None)
        if not day:
            raise HTTPException(status_code=404, detail=f"Day {day_index} not found")

        seed_content = f"{day['title']}. {day['description']}"
        profile_result = await BrandProfileService.get(user_id, db)
        brand = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        brand_context = BrandProfileService.to_brand_context(brand) if brand else {}

        result = await ContentGenerationService.generate_multi_platform_content(
            user_id=user_id,
            seed_content=seed_content,
            platforms=request.platforms,
            seed_type="calendar_idea",
            brand_context=brand_context,
            db=db,
        )

        if result.get("status"):
            drafts = result.get("responseData", {}).get("drafts", [])
            draft_ids = [d.get("draft_id") or d.get("id") for d in drafts if d]
            await cal_svc.mark_acted_on(plan_id, day_index, draft_ids, user_id, db)

            if request.include_images:
                draft_ids = [d.get("draft_id") or d.get("id") for d in drafts if d]
                if draft_ids:
                    await db["content_drafts"].update_many(
                        {"id": {"$in": draft_ids}},
                        {"$set": {"has_image": True}},
                    )
                    for d in drafts:
                        d["has_image"] = True

                for d in drafts:
                    background_tasks.add_task(
                        _generate_image_bg,
                        draft_id=d.get("draft_id") or d.get("id"),
                        platform=d.get("platform", "facebook"),
                        content=d.get("content", seed_content),
                        seed_content=seed_content,
                        brand_context=brand_context,
                        db=db,
                        reference_image=None,
                    )

        return result
    except HTTPException:
        raise
    except Exception as e:
        import traceback as _tb
        print(_tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/content-calendar/today")
async def get_today_suggestion(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Return today's content suggestion from the active plan."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    result = await cal_svc.get_today_suggestion(user_id, db)
    return UriResponse.get_single_data_response("today_suggestion", result)


# ==============================================================================
# CONTENT MANAGEMENT ENDPOINTS
# ==============================================================================

@router.get("/content-calendar")
async def get_content_calendar(
    status: Optional[str] = None,
    platform: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Get user's complete content calendar

    Shows all content across different statuses:
    - draft, pending_approval, approved, scheduled, published
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Query drafts directly by user_id (stored on every draft document)
        query: Dict[str, Any] = {"user_id": user_id}

        if status:
            query["status"] = status
        if platform:
            query["platform"] = platform

        # Use aggregation to avoid transferring large base64 image_url values from
        # MongoDB to the app server. For base64 images the pipeline emits a relative
        # proxy path (/uri-insights/social-media/draft-image/<id>) so the browser
        # fetches the image through the same gateway it uses for all API calls —
        # avoiding any ngrok/tunnel interstitial or domain-mismatch issues.
        pipeline = [
            {"$match": query},
            {"$sort": {"created_at": -1}},
            {"$skip": skip},
            {"$limit": limit},
            {"$addFields": {
                "_img_is_base64": {
                    "$eq": [{"$substr": [{"$ifNull": ["$image_url", ""]}, 0, 5]}, "data:"]
                },
                "_img_exists": {
                    "$gt": [{"$strLenCP": {"$ifNull": ["$image_url", ""]}}, 0]
                },
                "_draft_key": {"$ifNull": ["$id", "$draft_id"]},
            }},
            {"$addFields": {
                "has_image": {"$or": ["$_img_exists", {"$eq": ["$has_image", True]}]},
                "image_url": {
                    "$cond": {
                        "if": "$_img_is_base64",
                        # Return a relative path — the frontend prepends its own API base URL
                        "then": {"$concat": [
                            "/social-media/draft-image/", "$_draft_key"
                        ]},
                        "else": {"$cond": {
                            "if": "$_img_exists",
                            "then": "$image_url",
                            "else": None
                        }}
                    }
                },
            }},
            {"$project": {"_id": 0, "_img_is_base64": 0, "_img_exists": 0, "_draft_key": 0}},
        ]

        drafts = await db["content_drafts"].aggregate(pipeline).to_list(length=limit)

        # Replace relative image proxy paths with full absolute URLs.
        # URI_GATEWAY_BASE_API_URL may be an internal Docker URL so we fall
        # back to URI_PUBLIC_API_URL (set in .env) or omit the image entirely
        # for base64 drafts that haven't been uploaded to imgBB yet.
        public_url = (
            getattr(settings, "URI_PUBLIC_API_URL", None)
            or getattr(settings, "URI_GATEWAY_BASE_API_URL", None)
            or ""
        ).rstrip("/")
        for draft in drafts:
            img = draft.get("image_url") or ""
            if img.startswith("/uri-insights"):
                if public_url and not public_url.startswith("http://uri-gateway"):
                    draft["image_url"] = f"{public_url}{img}"
                else:
                    # Can't serve an internal URL to the browser — hide the image
                    draft["image_url"] = None

        total_count = await db["content_drafts"].count_documents(query)

        return UriResponse.get_single_data_response("content_calendar", {
            "user_id": user_id,
            "drafts": drafts,
            "total_count": total_count,
            "filters_applied": {
                "status": status,
                "platform": platform
            },
            "pagination": {
                "skip": skip,
                "limit": limit,
                "has_more": (skip + limit) < total_count
            }
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/analytics")
async def get_content_analytics(
    platform: Optional[str] = None,
    days: int = 30,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """Get content performance analytics"""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    
    try:
        # Get user's published content
        user_requests = await db["content_requests"].find({"user_id": user_id}).to_list(length=200)
        request_ids = [req["id"] for req in user_requests]
        
        query = {
            "request_id": {"$in": request_ids},
            "status": "published"
        }
        
        if platform:
            query["platform"] = platform
        
        # Filter by date range
        from datetime import timedelta
        date_filter = datetime.utcnow() - timedelta(days=days)
        query["published_date"] = {"$gte": date_filter}
        
        published_drafts = await db["content_drafts"].find(query).to_list(length=200)
        
        # Get analytics for published content
        analytics_data = []
        for draft in published_drafts:
            analytics = await db["content_analytics"].find_one({"draft_id": draft["id"]})
            if analytics:
                if "_id" in analytics:
                    del analytics["_id"]
                analytics_data.append({
                    "draft_id": draft["id"],
                    "platform": draft["platform"],
                    "published_date": draft["published_date"],
                    "analytics": analytics
                })
        
        return UriResponse.get_single_data_response("content_analytics", {
            "user_id": user_id,
            "analytics_data": analytics_data,
            "total_published": len(published_drafts),
            "date_range_days": days,
            "platform_filter": platform
        })
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================================================================
# PERFORMANCE / ANALYTICS
# ==============================================================================

@router.get("/performance")
async def get_performance(
    days: int = 30,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Fetch real-time post analytics from Outstand for the user's published drafts.
    Returns aggregated summary + per-post breakdown + per-platform summary.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    from datetime import timedelta
    import asyncio as _asyncio
    import httpx as _httpx

    try:
        date_filter = datetime.utcnow() - timedelta(days=days)
        published = await db["content_drafts"].find({
            "user_id": user_id,
            "status": "published",
            "platform_post_id": {"$exists": True, "$ne": None},
            "published_date": {"$gte": date_filter},
        }).sort("published_date", -1).to_list(length=50)

        if not published:
            return UriResponse.get_single_data_response("performance", {
                "has_data": False,
                "total_published": 0,
                "date_range_days": days,
                "summary": {},
                "by_platform": {},
                "top_posts": [],
            })

        # Load all direct (non-Outstand) connections so we can use their tokens
        direct_conns = await db["social_connections"].find(
            {"user_id": user_id, "connected_via": {"$ne": "outstand"}},
            {"_id": 0, "platform": 1, "connected_via": 1, "page_access_token": 1, "ig_user_id": 1},
        ).to_list(length=20)
        direct_conn_map = {c["platform"]: c for c in direct_conns}

        outstand = OutstandService()
        _graph_base = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"

        async def _fetch_instagram_direct(draft, conn):
            media_id = draft.get("platform_post_id")
            token = conn.get("page_access_token")
            if not media_id or not token:
                return None
            try:
                async with _httpx.AsyncClient(timeout=20) as _c:
                    media_resp = await _c.get(
                        f"{_graph_base}/{media_id}",
                        params={"fields": "like_count,comments_count,timestamp,media_type", "access_token": token},
                    )
                    media_data = media_resp.json()
                    if "error" in media_data:
                        print(f"⚠️ IG media fetch error for {media_id}: {media_data['error'].get('message')}")
                        return None

                    impressions = reach = 0
                    try:
                        ins_resp = await _c.get(
                            f"{_graph_base}/{media_id}/insights",
                            params={"metric": "impressions,reach", "period": "lifetime", "access_token": token},
                        )
                        for item in ins_resp.json().get("data", []):
                            val = (item.get("values") or [{}])[0].get("value", 0)
                            if item["name"] == "impressions":
                                impressions = val
                            elif item["name"] == "reach":
                                reach = val
                    except Exception:
                        pass

                likes = media_data.get("like_count", 0)
                comments = media_data.get("comments_count", 0)
                return {
                    "draft_id": draft.get("id"),
                    "platform_post_id": media_id,
                    "platform": "instagram",
                    "content_preview": (draft.get("content") or "")[:120],
                    "published_at": draft.get("published_date", ""),
                    "image_url": draft.get("image_url"),
                    "likes": likes,
                    "comments": comments,
                    "shares": 0,
                    "views": 0,
                    "impressions": impressions,
                    "reach": reach,
                    "engagement_rate": round(((likes + comments) / max(reach, 1)) * 100, 2) if reach else 0,
                }
            except Exception as e:
                print(f"⚠️ Instagram direct analytics failed for {media_id}: {e}")
                return None

        async def _fetch(draft):
            post_id = draft.get("platform_post_id")
            platform = draft.get("platform", "unknown")
            if not post_id:
                return None

            # Route to direct Graph API for non-Outstand platforms
            if platform in direct_conn_map:
                return await _fetch_instagram_direct(draft, direct_conn_map[platform])

            try:
                data = await outstand.get_post_analytics(post_id)
                agg = data.get("aggregated_metrics") or {}
                by_account = data.get("metrics_by_account") or []
                network = by_account[0]["social_account"]["network"] if by_account else platform
                return {
                    "draft_id": draft.get("id"),
                    "platform_post_id": post_id,
                    "platform": network or platform,
                    "content_preview": (draft.get("content") or "")[:120],
                    "published_at": draft.get("published_date", ""),
                    "image_url": draft.get("image_url"),
                    "likes": agg.get("total_likes", 0),
                    "comments": agg.get("total_comments", 0),
                    "shares": agg.get("total_shares", 0),
                    "views": agg.get("total_views", 0),
                    "impressions": agg.get("total_impressions", 0),
                    "reach": agg.get("total_reach", 0),
                    "engagement_rate": round(agg.get("average_engagement_rate", 0) * 100, 2),
                }
            except Exception as e:
                print(f"⚠️ Analytics fetch failed for post {post_id}: {e}")
                return None

        results = await _asyncio.gather(*[_fetch(d) for d in published])
        posts = [r for r in results if r is not None]

        # Aggregate summary
        def _sum(key): return sum(p.get(key, 0) or 0 for p in posts)
        total_posts = len(posts)
        summary = {
            "total_posts": total_posts,
            "total_impressions": _sum("impressions"),
            "total_reach": _sum("reach"),
            "total_likes": _sum("likes"),
            "total_comments": _sum("comments"),
            "total_shares": _sum("shares"),
            "total_views": _sum("views"),
            "avg_engagement_rate": round(
                sum(p.get("engagement_rate", 0) or 0 for p in posts) / total_posts, 2
            ) if total_posts else 0,
        }

        # Per-platform breakdown
        by_platform: dict = {}
        for p in posts:
            pl = p["platform"]
            if pl not in by_platform:
                by_platform[pl] = {"posts": 0, "impressions": 0, "reach": 0, "likes": 0, "comments": 0, "shares": 0, "engagement_rate_sum": 0}
            by_platform[pl]["posts"] += 1
            for k in ("impressions", "reach", "likes", "comments", "shares"):
                by_platform[pl][k] += p.get(k, 0) or 0
            by_platform[pl]["engagement_rate_sum"] += p.get("engagement_rate", 0) or 0
        for pl, data in by_platform.items():
            n = data["posts"]
            data["avg_engagement_rate"] = round(data.pop("engagement_rate_sum") / n, 2) if n else 0

        # Top posts by impressions
        top_posts = sorted(posts, key=lambda x: x.get("impressions", 0), reverse=True)[:10]

        return UriResponse.get_single_data_response("performance", {
            "has_data": True,
            "total_published": len(published),
            "date_range_days": days,
            "summary": summary,
            "by_platform": by_platform,
            "top_posts": top_posts,
        })

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/account-metrics")
async def get_account_metrics(
    days: int = 30,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Fetch account-level metrics (followers, engagement totals) for all of the
    user's connected social accounts via Outstand.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    import asyncio as _asyncio
    import time as _time
    import httpx as _httpx
    from datetime import timedelta

    try:
        outstand = OutstandService()
        until_ts = int(_time.time())
        since_ts = int((datetime.utcnow() - timedelta(days=days)).timestamp())

        # ── Outstand accounts ─────────────────────────────────────────────────
        result = await outstand.list_accounts(tenant_id=user_id)
        outstand_accounts = result.get("data", [])

        async def _fetch_outstand_metrics(acc):
            account_id = acc.get("id")
            if not account_id:
                return None
            try:
                data = await outstand.get_account_metrics(account_id, since=since_ts, until=until_ts)
                m = data.get("data", {})
                eng = m.get("engagement")
                period = m.get("period") or {}
                ps = m.get("platform_specific") or {}
                return {
                    "account_id": account_id,
                    "network": acc.get("network", "unknown"),
                    "page_name": ps.get("page_name") or acc.get("nickname") or acc.get("username"),
                    "category": ps.get("category"),
                    "followers_count": m.get("followers_count") or ps.get("followers_count") or ps.get("fan_count") or 0,
                    "following_count": m.get("following_count"),
                    "posts_count": m.get("posts_count"),
                    "engagement": {
                        "views": eng.get("views", 0),
                        "likes": eng.get("likes", 0),
                        "comments": eng.get("comments", 0),
                        "shares": eng.get("shares", 0),
                        "reposts": eng.get("reposts", 0),
                        "quotes": eng.get("quotes", 0),
                    } if eng else None,
                    "engagement_note": period.get("note"),
                    "platform_specific": ps,
                    "period": {"since": since_ts, "until": until_ts},
                }
            except Exception as e:
                print(f"⚠️ Outstand account metrics failed for {account_id}: {e}")
                return None

        # ── Direct (non-Outstand) connections ─────────────────────────────────
        direct_conns = await db["social_connections"].find(
            {"user_id": user_id, "connected_via": {"$ne": "outstand"}},
            {"_id": 0, "platform": 1, "connected_via": 1, "page_access_token": 1, "ig_user_id": 1, "page_name": 1},
        ).to_list(length=20)

        _graph_base = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"

        async def _fetch_instagram_direct_metrics(conn):
            ig_user_id = conn.get("ig_user_id")
            token = conn.get("page_access_token")
            if not ig_user_id or not token:
                return None
            try:
                async with _httpx.AsyncClient(timeout=20) as _c:
                    profile_resp = await _c.get(
                        f"{_graph_base}/{ig_user_id}",
                        params={"fields": "name,username,followers_count,media_count,biography,profile_picture_url", "access_token": token},
                    )
                    profile = profile_resp.json()
                    if "error" in profile:
                        print(f"⚠️ IG profile fetch error: {profile['error'].get('message')}")
                        return None

                    # Aggregate engagement from published posts in the period
                    posts_resp = await _c.get(
                        f"{_graph_base}/{ig_user_id}/media",
                        params={"fields": "like_count,comments_count,timestamp", "limit": 50, "access_token": token},
                    )
                    posts = posts_resp.json().get("data", [])
                    from datetime import timezone as _tz
                    since_dt = datetime.utcfromtimestamp(since_ts).replace(tzinfo=_tz.utc)
                    total_likes = total_comments = 0
                    for post in posts:
                        try:
                            from datetime import datetime as _dt
                            post_dt = _dt.fromisoformat(post["timestamp"].replace("Z", "+00:00"))
                            if post_dt >= since_dt:
                                total_likes += post.get("like_count", 0)
                                total_comments += post.get("comments_count", 0)
                        except Exception:
                            pass

                return {
                    "account_id": ig_user_id,
                    "network": "instagram",
                    "page_name": profile.get("name") or profile.get("username") or conn.get("page_name"),
                    "category": None,
                    "followers_count": profile.get("followers_count", 0),
                    "following_count": None,
                    "posts_count": profile.get("media_count"),
                    "engagement": {
                        "views": 0,
                        "likes": total_likes,
                        "comments": total_comments,
                        "shares": 0,
                        "reposts": 0,
                        "quotes": 0,
                    },
                    "engagement_note": f"Likes + comments on posts published in the last {days} days",
                    "platform_specific": {
                        "username": profile.get("username"),
                        "biography": profile.get("biography"),
                        "profile_picture_url": profile.get("profile_picture_url"),
                    },
                    "period": {"since": since_ts, "until": until_ts},
                }
            except Exception as e:
                print(f"⚠️ Instagram direct account metrics failed: {e}")
                return None

        async def _fetch_direct_metrics(conn):
            if conn.get("connected_via") in ("instagram_direct", "instagram_direct_oauth"):
                return await _fetch_instagram_direct_metrics(conn)
            return None  # extend here for other direct platforms

        outstand_results, direct_results = await _asyncio.gather(
            _asyncio.gather(*[_fetch_outstand_metrics(a) for a in outstand_accounts]),
            _asyncio.gather(*[_fetch_direct_metrics(c) for c in direct_conns]),
        )

        account_metrics = [r for r in (*outstand_results, *direct_results) if r is not None]

        return UriResponse.get_single_data_response("account_metrics", {
            "has_data": len(account_metrics) > 0,
            "accounts": account_metrics,
        })

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# UTILITY ENDPOINTS
# ==============================================================================

@router.get("/debug/outstand-account-metrics/{account_id}")
async def debug_outstand_account_metrics(
    account_id: str,
    days: int = 30,
    token: dict = Depends(JWTBearer())
):
    """Return raw Outstand account metrics response for debugging field mapping."""
    import time as _time
    from datetime import timedelta
    try:
        until_ts = int(_time.time())
        since_ts = int((datetime.utcnow() - timedelta(days=days)).timestamp())
        outstand = OutstandService()
        data = await outstand.get_account_metrics(account_id, since=since_ts, until=until_ts)
        return UriResponse.get_single_data_response("debug_account_metrics", data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/debug/outstand-post/{post_id}")
async def debug_outstand_post(
    post_id: str,
    token: dict = Depends(JWTBearer())
):
    """
    Fetch the live status of an Outstand post by its ID.
    Use this to diagnose why a post appears queued but hasn't appeared on the social network.
    """
    try:
        outstand = OutstandService()
        data = await outstand.get_post(post_id)
        return UriResponse.get_single_data_response("outstand_post", data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/platform-requirements/{platform}")
async def get_platform_requirements(platform: str):
    """Get content requirements for a specific platform"""
    try:
        requirements = ContentGenerationService.get_platform_requirements(platform)
        
        if requirements:
            return UriResponse.get_single_data_response("platform_requirements", {
                "platform": platform,
                "requirements": requirements
            })
        else:
            raise HTTPException(status_code=404, detail=f"Platform {platform} not supported")
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/draft-image/{draft_id}")
async def get_draft_image(
    draft_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Serve the AI-generated image for a draft as a binary response.
    Used by Outstand to fetch image media when publishing.
    No auth required — URL is unguessable via draft_id.
    """
    from fastapi.responses import Response
    import base64, re

    draft = await db["content_drafts"].find_one({"id": draft_id}, {"image_url": 1})
    if not draft or not draft.get("image_url"):
        raise HTTPException(status_code=404, detail="Image not found")

    data_url = draft["image_url"]
    match = re.match(r"data:([^;]+);base64,(.+)", data_url, re.DOTALL)
    if not match:
        raise HTTPException(status_code=422, detail="Invalid image data")

    mime_type = match.group(1)
    image_bytes = base64.b64decode(match.group(2))
    return Response(content=image_bytes, media_type=mime_type)


@router.get("/drafts/{draft_id}")
async def get_draft(
    draft_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Get a single draft by ID — poll this to check if an image has been generated."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    draft = await db["content_drafts"].find_one({"id": draft_id, "user_id": user_id}, {"_id": 0})
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Serve base64 images via proxy path (same logic as content-calendar)
    img = draft.get("image_url") or ""
    if img.startswith("data:"):
        draft["image_url"] = f"/social-media/draft-image/{draft_id}"
        draft["has_image"] = True

    return UriResponse.get_single_data_response("draft", draft)


@router.get("/drafts/{draft_id}/image-stream")
async def stream_draft_image(
    draft_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    SSE stream — sends a `image_ready` event with the draft payload the moment
    an image lands on the draft. Times out after 3 minutes.

    Frontend usage:
        const es = new EventSource(`/social-media/drafts/${draftId}/image-stream?token=...`)
        es.addEventListener('image_ready', e => { const draft = JSON.parse(e.data); es.close() })
        es.addEventListener('timeout', () => es.close())
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    async def event_stream() -> AsyncGenerator[str, None]:
        poll_interval = 2   # seconds between DB checks
        max_wait = 180      # seconds before giving up
        elapsed = 0

        yield "event: connected\ndata: {}\n\n"

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            draft = await db["content_drafts"].find_one(
                {"id": draft_id, "user_id": user_id},
                {"_id": 0}
            )
            if draft and draft.get("has_image") and draft.get("image_url"):
                img = draft.get("image_url", "")
                if img.startswith("data:"):
                    draft["image_url"] = f"/social-media/draft-image/{draft_id}"
                payload = json.dumps(draft)
                yield f"event: image_ready\ndata: {payload}\n\n"
                return

            yield f"event: waiting\ndata: {elapsed}\n\n"

        yield "event: timeout\ndata: Image generation took too long\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/test")
async def test_endpoint():
    """Simple test endpoint"""
    return {
        "message": "Social Media Manager Agent is working!",
        "status": "success",
        "features": [
            "AI Content Generation",
            "Social Account Connection", 
            "Approval Workflow",
            "Content Scheduling",
            "Image Generation",
            "Analytics Tracking"
        ],
        "timestamp": datetime.utcnow().isoformat()
    }

# ==============================================================================
# AUTO CONTENT GENERATION ENDPOINTS
# ==============================================================================

@router.get("/auto-generate/settings")
async def get_auto_generate_settings(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Get (or create) the auto-content generation settings for the current user."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    try:
        doc = await AutoContentService.get_or_create_settings(user_id, db)
        return UriResponse.get_single_data_response("auto_generate_settings", doc)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/auto-generate/settings")
async def update_auto_generate_settings(
    request: AutoGenerateSettingsRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Update auto-content generation settings (upsert)."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    try:
        payload = request.dict(exclude_none=True)
        if request.brand_context:
            payload["brand_context"] = request.brand_context.dict(exclude_none=True)
        doc = await AutoContentService.update_settings(user_id, payload, db)
        return UriResponse.get_single_data_response("auto_generate_settings", doc)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/auto-generate/connect-insights")
async def connect_insights(
    request: ConnectInsightsRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Persist the AiMediaReport generated by account tracking analysis.
    Called automatically by the frontend after the AI media report loads.
    Stores the data in account_analytics_context so the auto-content service
    can use industry, content themes, engagement drivers, and the weekly
    campaign calendar as richer context when generating posts.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    try:
        await AutoContentService.save_analytics_context(
            user_id=user_id,
            influencer_id=request.influencer_id,
            platform=request.platform,
            social_user_id=request.social_user_id,
            insights=request.insights,
            db=db,
        )
        return UriResponse.get_single_data_response(
            "connect_insights", {"saved": True, "platform": request.platform}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/auto-generate/trigger")
async def trigger_auto_generate(
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Manually trigger auto-content generation for the current user."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    background_tasks.add_task(_run_auto_generate_background, user_id, db)
    return UriResponse.get_single_data_response(
        "auto_generate_trigger",
        {"message": "Auto-content generation started in background", "user_id": user_id},
    )


# ==============================================================================
# BRAND PROFILE ENDPOINTS
# ==============================================================================

@router.get("/brand-profile")
async def get_brand_profile(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Get the brand profile (onboarding data) for the current user."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    try:
        return await BrandProfileService.get(user_id, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/brand-profile/logo")
async def upload_brand_logo(
    file: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Upload a brand logo image. Stores it to imgBB and saves the public URL
    to the user's brand profile. Accepted formats: PNG, JPG, WEBP, SVG.
    """
    import base64
    import httpx

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    allowed_types = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/svg+xml"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}. Use PNG, JPG, WEBP, or SVG.")

    try:
        contents = await file.read()
        if len(contents) > 5 * 1024 * 1024:  # 5 MB limit
            raise HTTPException(status_code=400, detail="Logo file must be under 5 MB.")

        b64 = base64.b64encode(contents).decode()

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.imgbb.com/1/upload",
                data={"key": settings.IMGBB_API_KEY, "image": b64},
            )
            resp_json = resp.json()

        if not resp_json.get("success"):
            raise HTTPException(status_code=502, detail=f"Image host upload failed: {resp_json.get('error', {}).get('message', 'unknown error')}")

        logo_url = resp_json["data"]["url"]

        await db["brand_profiles"].update_one(
            {"user_id": user_id},
            {"$set": {"logo_url": logo_url, "updated_at": datetime.utcnow()}},
            upsert=True,
        )

        return UriResponse.get_single_data_response("logo_upload", {"logo_url": logo_url})

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/brand-profile/sample-template")
async def upload_sample_template(
    file: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Upload a sample design or content template. Stores it to imgBB and appends
    the public URL to the user's brand profile sample_template_urls list.
    Accepted formats: PNG, JPG, WEBP, PDF. Max 10 MB per file.
    """
    import base64
    import httpx

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    allowed_types = {"image/png", "image/jpeg", "image/jpg", "image/webp", "application/pdf"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {file.content_type}. Use PNG, JPG, WEBP, or PDF.",
        )

    try:
        contents = await file.read()
        if len(contents) > 10 * 1024 * 1024:  # 10 MB limit
            raise HTTPException(status_code=400, detail="File must be under 10 MB.")

        b64 = base64.b64encode(contents).decode()

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.imgbb.com/1/upload",
                data={"key": settings.IMGBB_API_KEY, "image": b64, "name": file.filename},
            )
            resp_json = resp.json()

        if not resp_json.get("success"):
            raise HTTPException(
                status_code=502,
                detail=f"File host upload failed: {resp_json.get('error', {}).get('message', 'unknown error')}",
            )

        file_url = resp_json["data"]["url"]

        await db["brand_profiles"].update_one(
            {"user_id": user_id},
            {
                "$push": {"sample_template_urls": file_url},
                "$set": {"updated_at": datetime.utcnow()},
            },
            upsert=True,
        )

        return UriResponse.get_single_data_response("sample_template_upload", {"file_url": file_url})

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"❌ sample-template upload error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/brand-profile")
async def save_brand_profile(
    request: BrandProfileRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Save or update the brand profile for the current user."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    try:
        payload = request.dict(exclude_none=True)
        # Serialize nested Pydantic models to plain dicts
        if "guardrails" in payload and hasattr(payload["guardrails"], "dict"):
            payload["guardrails"] = payload["guardrails"].dict(exclude_none=True)
        if "key_dates" in payload:
            payload["key_dates"] = [
                kd.dict() if hasattr(kd, "dict") else kd
                for kd in payload["key_dates"]
            ]
        if "team_members" in payload:
            payload["team_members"] = [
                tm.dict() if hasattr(tm, "dict") else tm
                for tm in payload["team_members"]
            ]
        return await BrandProfileService.save(user_id, payload, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# BACKGROUND TASKS
# ==============================================================================

async def _generate_image_bg(
    draft_id: str,
    platform: str,
    content: str,
    seed_content: str,
    brand_context: Dict[str, Any],
    db: AsyncIOMotorDatabase,
    reference_image: Optional[str] = None,
    post_type: str = "feed",
    slide_index: Optional[int] = None,
):
    """
    Background task: generate an image for an existing draft and save it to DB.
    Runs after the text-only response has already been returned to the frontend.
    For carousel posts, slide_index indicates which slide this image belongs to.
    For story posts, uses the story (9:16) image spec.
    """
    import re
    import base64
    import httpx
    from app.core.config import settings as _cfg

    try:
        # For story posts pass image_type="story" so we get 1080x1920 dimensions
        image_type = "story" if post_type == "story" else "post_image"

        image_result = await ImageContentService._generate_platform_image(
            platform=platform,
            content=content,
            seed_content=seed_content,
            brand_context=brand_context,
            reference_image=reference_image,
            image_type=image_type,
        )

        if not image_result.get("status"):
            print(f"⚠️ BG image gen failed for draft {draft_id}: {image_result.get('responseMessage')}")
            return

        raw_url = image_result["responseData"]["image_url"]
        stored_url = raw_url

        # Upload base64 image to imgBB for a public URL
        if raw_url and raw_url.startswith("data:"):
            try:
                match = re.match(r"data:[^;]+;base64,(.+)", raw_url, re.DOTALL)
                if match and _cfg.IMGBB_API_KEY:
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.post(
                            "https://api.imgbb.com/1/upload",
                            data={"key": _cfg.IMGBB_API_KEY, "image": match.group(1)},
                        )
                        rj = resp.json()
                    if rj.get("success"):
                        stored_url = rj["data"]["url"]
                        print(f"☁️  BG image uploaded to imgBB: {stored_url}")
                    else:
                        print(f"⚠️  BG imgBB upload failed: {rj.get('error')}")
            except Exception as upload_err:
                print(f"⚠️  BG imgBB upload error: {upload_err}")

        final_url = stored_url if not stored_url.startswith("data:") else None
        if final_url and db is not None:
            if post_type == "carousel" and slide_index is not None:
                # Update the specific slide's image_url in the slides array
                result = await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {"$set": {
                        f"slides.{slide_index}.image_url": final_url,
                        "has_image": True,
                    }},
                )
                print(f"✅ BG carousel slide {slide_index} image saved for draft {draft_id}: matched={result.matched_count}")
            else:
                result = await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {"$set": {"image_url": final_url, "has_image": True}},
                )
                print(f"✅ BG image saved for draft {draft_id}: matched={result.matched_count}")
        else:
            print(f"⚠️  BG image not saved for draft {draft_id} (no public URL)")

    except Exception as e:
        print(f"❌ BG image task error for draft {draft_id}: {e}\n{traceback.format_exc()}")


async def publish_content_background(db: AsyncIOMotorDatabase, user_id: str, draft_ids: List[str]):
    """Background task for immediate content publishing"""
    try:
        result = await ApprovalWorkflowService._trigger_immediate_publishing(
            db=db,
            user_id=user_id,
            draft_ids=draft_ids
        )
        print(f"✅ Background publishing completed: {result}")
        
    except Exception as e:
        print(f"❌ Background publishing failed: {str(e)}")

async def _run_auto_generate_background(user_id: str, db: AsyncIOMotorDatabase):
    """Background wrapper for auto-content generation (returns result via logs)."""
    try:
        result = await AutoContentService.generate_for_user(user_id, db)
        print(f"✅ Auto-generate complete for user={user_id}: {result}")
    except Exception as e:
        print(f"❌ Auto-generate failed for user={user_id}: {e}")


# This can be set up as a periodic background task
async def scheduled_content_publisher(db: AsyncIOMotorDatabase):
    """Periodic task to publish scheduled content"""
    try:
        result = await ApprovalWorkflowService.publish_scheduled_content(db=db)
        if result.get("published_count", 0) > 0:
            print(f"✅ Published {result['published_count']} scheduled posts")

    except Exception as e:
        print(f"❌ Scheduled publishing failed: {str(e)}")


@router.post("/publish-scheduled")
async def trigger_publish_scheduled(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Cron endpoint — called every 5 minutes to check and update scheduled posts.
    Protected by X-Cron-Secret header.
    """
    from app.core.config import settings as _cfg
    expected = getattr(_cfg, "CRON_SECRET", "") or ""
    cron_secret = request.headers.get("X-Cron-Secret", "")
    if expected and cron_secret != expected:
        raise HTTPException(status_code=403, detail="Invalid cron secret")

    result = await ApprovalWorkflowService.publish_scheduled_content(db=db)
    return result
