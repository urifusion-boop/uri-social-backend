# app/agents/social_media_manager/routers/complete_social_manager.py

import asyncio
import json
import traceback
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, UploadFile, File
from fastapi.responses import RedirectResponse, StreamingResponse
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

class SocialConnectionRequest(BaseModel):
    platforms: List[str] = Field(..., min_items=1, max_items=10)

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
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Load brand profile from onboarding (source of truth).
        profile_result = await BrandProfileService.get(user_id, db)
        profile_data = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        brand_context_dict = BrandProfileService.to_brand_context(profile_data) if profile_data else {}

        # Allow explicit overrides from the request (legacy / power-user path)
        if request.brand_context:
            overrides = request.brand_context.dict(exclude_none=True)
            brand_context_dict = {**brand_context_dict, **overrides}

        # Always generate text synchronously and return immediately.
        result = await ContentGenerationService.generate_multi_platform_content(
            user_id=user_id,
            seed_content=request.seed_content,
            platforms=request.platforms,
            seed_type=request.seed_type,
            brand_context=brand_context_dict,
            db=db,
        )

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
                background_tasks.add_task(
                    _generate_image_bg,
                    draft_id=draft["id"],
                    platform=draft["platform"],
                    content=draft["content"],
                    seed_content=request.seed_content,
                    brand_context=brand_context_dict,
                    db=db,
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
    )


@router.get("/connect/callback/outstand")
async def outstand_oauth_callback(
    sessionToken: Optional[str] = Query(None),
    session_token: Optional[str] = Query(None),
    session: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """
    OAuth callback — Outstand redirects the user's browser here after they
    authorise on the social platform. No JWT required.

    Redirects the user to the frontend brand-setup page with the sessionToken
    so the frontend can call GET /connect/pending/{sessionToken} and then
    POST /connect/finalize to complete the connection.
    """
    import urllib.parse

    web_app_url = settings.WEB_APP_URL
    # Outstand may send the token as "session", "sessionToken", or "session_token"
    token_value = sessionToken or session_token or session

    if error:
        encoded_error = urllib.parse.quote(error)
        return RedirectResponse(
            f"{web_app_url}/social-media/brand-setup"
            f"?connected=false&error={encoded_error}"
        )

    if not token_value:
        return RedirectResponse(
            f"{web_app_url}/social-media/brand-setup"
            f"?connected=false&error=missing_session_token"
        )

    return RedirectResponse(
        f"{web_app_url}/social-media/brand-setup"
        f"?sessionToken={urllib.parse.quote(token_value)}&connected=pending"
    )


@router.get("/connect/pending/{session_token}")
async def get_pending_connection(
    session_token: str,
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

    return await SocialAccountService.get_pending_connection(session_token)


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
        # Get user's content requests to filter by user
        requests = await db["content_requests"].find({"user_id": user_id}).to_list(length=100)
        request_ids = [req["id"] for req in requests]
        
        # Get scheduled drafts for these requests
        scheduled_drafts = await db["content_drafts"].find({
            "request_id": {"$in": request_ids},
            "status": "scheduled"
        }).sort("scheduled_date", 1).to_list(length=100)
        
        # Clean up ObjectIds
        for draft in scheduled_drafts:
            if "_id" in draft:
                del draft["_id"]
        
        return UriResponse.get_single_data_response("scheduled_content", {
            "user_id": user_id,
            "scheduled_drafts": scheduled_drafts,
            "total_scheduled": len(scheduled_drafts)
        })
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
# UTILITY ENDPOINTS
# ==============================================================================

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

        async with httpx.AsyncClient(timeout=30) as client:
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
):
    """
    Background task: generate an image for an existing draft and save it to DB.
    Runs after the text-only response has already been returned to the frontend.
    """
    import re
    import base64
    import httpx
    from app.core.config import settings as _cfg

    try:
        image_result = await ImageContentService._generate_platform_image(
            platform=platform,
            content=content,
            seed_content=seed_content,
            brand_context=brand_context,
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
