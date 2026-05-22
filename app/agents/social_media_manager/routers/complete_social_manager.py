# app/agents/social_media_manager/routers/complete_social_manager.py

import asyncio
import json
import subprocess
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
from ..services.voice_sample_analyzer_service import VoiceSampleAnalyzerService

router = APIRouter(tags=["Social Media Manager"])


def _apply_intelligent_fallbacks(profile_data: Dict[str, Any], missing_fields: List[str]) -> Dict[str, Any]:
    """
    Apply intelligent, brand-safe fallbacks for missing brand profile fields.
    Uses URI Social's brand identity when user's brand is incomplete.

    This prevents hallucinations while maintaining good UX.
    """
    profile = profile_data.copy()

    # URI Social brand colors (sophisticated, professional palette)
    URI_BRAND_COLORS = [
        "#C41E3A",  # Deep magenta (primary)
        "#FFFEF2",  # Ivory white (secondary)
        "#8B1538",  # Darker magenta (accent)
        "#FFE5EC",  # Soft blush (light accent)
    ]

    if "brand_colors" in missing_fields:
        profile["brand_colors"] = URI_BRAND_COLORS
        print(f"🎨 Fallback: Using URI Social brand colors {URI_BRAND_COLORS}")

    if "brand_name" in missing_fields:
        # Use a generic but professional placeholder
        profile["brand_name"] = profile.get("business_name", "Your Brand")
        print(f"📛 Fallback: Using placeholder brand name: {profile['brand_name']}")

    if "industry" in missing_fields:
        # Default to general_other which uses lifestyle_natural style (no text overlays)
        profile["industry"] = "general_other"
        print(f"🏭 Fallback: Using general industry category")

    if "style_selections" not in profile or not profile.get("style_selections"):
        # Default to lifestyle_natural - clean, no text overlays, safe
        profile["style_selections"] = ["lifestyle_natural"]
        profile["style_prompt_fragments"] = []
        profile["style_rotation_index"] = 0
        print(f"🎨 Fallback: Using lifestyle_natural style (no text overlays)")

    if "region" not in profile or not profile.get("region"):
        # Default to West Africa since most users are likely Nigerian
        profile["region"] = "West Africa, Nigeria"
        print(f"🌍 Fallback: Using West Africa, Nigeria as default region")

    return profile


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

class StoryboardRequest(BaseModel):
    brand_images: List[str] = Field(..., min_items=1, max_items=5)
    optional_text: Optional[str] = Field(None, max_length=1000)
    target_platform: str = "instagram_reels"
    target_duration_seconds: int = Field(15, ge=5, le=30)
    video_style: Optional[str] = "clean_commercial"

class StoryboardFramesRequest(BaseModel):
    scenes: List[Dict[str, Any]]
    brand_images: List[str] = Field(default_factory=list, max_items=5)

class PublishVideoDraftRequest(BaseModel):
    draft_id: str
    platform: str   # "instagram_reels" | "facebook_reels"
    caption: Optional[str] = None

class VideoFromStoryboardRequest(BaseModel):
    storyboard: Dict[str, Any]
    brand_images: List[str] = Field(default_factory=list, max_items=5)
    model: str = "veo-3.1-generate-preview"

class ContentGenerationRequest(BaseModel):
    seed_content: str = Field(..., min_length=10, max_length=5000)
    platforms: List[str] = Field(..., min_items=1, max_items=5)
    seed_type: str = "text"
    include_images: bool = False
    image_model: Optional[str] = None  # e.g. "fal-ai/flux-pro/v1.1", "fal-ai/flux/dev", "fal-ai/ideogram/v3", or None (default Imagen)
    brand_context: Optional[BrandContextRequest] = None
    reference_image: Optional[str] = None  # base64 data URL uploaded by user for contextual reference
    post_type: str = "feed"   # feed | carousel | story
    num_slides: int = 3        # carousel only (2–5)
    acknowledged_incomplete_profile: bool = False  # OPTION 1: User acknowledged incomplete profile warning

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
    logo_position: Optional[str] = None  # top_left | top_center | top_right | bottom_left | bottom_center | bottom_right | center
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
    # Visual style
    style_selections: Optional[List[str]] = None
    style_prompt_fragments: Optional[List[str]] = None
    # Typography
    font_style: Optional[str] = None
    font_style_prompt: Optional[str] = None

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

        # ========== OPTION 1: PROGRESSIVE ENFORCEMENT ==========
        # Load brand profile from onboarding (source of truth).
        profile_result = await BrandProfileService.get(user_id, db)
        profile_data = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}

        if not profile_data:
            return UriResponse.error_response(
                f"No brand profile found for user {user_id}. Please complete onboarding first.",
                code=400
            )

        # Check for critical missing fields
        # Empty strings should be treated as missing
        brand_name_value = profile_data.get("brand_name")
        brand_colors_value = profile_data.get("brand_colors")
        industry_value = profile_data.get("industry")

        print(f"\n🔍 BRAND PROFILE DEBUG for user {user_id}:")
        print(f"  brand_name: {repr(brand_name_value)} (empty: {not brand_name_value})")
        print(f"  brand_colors: {repr(brand_colors_value)} (empty: {not (brand_colors_value and len(brand_colors_value) > 0)})")
        print(f"  industry: {repr(industry_value)} (empty: {not industry_value})")

        required_fields = {
            "brand_name": brand_name_value and brand_name_value.strip(),
            "brand_colors": brand_colors_value and len(brand_colors_value) > 0,
            "industry": industry_value and industry_value.strip(),
        }
        missing_fields = [k for k, v in required_fields.items() if not v]

        print(f"  missing_fields: {missing_fields}")
        print(f"  acknowledged_incomplete_profile: {getattr(request, 'acknowledged_incomplete_profile', False)}\n")

        # PROGRESSIVE ENFORCEMENT: Warn but allow generation with intelligent fallbacks
        if missing_fields:
            # Check if user acknowledged incomplete profile
            acknowledged = getattr(request, 'acknowledged_incomplete_profile', False)

            if not acknowledged:
                # Return warning response for frontend modal
                implications = {}
                if "brand_colors" in missing_fields:
                    implications["brand_colors"] = "We'll use URI Social's brand colors (Deep Magenta and Ivory)"
                if "industry" in missing_fields:
                    implications["industry"] = "We'll use generic lifestyle styling"
                if "brand_name" in missing_fields:
                    implications["brand_name"] = "We'll use a placeholder brand name"

                return {
                    "status": False,
                    "responseCode": "INCOMPLETE_PROFILE",
                    "responseMessage": "Your brand profile is incomplete. Complete it for better brand consistency, or continue with safe defaults.",
                    "responseData": {
                        "missing_fields": missing_fields,
                        "implications": implications,
                        "can_proceed": True,
                    }
                }

        # Apply intelligent fallbacks for missing fields
        profile_data = _apply_intelligent_fallbacks(profile_data, missing_fields)

        brand_context_dict = BrandProfileService.to_brand_context(profile_data)
        brand_context_dict["user_id"] = user_id
        brand_context_dict["using_fallbacks"] = len(missing_fields) > 0
        brand_context_dict["fallback_fields"] = missing_fields
        print(f"🖼️  LOGO DEBUG user={user_id}: logo_url={repr(profile_data.get('logo_url'))}, logo_position={repr(profile_data.get('logo_position'))} → brand_context logo_position={repr(brand_context_dict.get('logo_position'))}")

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
                reference_image=request.reference_image,
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
                    # Pass total_slides and carousel_id for visual continuity
                    total_slides = len(slides)
                    carousel_id = draft["id"]  # All slides share same carousel_id
                    for slide_index, slide in enumerate(slides):
                        # Use slide's headline + body as the main content (no seed duplication)
                        slide_content = f"{slide.get('headline', '')} {slide.get('body', '')}".strip()
                        background_tasks.add_task(
                            _generate_image_bg,
                            draft_id=draft["id"],
                            platform=draft["platform"],
                            content=slide_content or draft["content"],
                            seed_content=slide_content,  # Use slide content instead of original seed to avoid duplication
                            brand_context=brand_context_dict,
                            db=db,
                            reference_image=request.reference_image,
                            post_type=post_type,
                            slide_index=slide_index,
                            image_model=request.image_model,
                            total_slides=total_slides,
                            carousel_id=carousel_id,
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
                        image_model=request.image_model,
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


@router.get("/connect/facebook-direct/initiate")
async def facebook_direct_initiate(source: Optional[str] = Query("settings")):
    """
    Redirect the user's browser to Facebook's OAuth page to connect a Facebook Page
    directly (without Outstand). On completion, Facebook redirects to
    /connect/facebook-direct/callback.
    """
    import urllib.parse

    app_id = settings.META_APP_ID
    if not app_id:
        raise HTTPException(status_code=500, detail="META_APP_ID not configured")

    _base = (settings.PUBLIC_API_URL or settings.URI_GATEWAY_BASE_API_URL).rstrip("/")
    redirect_uri = f"{_base}/social-media/connect/facebook-direct/callback"

    scopes = [
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
        "pages_manage_metadata",
    ]
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes),
        "response_type": "code",
        "state": source or "settings",
    }
    auth_url = f"https://www.facebook.com/{settings.FACEBOOK_API_VERSION}/dialog/oauth?" + urllib.parse.urlencode(params)
    return RedirectResponse(auth_url)


@router.get("/connect/facebook-direct/callback")
async def facebook_direct_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_reason: Optional[str] = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Facebook OAuth callback for direct Facebook Page connection.
    Exchanges the auth code for a Page access token, picks the first page,
    stores a pending connection, and redirects back to the workspace.
    """
    import urllib.parse
    import httpx
    from datetime import timezone

    web_app_url = settings.WEB_APP_URL.strip("'\"")
    base_redirect = f"{web_app_url}/workspace?tab=connections"

    if error:
        msg = urllib.parse.quote(error_reason or error)
        return RedirectResponse(f"{base_redirect}&connected=false&error={msg}")

    if not code:
        return RedirectResponse(f"{base_redirect}&connected=false&error=missing_code")

    _base = (settings.PUBLIC_API_URL or settings.URI_GATEWAY_BASE_API_URL).rstrip("/")
    redirect_uri = f"{_base}/social-media/connect/facebook-direct/callback"
    app_id = settings.META_APP_ID
    app_secret = settings.META_APP_SECRET
    graph_base = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: exchange code → short-lived user token
            token_resp = await client.get(
                f"{graph_base}/oauth/access_token",
                params={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
            )
            token_data = token_resp.json()
            if "error" in token_data:
                raise ValueError(f"Token exchange error: {token_data['error'].get('message')}")
            short_token = token_data["access_token"]

            # Step 2: exchange → long-lived user token
            ll_resp = await client.get(
                f"{graph_base}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "fb_exchange_token": short_token,
                },
            )
            ll_data = ll_resp.json()
            long_token = ll_data.get("access_token", short_token)

            # Step 3: get the user's Facebook Pages
            pages_resp = await client.get(
                f"{graph_base}/me/accounts",
                params={"access_token": long_token, "fields": "id,name,access_token,picture"},
            )
            pages_data = pages_resp.json()
            pages = pages_data.get("data", [])

            if not pages:
                err_msg = urllib.parse.quote("No Facebook Pages found. You need a Facebook Page to connect.")
                return RedirectResponse(f"{base_redirect}&connected=false&error={err_msg}")

            # Use the first page (most users have one main page)
            page = pages[0]
            page_id = page["id"]
            page_name = page["name"]
            page_token = page["access_token"]
            profile_pic = page.get("picture", {}).get("data", {}).get("url", "") if isinstance(page.get("picture"), dict) else ""

            now = datetime.now(timezone.utc).isoformat()
            conn_doc = {
                "id": f"fb_{page_id}",
                "user_id": None,               # set by finalize
                "platform": "facebook",
                "connected_via": "facebook_direct_oauth",
                "page_id": page_id,
                "page_access_token": page_token,
                "account_name": page_name,
                "profile_picture_url": profile_pic,
                "connection_status": "pending_user_match",
                "connected_at": now,
                "updated_at": now,
            }
            await db["social_connections"].update_one(
                {"id": f"fb_{page_id}"},
                {"$set": conn_doc},
                upsert=True,
            )
            print(f"[FBDirectOAuth] ✅ Stored page '{page_name}' (page_id={page_id}) pending user match")

            params_out = (
                f"connected=facebook_direct"
                f"&fb_page_id={urllib.parse.quote(page_id)}"
                f"&page_name={urllib.parse.quote(page_name)}"
            )
            return RedirectResponse(f"{base_redirect}&{params_out}")

    except Exception as e:
        print(f"[FBDirectOAuth] ❌ Error: {e}")
        return RedirectResponse(
            f"{base_redirect}&connected=false&error={urllib.parse.quote(str(e))}"
        )


@router.post("/connect/facebook-direct/finalize")
async def facebook_direct_finalize(
    fb_page_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Called by the frontend after the Facebook direct OAuth callback to
    associate the pending connection with the authenticated user.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    result = await db["social_connections"].update_one(
        {"id": f"fb_{fb_page_id}"},
        {"$set": {"user_id": user_id, "connection_status": "active", "updated_at": datetime.utcnow().isoformat()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Facebook connection not found — try reconnecting")

    return UriResponse.get_single_data_response("facebook_connected", {"fb_page_id": fb_page_id})


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

    web_app_url = settings.WEB_APP_URL.strip("'\"")
    # Always redirect to the workspace connections tab — that is where
    # the finalizeInstagramDirect handler lives. /settings/social-accounts
    # does not exist as a route so the finalize call would never fire there.
    base_redirect = f"{web_app_url}/workspace?tab=connections"

    if error:
        msg = urllib.parse.quote(error_reason or error)
        return RedirectResponse(f"{base_redirect}&connected=false&error={msg}")

    if not code:
        return RedirectResponse(f"{base_redirect}&connected=false&error=missing_code")

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
        # Use id (the unique indexed field) as the upsert key so reconnecting
        # never hits a duplicate-key error regardless of how the old record was stored.
        now = datetime.now(timezone.utc).isoformat()
        conn_doc = {
            "id": ig_user_id,
            "user_id": None,  # matched when user calls /connections after redirect
            "platform": "instagram",
            "connected_via": "instagram_direct_oauth",
            "ig_user_id": ig_user_id,
            "page_id": pid,  # Facebook Page ID linked to this Instagram account
            "page_access_token": page_token,
            "username": username,
            "account_name": username,
            "profile_picture_url": profile_picture_url,
            "connection_status": "pending_user_match",
            "connected_at": now,
            "updated_at": now,
        }
        await db["social_connections"].update_one(
            {"id": ig_user_id},
            {"$set": conn_doc},
            upsert=True,
        )
        print(f"[IGDirectOAuth] ✅ Stored @{username} (ig_user_id={ig_user_id}) pending user match")

        params_out = (
            f"connected=instagram_direct"
            f"&ig_user_id={urllib.parse.quote(ig_user_id)}"
            f"&username={urllib.parse.quote(username)}"
        )
        # base_redirect already has ?tab=connections so append with &
        return RedirectResponse(f"{base_redirect}&{params_out}")

    except Exception as e:
        print(f"[IGDirectOAuth] ❌ Error: {e}")
        return RedirectResponse(
            f"{base_redirect}&connected=false&error={urllib.parse.quote(str(e))}"
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
        {"ig_user_id": ig_user_id},
        {"$set": {"user_id": user_id, "connection_status": "active", "updated_at": datetime.utcnow().isoformat()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Instagram connection not found — try reconnecting")

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

    web_app_url = settings.WEB_APP_URL.strip("'\"")
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


@router.delete("/connections/instagram-direct/{ig_user_id}")
async def disconnect_instagram_direct(
    ig_user_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Disconnect an Instagram account connected via direct OAuth (not Outstand).
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    result = await db["social_connections"].delete_one({
        "ig_user_id": ig_user_id,
        "user_id": user_id,
    })
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Instagram connection not found")

    return {"status": True, "responseMessage": "Instagram account disconnected"}


@router.delete("/connections/facebook-direct")
async def disconnect_facebook_direct(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Disconnect a Facebook Page connected via direct OAuth (not Outstand).
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    result = await db["social_connections"].delete_one({
        "user_id": user_id,
        "platform": "facebook",
        "connected_via": "facebook_direct_oauth",
    })
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Facebook connection not found")

    return {"status": True, "responseMessage": "Facebook page disconnected"}


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


@router.post("/drafts/{draft_id}/edit-image")
async def edit_draft_image(
    draft_id: str,
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Edit image in-place using user feedback
    PRD: URI-Social-Image-Editing-PRD.docx

    In-place editing preserves everything the user likes about the current image
    and only changes what they request. Unlike regeneration, this keeps the same
    layout, composition, and style.

    Edit categories:
    - text_edit: Change text (price, dates, etc.) - FREE
    - style_edit: Change colors, brightness, spacing - FREE
    - content_edit: Add/remove elements - 1 free, then 1 credit
    - full_redesign: Complete redo - 1 credit
    """
    from app.agents.social_media_manager.services.image_editing_service import ImageEditingService

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    body = await request.json()
    feedback = (body.get("feedback") or "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="feedback is required")

    # Optional: force_category parameter from quick buttons (bypasses classifier)
    force_category = body.get("force_category")

    # Call the editing service
    result = await ImageEditingService.edit_image_for_draft(
        draft_id=draft_id,
        user_id=user_id,
        feedback=feedback,
        db=db,
        force_category=force_category
    )

    # Return the result (could be success, credit warning, or error)
    return JSONResponse(
        status_code=200 if result.get("status") else 400,
        content=result
    )


@router.post("/drafts/{draft_id}/undo-image")
async def undo_draft_image_edit(
    draft_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Undo last image edit and restore previous version
    PRD Section 5.3: Undo Function

    Restores the exact previous version from version history.
    FREE - no API call, just database restore.
    """
    from app.agents.social_media_manager.services.image_editing_service import ImageEditingService

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    # Call undo service
    result = await ImageEditingService.undo_image_edit(
        db=db,
        draft_id=draft_id,
        user_id=user_id
    )

    return JSONResponse(
        status_code=200 if result.get("status") else 400,
        content=result
    )


# ==============================================================================
# CAROUSEL EDITING ENDPOINTS
# ==============================================================================

@router.patch("/drafts/{draft_id}/slides/{slide_index}")
async def update_carousel_slide(
    draft_id: str,
    slide_index: int,
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Update text (headline/body) of a single carousel slide.
    Does NOT regenerate the image - only updates text.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    body = await request.json()
    headline = body.get("headline")
    body_text = body.get("body")

    if not headline and not body_text:
        raise HTTPException(status_code=400, detail="headline or body is required")

    # Verify draft exists and belongs to user
    draft = await db["content_drafts"].find_one(
        {"$or": [{"id": draft_id}, {"draft_id": draft_id}], "user_id": user_id}
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Verify it's a carousel
    if draft.get("post_type") != "carousel":
        raise HTTPException(status_code=400, detail="This endpoint is only for carousel posts")

    # Verify slide exists
    slides = draft.get("slides", [])
    if slide_index < 0 or slide_index >= len(slides):
        raise HTTPException(status_code=400, detail=f"Slide index {slide_index} out of range (0-{len(slides)-1})")

    # Build update fields
    update_fields = {}
    if headline:
        update_fields[f"slides.{slide_index}.headline"] = headline.strip()
    if body_text:
        update_fields[f"slides.{slide_index}.body"] = body_text.strip()
    update_fields["updated_at"] = datetime.utcnow()

    # Update the slide
    result = await db["content_drafts"].update_one(
        {"id": draft_id},
        {"$set": update_fields}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Draft not found")

    return JSONResponse(
        status_code=200,
        content={
            "status": True,
            "responseMessage": f"Slide {slide_index + 1} updated successfully",
            "responseData": {
                "slide_index": slide_index,
                "updated_fields": list(update_fields.keys())
            }
        }
    )


@router.post("/drafts/{draft_id}/slides/{slide_index}/regenerate-image")
async def regenerate_carousel_slide_image(
    draft_id: str,
    slide_index: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Regenerate the image for a single carousel slide.
    Optionally accepts feedback for adjustments.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    body = await request.json()
    feedback = body.get("feedback", "").strip()

    # Verify draft exists and belongs to user
    draft = await db["content_drafts"].find_one(
        {"$or": [{"id": draft_id}, {"draft_id": draft_id}], "user_id": user_id}
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Verify it's a carousel
    if draft.get("post_type") != "carousel":
        raise HTTPException(status_code=400, detail="This endpoint is only for carousel posts")

    # Verify slide exists
    slides = draft.get("slides", [])
    if slide_index < 0 or slide_index >= len(slides):
        raise HTTPException(status_code=400, detail=f"Slide index {slide_index} out of range (0-{len(slides)-1})")

    slide = slides[slide_index]
    retry_count = slide.get("image_retry_count", 0)

    # Check retry limits (first retry free, subsequent cost credits)
    if retry_count >= 1:
        # Check if user has credits
        from app.agents.social_media_manager.services.credit_service import CreditService
        credit_service = CreditService(db)
        has_credits = await credit_service.check_sufficient_credits(user_id, 1)

        if not has_credits:
            return JSONResponse(
                status_code=402,
                content={
                    "status": False,
                    "responseCode": "insufficient_credits",
                    "responseMessage": "You need 1 credit to regenerate this slide again"
                }
            )

        # Deduct credit
        await credit_service.deduct_credit(
            user_id=user_id,
            campaign_id=draft_id,
            reason=f"carousel_slide_{slide_index}_regenerate"
        )

    # Increment retry count
    await db["content_drafts"].update_one(
        {"id": draft_id},
        {"$inc": {f"slides.{slide_index}.image_retry_count": 1}}
    )

    # Get brand context for regeneration
    brand_profile = await db["brand_profiles"].find_one({"user_id": user_id})
    brand_context = BrandProfileService.to_brand_context(brand_profile or {})

    # Build slide content
    slide_content = f"{slide.get('headline', '')} {slide.get('body', '')}".strip()

    # Regenerate image in background
    total_slides = len(slides)
    carousel_id = draft.get("id")

    background_tasks.add_task(
        _generate_image_bg,
        draft_id=draft_id,
        platform=draft.get("platform", "instagram"),
        content=slide_content,
        seed_content=slide_content,
        brand_context=brand_context,
        db=db,
        reference_image=None,
        post_type="carousel",
        slide_index=slide_index,
        image_model=None,
        total_slides=total_slides,
        carousel_id=carousel_id,
    )

    return JSONResponse(
        status_code=200,
        content={
            "status": True,
            "responseMessage": f"Regenerating image for slide {slide_index + 1}...",
            "responseData": {
                "slide_index": slide_index,
                "retry_count": retry_count + 1,
                "credit_charged": retry_count >= 1
            }
        }
    )


@router.delete("/drafts/{draft_id}/slides/{slide_index}")
async def delete_carousel_slide(
    draft_id: str,
    slide_index: int,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Delete a single slide from a carousel.
    Minimum 2 slides required (can't delete if only 2 left).
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    # Verify draft exists and belongs to user
    draft = await db["content_drafts"].find_one(
        {"$or": [{"id": draft_id}, {"draft_id": draft_id}], "user_id": user_id}
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Verify it's a carousel
    if draft.get("post_type") != "carousel":
        raise HTTPException(status_code=400, detail="This endpoint is only for carousel posts")

    # Verify slide exists
    slides = draft.get("slides", [])
    if slide_index < 0 or slide_index >= len(slides):
        raise HTTPException(status_code=400, detail=f"Slide index {slide_index} out of range (0-{len(slides)-1})")

    # Check minimum slides
    if len(slides) <= 2:
        raise HTTPException(status_code=400, detail="Cannot delete slide. Minimum 2 slides required.")

    # Remove the slide and renumber remaining slides
    slides.pop(slide_index)
    for i, slide in enumerate(slides):
        slide["slide_number"] = i + 1

    # Update draft
    await db["content_drafts"].update_one(
        {"id": draft_id},
        {"$set": {"slides": slides, "updated_at": datetime.utcnow()}}
    )

    return JSONResponse(
        status_code=200,
        content={
            "status": True,
            "responseMessage": f"Slide {slide_index + 1} deleted successfully",
            "responseData": {
                "deleted_slide_index": slide_index,
                "remaining_slides": len(slides)
            }
        }
    )


@router.post("/drafts/{draft_id}/slides/reorder")
async def reorder_carousel_slides(
    draft_id: str,
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Reorder carousel slides based on new index array.

    Request body:
    {
        "new_order": [2, 0, 1, 3]  // New positions (0-indexed)
    }
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    body = await request.json()
    new_order = body.get("new_order", [])

    if not new_order or not isinstance(new_order, list):
        raise HTTPException(status_code=400, detail="new_order array is required")

    # Verify draft exists and belongs to user
    draft = await db["content_drafts"].find_one(
        {"$or": [{"id": draft_id}, {"draft_id": draft_id}], "user_id": user_id}
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Verify it's a carousel
    if draft.get("post_type") != "carousel":
        raise HTTPException(status_code=400, detail="This endpoint is only for carousel posts")

    slides = draft.get("slides", [])

    # Validate new_order
    if len(new_order) != len(slides):
        raise HTTPException(
            status_code=400,
            detail=f"new_order length ({len(new_order)}) must match slides count ({len(slides)})"
        )

    if set(new_order) != set(range(len(slides))):
        raise HTTPException(
            status_code=400,
            detail="new_order must contain each index exactly once (e.g., [2, 0, 1])"
        )

    # Reorder slides
    try:
        reordered_slides = [slides[i] for i in new_order]
    except IndexError as e:
        raise HTTPException(status_code=400, detail=f"Invalid index in new_order: {e}")

    # Update slide_number after reorder
    for i, slide in enumerate(reordered_slides):
        slide["slide_number"] = i + 1

    # Update draft
    await db["content_drafts"].update_one(
        {"id": draft_id},
        {"$set": {"slides": reordered_slides, "updated_at": datetime.utcnow()}}
    )

    return JSONResponse(
        status_code=200,
        content={
            "status": True,
            "responseMessage": "Slides reordered successfully",
            "responseData": {
                "old_order": list(range(len(slides))),
                "new_order": new_order,
                "total_slides": len(reordered_slides)
            }
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


class SyncImageRequest(BaseModel):
    source_draft_id: str
    target_draft_ids: List[str]


@router.post("/image-sync")
async def sync_image_across_drafts(
    request: SyncImageRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Copy image_url from a source draft to one or more target drafts (same user only)."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    from app.domain.responses.uri_response import UriResponse

    source = await db["content_drafts"].find_one(
        {"id": request.source_draft_id, "user_id": user_id},
        {"image_url": 1, "has_image": 1},
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source draft not found")

    image_url = source.get("image_url")
    if not image_url:
        raise HTTPException(status_code=422, detail="Source draft has no image yet")

    if not request.target_draft_ids:
        raise HTTPException(status_code=422, detail="No target draft IDs provided")

    result = await db["content_drafts"].update_many(
        {"id": {"$in": request.target_draft_ids}, "user_id": user_id},
        {"$set": {"image_url": image_url, "has_image": True}},
    )

    return UriResponse.get_single_data_response("sync_image", {
        "updated_count": result.modified_count,
        "source_draft_id": request.source_draft_id,
    })


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
        print(f"[Calendar] plan returned plan_id={plan.get('plan_id')} generation_method={plan.get('generation_method')} force={request.force_regenerate}")
        return UriResponse.get_single_data_response("calendar_plan", {
            **plan,
            "regenerated": request.force_regenerate,
        })
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


@router.get("/content-calendar/performance")
async def get_calendar_performance(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Return aggregated post performance data used for content scoring."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService
    data = await PerformanceAnalyticsService.get_user_performance(user_id, db)
    return UriResponse.get_single_data_response("performance", data)


@router.get("/content-calendar/trends")
async def get_calendar_trends(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Return trending keywords for the user's industry."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    from app.services.TrendDataService import TrendDataService
    brand = await BrandProfileService.get(user_id, db)
    if isinstance(brand, dict) and "responseData" in brand:
        brand = brand["responseData"]
    brand = brand or {}

    industry = brand.get("industry", "business")
    region = brand.get("region", "")
    # Build brand-specific seeds from content pillars and key products
    brand_seeds = (brand.get("content_pillars") or [])[:2] + (brand.get("key_products_services") or [])[:2]

    keywords = await TrendDataService.get_trending_keywords(
        industry, region=region, brand_seeds=brand_seeds, db=db
    )
    return UriResponse.get_single_data_response("trends", {
        "industry": industry,
        "region": region,
        "keywords": keywords,
        "count": len(keywords),
    })


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
                        params={"fields": "like_count,comments_count,timestamp,media_type,media_product_type", "access_token": token},
                    )
                    media_data = media_resp.json()
                    if "error" in media_data:
                        print(f"⚠️ IG media fetch error for {media_id}: {media_data['error'].get('message')}")
                        return None

                    media_type = media_data.get("media_type", "IMAGE")
                    media_product_type = media_data.get("media_product_type", "")
                    is_reel = media_product_type == "REELS" or media_type == "REELS"
                    is_video = media_type == "VIDEO" and not is_reel

                    # Choose metrics based on media type
                    if is_reel:
                        metrics = "plays,reach,likes,comments,shares,saved,total_interactions"
                    elif is_video:
                        metrics = "impressions,reach,saved,video_views"
                    else:
                        metrics = "impressions,reach,saved"

                    impressions = reach = views = 0
                    try:
                        ins_resp = await _c.get(
                            f"{_graph_base}/{media_id}/insights",
                            params={"metric": metrics, "period": "lifetime", "access_token": token},
                        )
                        ins_data = ins_resp.json()
                        for item in ins_data.get("data", []):
                            # v21+ uses "total_value", older uses "values[0].value"
                            val = item.get("total_value", {}).get("value") or \
                                  (item.get("values") or [{}])[0].get("value", 0)
                            name = item["name"]
                            if name == "impressions":
                                impressions = val
                            elif name == "reach":
                                reach = val
                            elif name in ("plays", "video_views"):
                                views = val
                    except Exception as ins_err:
                        print(f"⚠️ IG insights fetch failed for {media_id}: {ins_err}")

                likes = media_data.get("like_count", 0)
                comments = media_data.get("comments_count", 0)
                effective_reach = reach or impressions
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
                    "views": views,
                    "impressions": impressions,
                    "reach": reach,
                    "engagement_rate": round(((likes + comments) / max(effective_reach, 1)) * 100, 2) if effective_reach else 0,
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

        # Persist fetched analytics so PerformanceAnalyticsService has real data
        if posts:
            import asyncio as _aio
            async def _persist(post):
                try:
                    await db["content_analytics"].update_one(
                        {"draft_id": post["draft_id"]},
                        {"$set": {
                            "draft_id": post["draft_id"],
                            "likes": post.get("likes", 0),
                            "comments": post.get("comments", 0),
                            "shares": post.get("shares", 0),
                            "views": post.get("views", 0),
                            "impressions": post.get("impressions", 0),
                            "reach": post.get("reach", 0),
                            "engagement_rate": post.get("engagement_rate", 0),
                            "platform": post.get("platform", ""),
                            "updated_at": datetime.utcnow(),
                        }},
                        upsert=True,
                    )
                except Exception:
                    pass
            _aio.ensure_future(_aio.gather(*[_persist(p) for p in posts]))

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

                    # Account-level insights (impressions, reach, profile_views)
                    total_impressions = total_reach = total_profile_views = 0
                    try:
                        insights_resp = await _c.get(
                            f"{_graph_base}/{ig_user_id}/insights",
                            params={
                                "metric": "impressions,reach,profile_views",
                                "period": "day",
                                "since": since_ts,
                                "until": until_ts,
                                "access_token": token,
                            },
                        )
                        for item in insights_resp.json().get("data", []):
                            daily_total = sum(
                                v.get("value", 0) for v in (item.get("values") or [])
                            )
                            name = item.get("name", "")
                            if name == "impressions":
                                total_impressions = daily_total
                            elif name == "reach":
                                total_reach = daily_total
                            elif name == "profile_views":
                                total_profile_views = daily_total
                    except Exception as ig_ins_err:
                        print(f"⚠️ IG account insights failed: {ig_ins_err}")

                    # Aggregate per-post engagement in the period
                    posts_resp = await _c.get(
                        f"{_graph_base}/{ig_user_id}/media",
                        params={"fields": "like_count,comments_count,timestamp,media_type,media_product_type", "limit": 50, "access_token": token},
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
                        "views": total_impressions,
                        "likes": total_likes,
                        "comments": total_comments,
                        "shares": 0,
                        "reposts": 0,
                        "quotes": 0,
                        "reach": total_reach,
                        "profile_views": total_profile_views,
                    },
                    "engagement_note": f"Account impressions + reach for the last {days} days via Instagram Insights API",
                    "platform_specific": {
                        "username": profile.get("username"),
                        "biography": profile.get("biography"),
                        "profile_picture_url": profile.get("profile_picture_url"),
                        "impressions": total_impressions,
                        "reach": total_reach,
                        "profile_views": total_profile_views,
                    },
                    "period": {"since": since_ts, "until": until_ts},
                }
            except Exception as e:
                print(f"⚠️ Instagram direct account metrics failed: {e}")
                return None

        # ── LinkedIn direct connections ───────────────────────────────────────
        linkedin_conns = await db["social_connections"].find(
            {"user_id": user_id, "platform": "linkedin", "connection_status": "active"},
            {"_id": 0, "linkedin_access_token": 1, "person_urn": 1, "active_author_urn": 1,
             "account_name": 1, "username": 1, "pages": 1},
        ).to_list(length=5)

        async def _fetch_linkedin_direct_metrics(conn):
            access_token = conn.get("linkedin_access_token")
            person_urn = conn.get("active_author_urn") or conn.get("person_urn")
            if not access_token or not person_urn:
                return None
            try:
                from app.agents.social_media_manager.services.linkedin_direct_service import LinkedInDirectService
                svc = LinkedInDirectService()

                # Fetch basic profile (name, email)
                profile = await svc.get_profile(access_token)

                # Fetch follower count from LinkedIn Community Management API
                # Works for personal profiles; falls back to 0 if scope not granted
                followers_count = 0
                try:
                    async with _httpx.AsyncClient(timeout=15) as _c:
                        fol_resp = await _c.get(
                            "https://api.linkedin.com/v2/networkSizes/urn:li:person:" + person_urn.split(":")[-1],
                            params={"edgeType": "CompanyFollowedByMember"},
                            headers={"Authorization": f"Bearer {access_token}",
                                     "X-Restli-Protocol-Version": "2.0.0"},
                        )
                        if fol_resp.status_code == 200:
                            followers_count = fol_resp.json().get("firstDegreeSize", 0)
                except Exception:
                    pass  # follower count is best-effort

                # Count published posts from our DB in the period
                from datetime import timezone as _tz
                since_dt = datetime.utcfromtimestamp(since_ts).replace(tzinfo=_tz.utc)
                post_count = await db["content_drafts"].count_documents({
                    "user_id": user_id,
                    "status": "published",
                    "platforms": {"$in": ["linkedin"]},
                })

                # Aggregate likes + comments from content_analytics for LinkedIn posts
                li_draft_ids = await db["content_drafts"].distinct(
                    "id",
                    {"user_id": user_id, "status": "published", "platforms": {"$in": ["linkedin"]}},
                )
                analytics_cursor = db["content_analytics"].find(
                    {"draft_id": {"$in": li_draft_ids}},
                    {"likes": 1, "comments": 1, "shares": 1, "impressions": 1},
                )
                total_likes = total_comments = total_shares = total_impressions = 0
                async for ana in analytics_cursor:
                    total_likes       += int(ana.get("likes", 0) or 0)
                    total_comments    += int(ana.get("comments", 0) or 0)
                    total_shares      += int(ana.get("shares", 0) or 0)
                    total_impressions += int(ana.get("impressions", 0) or 0)

                return {
                    "account_id": person_urn,
                    "network": "linkedin",
                    "page_name": profile.get("name") or conn.get("account_name") or conn.get("username"),
                    "category": None,
                    "followers_count": followers_count,
                    "following_count": None,
                    "posts_count": post_count,
                    "engagement": {
                        "views": total_impressions,
                        "likes": total_likes,
                        "comments": total_comments,
                        "shares": total_shares,
                        "reposts": 0,
                        "quotes": 0,
                    },
                    "engagement_note": f"From {post_count} LinkedIn posts published (all time)",
                    "platform_specific": {
                        "email": profile.get("email"),
                        "person_urn": person_urn,
                        "pages": conn.get("pages", []),
                    },
                    "period": {"since": since_ts, "until": until_ts},
                }
            except Exception as e:
                print(f"⚠️ LinkedIn direct account metrics failed for {person_urn}: {e}")
                return None

        async def _fetch_direct_metrics(conn):
            if conn.get("connected_via") in ("instagram_direct", "instagram_direct_oauth"):
                return await _fetch_instagram_direct_metrics(conn)
            return None  # extend here for other direct platforms

        outstand_results, direct_results, linkedin_results = await _asyncio.gather(
            _asyncio.gather(*[_fetch_outstand_metrics(a) for a in outstand_accounts]),
            _asyncio.gather(*[_fetch_direct_metrics(c) for c in direct_conns]),
            _asyncio.gather(*[_fetch_linkedin_direct_metrics(c) for c in linkedin_conns]),
        )

        account_metrics = [r for r in (*outstand_results, *direct_results, *linkedin_results) if r is not None]

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

@router.get("/debug/connections-raw")
async def debug_connections_raw(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Show the raw social_connections documents for the current user.
    Exposes connected_via, ig_user_id, page_id, and whether page_access_token is present.
    Used to diagnose Instagram/Facebook routing issues.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    conns = await db["social_connections"].find({"user_id": user_id}).to_list(length=50)
    result = []
    for c in conns:
        result.append({
            "id": c.get("id"),
            "platform": c.get("platform"),
            "connected_via": c.get("connected_via"),
            "connection_status": c.get("connection_status"),
            "ig_user_id": c.get("ig_user_id"),
            "page_id": c.get("page_id"),
            "has_page_access_token": bool(c.get("page_access_token")),
            "outstand_account_id": c.get("outstand_account_id"),
            "username": c.get("username"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        })
    return UriResponse.get_single_data_response("connections_raw", result)


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
    Upload a brand logo image. Saves to local static storage and stores the URL
    in the user's brand profile. Accepted formats: PNG, JPG, WEBP, SVG.
    """
    import os, uuid

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    allowed_types = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/svg+xml"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}. Use PNG, JPG, WEBP, or SVG.")

    try:
        contents = await file.read()
        if len(contents) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Logo file must be under 5 MB.")

        from app.utils.cloudinary_upload import upload_bytes
        logo_url = await upload_bytes(contents, folder="uri-social/logos")

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
    Upload a sample design or content template. Saves to local static storage and
    appends the URL to the user's brand profile sample_template_urls list.
    Accepted formats: PNG, JPG, WEBP, PDF. Max 10 MB per file.
    """
    import os, uuid

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
        if len(contents) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File must be under 10 MB.")

        from app.utils.cloudinary_upload import upload_bytes
        resource_type = "raw" if file.content_type == "application/pdf" else "image"
        file_url = await upload_bytes(contents, folder="uri-social/templates", resource_type=resource_type)

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


@router.post("/brand-profile/analyze-voice-samples")
async def analyze_voice_samples(
    request: Dict[str, Any],
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Analyze sample captions to extract voice patterns.

    Caption Voice System (PRD Section 6) - Voice Sample Analysis

    Request body:
    {
        "sample_captions": ["caption 1", "caption 2", "caption 3"],
        "merge_with_profile": true  // optional, default true
    }

    Returns the analysis and optionally updates the user's brand profile
    with merged voice settings.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        sample_captions = request.get("sample_captions", [])
        merge_with_profile = request.get("merge_with_profile", True)

        if not sample_captions or len(sample_captions) == 0:
            raise HTTPException(status_code=400, detail="sample_captions is required and must not be empty")

        if len(sample_captions) > 5:
            raise HTTPException(status_code=400, detail="Maximum 5 sample captions allowed")

        # Analyze the samples
        analysis = await VoiceSampleAnalyzerService.analyze_voice_samples(sample_captions)

        # If merge_with_profile is true, update the brand profile
        if merge_with_profile:
            # Get current profile
            profile_result = await BrandProfileService.get(user_id, db)
            if profile_result.get("status"):
                profile_data = profile_result.get("responseData") or {}
                current_voice_profile = profile_data.get("voice_profile") or {}

                # Merge analysis with current profile
                updated_voice_profile = VoiceSampleAnalyzerService.merge_analysis_with_profile(
                    current_voice_profile,
                    analysis
                )

                # Save updated profile
                await BrandProfileService.save(
                    user_id,
                    {
                        "voice_profile": updated_voice_profile,
                        "voice_sample_analysis": analysis,
                    },
                    db
                )

                return UriResponse.get_single_data_response("voice_analysis", {
                    "analysis": analysis,
                    "updated_voice_profile": updated_voice_profile,
                    "merged": True,
                })

        # Return analysis only
        return UriResponse.get_single_data_response("voice_analysis", {
            "analysis": analysis,
            "merged": False,
        })

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error analyzing voice samples: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to analyze voice samples: {str(e)}")


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
    image_model: Optional[str] = None,
    total_slides: Optional[int] = None,
    carousel_id: Optional[str] = None,
):
    """
    Background task: generate an image for an existing draft and save it to DB.
    Runs after the text-only response has already been returned to the frontend.
    For carousel posts, slide_index indicates which slide this image belongs to,
    and carousel_id ensures all slides share the same visual style.
    For story posts, uses the story (9:16) image spec.
    """
    import re
    import os
    import base64

    try:
        from app.agents.social_media_manager.services.style_library import pick_next_style

        # ── Visual style rotation ─────────────────────────────────────────────
        # For carousel posts: lock style to first slide, reuse for all subsequent slides
        # For regular posts: rotate style normally
        if post_type == "carousel" and carousel_id and slide_index is not None:
            if slide_index == 0:
                # First slide: pick style and cache it for this carousel
                _bp = await db["brand_profiles"].find_one(
                    {"user_id": brand_context.get("user_id", "")},
                    {"style_selections": 1, "style_prompt_fragments": 1, "style_rotation_index": 1, "industry": 1},
                ) or {}
                _style_selections = _bp.get("style_selections") or []
                _style_prompt_fragments = _bp.get("style_prompt_fragments") or []
                _rotation_index = int(_bp.get("style_rotation_index") or 0)
                _industry = _bp.get("industry") or brand_context.get("industry", "")

                _slug, _fragment, _next_index = pick_next_style(
                    _style_selections, _rotation_index, _industry, _style_prompt_fragments
                )

                if _fragment:
                    brand_context = {**brand_context, "style_prompt_fragment": _fragment, "style_slug": _slug}
                    print(f"🎨 Carousel style [{_slug}] applied for all {total_slides} slides (next index: {_next_index})")

                    # Cache style in draft document for subsequent slides
                    await db["content_drafts"].update_one(
                        {"id": carousel_id},
                        {"$set": {
                            "carousel_style_slug": _slug,
                            "carousel_style_fragment": _fragment
                        }}
                    )

                    # Only increment rotation index ONCE per carousel (not per slide)
                    await db["brand_profiles"].update_one(
                        {"user_id": brand_context.get("user_id", "")},
                        {"$set": {"style_rotation_index": _next_index}},
                    )
            else:
                # Subsequent slides: reuse cached style from first slide
                draft = await db["content_drafts"].find_one(
                    {"id": carousel_id},
                    {"carousel_style_slug": 1, "carousel_style_fragment": 1}
                )
                if draft:
                    _slug = draft.get("carousel_style_slug")
                    _fragment = draft.get("carousel_style_fragment")
                    if _fragment:
                        brand_context = {**brand_context, "style_prompt_fragment": _fragment, "style_slug": _slug}
                        print(f"🎨 Reusing carousel style [{_slug}] for slide {slide_index + 1}/{total_slides}")
        else:
            # Regular post: normal style rotation
            _bp = await db["brand_profiles"].find_one(
                {"user_id": brand_context.get("user_id", "")},
                {"style_selections": 1, "style_prompt_fragments": 1, "style_rotation_index": 1, "industry": 1},
            ) or {}
            _style_selections = _bp.get("style_selections") or []
            _style_prompt_fragments = _bp.get("style_prompt_fragments") or []
            _rotation_index = int(_bp.get("style_rotation_index") or 0)
            _industry = _bp.get("industry") or brand_context.get("industry", "")

            _slug, _fragment, _next_index = pick_next_style(
                _style_selections, _rotation_index, _industry, _style_prompt_fragments
            )

            if _fragment:
                brand_context = {**brand_context, "style_prompt_fragment": _fragment, "style_slug": _slug}
                print(f"🎨 Style [{_slug}] applied for this image (next index: {_next_index})")
                # Persist incremented rotation index
                await db["brand_profiles"].update_one(
                    {"user_id": brand_context.get("user_id", "")},
                    {"$set": {"style_rotation_index": _next_index}},
                )

        # For story posts pass image_type="story" so we get 1080x1920 dimensions
        image_type = "story" if post_type == "story" else "post_image"

        image_result = await ImageContentService._generate_platform_image(
            platform=platform,
            content=content,
            seed_content=seed_content,
            brand_context=brand_context,
            reference_image=reference_image,
            image_type=image_type,
            image_model=image_model,
            slide_index=slide_index,
            total_slides=total_slides,
        )

        if not image_result.get("status"):
            print(f"⚠️ BG image gen failed for draft {draft_id}: {image_result.get('responseMessage')}")
            return

        raw_url = image_result["responseData"]["image_url"]
        stored_url = raw_url

        # Upload base64 image to Cloudinary
        if raw_url and raw_url.startswith("data:"):
            print(f"🔄 Uploading image to Cloudinary for draft {draft_id}...")
            try:
                from app.utils.cloudinary_upload import upload_base64
                stored_url = await upload_base64(raw_url, folder="uri-social/content-drafts")
                print(f"☁️  ✅ CLOUDINARY UPLOAD SUCCESS!")
                print(f"   📍 Draft ID: {draft_id}")
                print(f"   🔗 URL: {stored_url}")
            except Exception as upload_err:
                print(f"⚠️  ❌ CLOUDINARY UPLOAD FAILED!")
                print(f"   📍 Draft ID: {draft_id}")
                print(f"   ❌ Error: {upload_err}")

        final_url = stored_url if not stored_url.startswith("data:") else None
        if final_url and db is not None:
            if post_type == "carousel" and slide_index is not None:
                # Update the specific slide's image_url in the slides array
                result = await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {"$set": {
                        f"slides.{slide_index}.image_url": final_url,
                        f"slides.{slide_index}.image_failed": False,
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
            if post_type == "carousel" and slide_index is not None:
                # Mark slide as failed
                await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {"$set": {f"slides.{slide_index}.image_failed": True}},
                )
            print(f"⚠️  BG image not saved for draft {draft_id} (no public URL)")

    except Exception as e:
        # Mark slide as failed on exception
        if db and post_type == "carousel" and slide_index is not None:
            try:
                await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {"$set": {f"slides.{slide_index}.image_failed": True}},
                )
            except:
                pass
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


async def _generate_blog_image_bg(
    draft_id: str,
    title: str,
    topic: str,
    tone: str,
    industry: str,
    brand_colors: List[str],
    db: AsyncIOMotorDatabase
):
    """
    Background task: generate featured image for blog post and update draft in DB
    """
    try:
        print(f"\n{'='*60}")
        print(f"🎨 BACKGROUND: Generating blog featured image")
        print(f"Draft ID: {draft_id}")
        print(f"{'='*60}\n")

        # Generate image using ImageContentService
        image_result = await ImageContentService.generate_blog_featured_image(
            title=title,
            topic=topic,
            tone=tone,
            industry=industry,
            brand_colors=brand_colors
        )

        if not image_result.get("success"):
            print(f"⚠️ BG blog image generation failed for draft {draft_id}")
            return

        raw_url = image_result["url"]
        stored_url = raw_url

        # Upload base64 image to Cloudinary
        if raw_url and raw_url.startswith("data:"):
            print(f"🔄 Uploading blog image to Cloudinary for draft {draft_id}...")
            try:
                from app.utils.cloudinary_upload import upload_base64
                stored_url = await upload_base64(raw_url, folder="uri-social/blog-images")
                print(f"☁️  ✅ CLOUDINARY UPLOAD SUCCESS!")
                print(f"   📍 Draft ID: {draft_id}")
                print(f"   🔗 URL: {stored_url}")
            except Exception as e:
                print(f"❌ Cloudinary upload failed for blog image: {str(e)}")
                print(f"   Storing base64 data URL instead")

        # Update blog draft with featured image
        blog_drafts_collection = db["blog_drafts"]

        await blog_drafts_collection.update_one(
            {"id": draft_id},
            {
                "$set": {
                    "featured_image_url": stored_url,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        print(f"✅ Blog featured image saved to blog_drafts {draft_id}")

    except Exception as e:
        print(f"❌ Background blog image generation error for draft {draft_id}: {str(e)}")
        import traceback
        traceback.print_exc()


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


@router.post("/generate-storyboard")
async def generate_storyboard(
    request: StoryboardRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Generate a GPT-4o Vision video storyboard from brand images.

    Accepts up to 5 base64 image data URLs plus optional creative direction text.
    Returns a structured storyboard JSON with per-scene video prompts, motion
    descriptions, reference image indices, and optional text overlays.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    profile_result = await BrandProfileService.get(user_id, db)
    profile_data = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}

    brand_context = {
        "brand_name": profile_data.get("brand_name", ""),
        "industry": profile_data.get("industry", "general_other"),
        "brand_colors": profile_data.get("brand_colors", []),
        "brand_voice": profile_data.get("derived_voice", ""),
        "region": profile_data.get("region", ""),
    }

    from app.agents.social_media_manager.services.video_storyboard_service import VideoStoryboardService

    result = await VideoStoryboardService.generate_storyboard(
        brand_images=request.brand_images,
        optional_text=request.optional_text,
        brand_context=brand_context,
        target_platform=request.target_platform,
        target_duration_seconds=request.target_duration_seconds,
        video_style=request.video_style,
    )

    if not result.get("status"):
        raise HTTPException(status_code=400, detail=result.get("error", "Storyboard generation failed"))

    return UriResponse.get_single_data_response("storyboard", result["storyboard"])


@router.post("/generate-video-from-storyboard")
async def generate_video_from_storyboard(
    request: VideoFromStoryboardRequest,
    background_tasks: BackgroundTasks,
    token: dict = Depends(JWTBearer()),
):
    """
    Start Veo 3.1 video generation for every scene in a storyboard.
    Returns a job_id immediately. Poll GET /video-job/{job_id} for progress.
    """
    from app.agents.social_media_manager.services.video_generation_service import (
        VideoGenerationService,
    )

    _get_user_id(token)  # auth check

    job_id = await VideoGenerationService.create_job(request.storyboard, request.model)
    background_tasks.add_task(
        VideoGenerationService.run_job,
        job_id,
        request.storyboard,
        request.brand_images,
        request.model,
    )
    return UriResponse.get_single_data_response(
        "video_job",
        {"job_id": job_id, "status": "queued", "total_scenes": len(request.storyboard.get("scenes", []))},
    )


@router.get("/video-job/{job_id}")
async def get_video_job(
    job_id: str,
    token: dict = Depends(JWTBearer()),
):
    """
    Poll for video generation progress.
    status: queued | generating | complete | failed
    current_scene: which scene is being generated right now (1-based)
    clips: completed clips so far (grows as each scene finishes)
    """
    from app.agents.social_media_manager.services.video_generation_service import get_job

    _get_user_id(token)  # auth check

    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return UriResponse.get_single_data_response("video_job", job)


@router.post("/generate-storyboard-frames")
async def generate_storyboard_frames(
    request: StoryboardFramesRequest,
    background_tasks: BackgroundTasks,
    token: dict = Depends(JWTBearer()),
):
    """
    Start background generation of a unique frame image for each storyboard scene.
    Returns a job_id immediately. Poll GET /storyboard-frame-job/{job_id} for progress.
    """
    from app.agents.social_media_manager.services.video_storyboard_service import VideoStoryboardService

    _get_user_id(token)  # auth check

    job_id = await VideoStoryboardService.create_frame_job(request.scenes)
    background_tasks.add_task(VideoStoryboardService.run_frame_job, job_id, request.scenes, request.brand_images)

    return UriResponse.get_single_data_response(
        "frame_job",
        {"job_id": job_id, "status": "generating", "total_scenes": len(request.scenes)},
    )


@router.get("/storyboard-frame-job/{job_id}")
async def get_storyboard_frame_job(
    job_id: str,
    token: dict = Depends(JWTBearer()),
):
    """Poll for storyboard frame image generation progress."""
    from app.agents.social_media_manager.services.video_storyboard_service import VideoStoryboardService

    _get_user_id(token)  # auth check

    job = await VideoStoryboardService.get_frame_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Frame job not found")

    return UriResponse.get_single_data_response("frame_job", job)


@router.post("/merge-video-job/{job_id}")
async def merge_video_job(
    job_id: str,
    token: dict = Depends(JWTBearer()),
):
    """
    Merge all completed clips from a finished video job into a single video.
    Returns the merged Cloudinary video URL.
    """
    from app.agents.social_media_manager.services.video_generation_service import get_job
    from app.agents.social_media_manager.services.video_merge_service import VideoMergeService

    _get_user_id(token)  # auth check

    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "complete":
        raise HTTPException(status_code=400, detail="Job is not complete yet")

    clip_urls = [c["video_url"] for c in job.get("clips", []) if c.get("video_url")]
    if not clip_urls:
        raise HTTPException(status_code=400, detail="No completed clips to merge")

    try:
        merged_url = await VideoMergeService.merge_clips(clip_urls)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"ffmpeg merge failed: {e.stderr.decode()[:300]}")

    return UriResponse.get_single_data_response("merged_video", {"merged_video_url": merged_url})


class SaveVideoDraftRequest(BaseModel):
    merged_video_url: str
    caption: str = ""
    platforms: List[str] = Field(default_factory=list)


@router.post("/video-drafts")
async def save_video_draft(
    request: SaveVideoDraftRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Save a merged video as a draft for later posting."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from datetime import datetime, timezone
    import uuid as _uuid

    draft_id = _uuid.uuid4().hex
    doc = {
        "id": draft_id,
        "request_id": draft_id,      # satisfies unique index on content_drafts
        "platform": "video",          # satisfies compound index (request_id, platform)
        "user_id": user_id,
        "media_type": "video",
        "video_url": request.merged_video_url,
        "content": request.caption,
        "platforms": request.platforms,
        "status": "draft",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db["content_drafts"].insert_one(doc)

    doc.pop("_id", None)
    return UriResponse.get_single_data_response("video_draft", doc)


@router.get("/video-drafts")
async def list_video_drafts(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """List all saved video drafts for the current user."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    drafts = await db["content_drafts"].find(
        {"user_id": user_id, "media_type": "video"},
        {"_id": 0},
    ).sort("created_at", -1).to_list(length=50)

    return UriResponse.get_single_data_response("video_drafts", drafts)


@router.post("/publish-video-draft")
async def publish_video_draft(
    request: PublishVideoDraftRequest,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Start async publishing of a saved video draft to Instagram Reels or Facebook.
    Returns a job_id immediately. Poll GET /video-publish-job/{job_id} for status.
    """
    from app.agents.social_media_manager.services.video_publish_service import VideoPublishService

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Only Instagram and Facebook supported
    SUPPORTED = {"instagram_reels", "facebook_reels"}
    if request.platform not in SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Platform '{request.platform}' is not yet supported. Supported: {', '.join(SUPPORTED)}")

    # Load the draft
    draft = await db["content_drafts"].find_one(
        {"id": request.draft_id, "user_id": user_id, "media_type": "video"},
        {"_id": 0},
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Video draft not found")

    video_url = draft.get("video_url")
    if not video_url:
        raise HTTPException(status_code=400, detail="Draft has no video URL")

    caption = request.caption if request.caption is not None else (draft.get("content") or "")

    # Resolve connection.
    # instagram_reels → look for "instagram" direct connection
    # facebook_reels  → look for "facebook" first; fall back to "instagram" because the
    #                   Instagram OAuth flow stores a Facebook Page access token that can
    #                   also be used to post videos to the Facebook Page.
    if request.platform == "instagram_reels":
        conn = await db["social_connections"].find_one(
            {"user_id": user_id, "platform": "instagram"},
            {"_id": 0, "page_access_token": 1, "ig_user_id": 1},
        )
        if not conn or not conn.get("page_access_token"):
            raise HTTPException(status_code=400, detail="No connected Instagram account found. Please connect your account first.")
        if not conn.get("ig_user_id"):
            raise HTTPException(status_code=400, detail="Instagram account missing ig_user_id. Please reconnect.")
    else:  # facebook_reels
        conn = await db["social_connections"].find_one(
            {"user_id": user_id, "platform": "facebook"},
            {"_id": 0, "page_access_token": 1},
        )
        if not conn or not conn.get("page_access_token"):
            # Fall back to Instagram connection — its page_access_token is a Facebook Page token
            conn = await db["social_connections"].find_one(
                {"user_id": user_id, "platform": "instagram"},
                {"_id": 0, "page_access_token": 1},
            )
        if not conn or not conn.get("page_access_token"):
            raise HTTPException(status_code=400, detail="No connected Facebook or Instagram account found. Connect Instagram to enable Facebook posting.")

    job_id = await VideoPublishService.create_job(request.draft_id, request.platform, user_id)
    background_tasks.add_task(
        VideoPublishService.run_job,
        job_id,
        request.draft_id,
        request.platform,
        video_url,
        caption,
        conn,
        db,
    )

    return UriResponse.get_single_data_response("publish_job", {"job_id": job_id})


@router.get("/video-publish-job/{job_id}")
async def get_video_publish_job(
    job_id: str,
    token: dict = Depends(JWTBearer()),
):
    """Poll the status of a video publish job."""
    from app.agents.social_media_manager.services.video_publish_service import get_publish_job

    _get_user_id(token)  # auth check
    job = await get_publish_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Publish job not found")
    return UriResponse.get_single_data_response("publish_job", job)


@router.post("/extract-image-text")
async def extract_image_text(
    image_url: str = Query(..., description="URL of the image to extract text from"),
    token: dict = Depends(JWTBearer()),
):
    """
    Extract text overlaid on an image using OpenAI Vision API.
    Used when user clicks 'Text' edit button to pre-fill with actual image text.
    """
    try:
        from app.services.AIService import client as ai_client

        prompt = (
            "Extract ALL text that is overlaid or written on this image. "
            "Return ONLY the exact text you see - no descriptions, no analysis. "
            "If there are multiple text elements, list them separated by line breaks. "
            "If there is no text on the image, return 'No text found'."
        )

        response = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}}
                        ]
                    }
                ],
                max_tokens=300
            )
        )

        extracted_text = response.choices[0].message.content.strip()

        return UriResponse.get_single_data_response("image_text", {
            "text": extracted_text,
            "image_url": image_url
        })

    except Exception as e:
        print(f"⚠️ Error extracting text from image: {e}")
        return UriResponse.error_response(f"Failed to extract text: {str(e)}")


@router.post("/upload-custom-font")
async def upload_custom_font(
    file: UploadFile = File(...),
    token: dict = Depends(JWTBearer()),
):
    """
    Upload a custom font file (.ttf or .otf) for the user's brand.
    Typography System PRD - Phase 1: Custom Font Upload
    """
    try:
        user_id = token.get("user_id")

        # Validate file type
        filename = file.filename.lower()
        if not (filename.endswith('.ttf') or filename.endswith('.otf')):
            return UriResponse.error_response("Invalid file type. Please upload .ttf or .otf font files only.")

        # Validate file size (max 5MB)
        file_bytes = await file.read()
        file_size_mb = len(file_bytes) / (1024 * 1024)
        if file_size_mb > 5:
            return UriResponse.error_response(f"File too large ({file_size_mb:.1f}MB). Maximum size is 5MB.")

        print(f"[CUSTOM_FONT] Uploading font: {file.filename} ({file_size_mb:.2f}MB)")

        # Upload to Cloudinary
        from app.utils.cloudinary_upload import upload_bytes

        font_url = await upload_bytes(
            file_bytes,
            folder=f"uri-social/custom-fonts/{user_id}",
            resource_type="raw",  # Non-image file
            public_id=file.filename.rsplit('.', 1)[0]  # Use original filename
        )

        print(f"[CUSTOM_FONT] ✅ Font uploaded: {font_url}")

        return UriResponse.get_single_data_response("font_uploaded", {
            "font_url": font_url,
            "filename": file.filename,
            "size_mb": round(file_size_mb, 2)
        })

    except Exception as e:
        print(f"⚠️ Error uploading custom font: {e}")
        return UriResponse.error_response(f"Failed to upload font: {str(e)}")


@router.post("/analyze-custom-font")
async def analyze_custom_font(
    font_url: str = Query(..., description="Cloudinary URL of the uploaded font file"),
    token: dict = Depends(JWTBearer()),
):
    """
    Analyze a custom font using GPT-4o-mini Vision API.
    Returns font characteristics and a prompt directive for AI image generation.
    Typography System PRD - Phase 1: Custom Font Analysis
    """
    try:
        from app.agents.social_media_manager.services.custom_font_service import CustomFontService

        print(f"[CUSTOM_FONT] Analyzing font: {font_url}")

        # Analyze the font
        analysis_result = await CustomFontService.analyze_font(font_url)

        return UriResponse.get_single_data_response("font_analyzed", {
            "font_url": font_url,
            "analysis": analysis_result["analysis"],
            "prompt_directive": analysis_result["prompt_directive"]
        })

    except Exception as e:
        print(f"⚠️ Error analyzing custom font: {e}")
        return UriResponse.error_response(f"Failed to analyze font: {str(e)}")


# ============================================================================
# BLOG CONTENT GENERATOR
# ============================================================================

class BlogGenerationRequest(BaseModel):
    """Request model for blog content generation"""
    topic: str = Field(..., description="Blog post topic/title", min_length=10, max_length=200)
    keywords: List[str] = Field(..., description="SEO keywords (2-5 keywords)", min_items=1, max_items=10)
    tone: str = Field(..., description="Content tone: professional, inspirational, educational, conversational")
    word_count: int = Field(..., description="Target word count: 1000, 2000, or 3000", ge=500, le=5000)


@router.post("/generate-blog")
async def generate_blog_content(
    request: BlogGenerationRequest,
    background_tasks: BackgroundTasks,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate long-form blog content with AI

    Features:
    - GPT-4 Turbo powered blog writing
    - SEO-optimized title and meta description
    - Structured HTML content with proper headings
    - DALL-E 3 generated featured image
    - Social media promotional snippets
    - Brand voice consistency

    Blog Generator Demo - Phase 1
    """
    try:
        user_id = _get_user_id(token)
        if not user_id:
            return UriResponse.error_response("User ID not found in token")

        print(f"\n{'='*60}")
        print(f"📝 BLOG GENERATION REQUEST")
        print(f"{'='*60}")
        print(f"Topic: {request.topic}")
        print(f"Keywords: {', '.join(request.keywords)}")
        print(f"Tone: {request.tone}")
        print(f"Word Count: {request.word_count}")
        print(f"{'='*60}\n")

        # Validate tone
        valid_tones = ["professional", "inspirational", "educational", "conversational"]
        if request.tone not in valid_tones:
            return UriResponse.error_response(
                f"Invalid tone. Must be one of: {', '.join(valid_tones)}"
            )

        # Validate word count
        valid_word_counts = [1000, 2000, 3000]
        if request.word_count not in valid_word_counts:
            # Allow approximate values (within 500 words)
            closest = min(valid_word_counts, key=lambda x: abs(x - request.word_count))
            if abs(closest - request.word_count) > 500:
                return UriResponse.error_response(
                    f"Word count must be approximately 1000, 2000, or 3000 words"
                )
            request.word_count = closest

        # Get user's brand profile for voice consistency
        brand_profile = await BrandProfileService.get(user_id, db)
        brand_data = None

        if brand_profile:
            brand_data = {
                "brand_name": brand_profile.get("brand_name", "Your Brand"),
                "derived_voice": brand_profile.get("derived_voice", ""),
                "brand_colors": brand_profile.get("brand_colors", []),
                "industry": brand_profile.get("industry", ""),
            }
            print(f"✅ Using brand profile: {brand_data['brand_name']}")
        else:
            print(f"⚠️ No brand profile found, using defaults")

        # Generate blog content
        blog_result = await ImageContentService.generate_long_form_content(
            topic=request.topic,
            keywords=request.keywords,
            tone=request.tone,
            word_count=request.word_count,
            brand_profile=brand_data,
            user_id=user_id
        )

        # Save blog to blog_drafts collection (separate from social posts)
        import uuid
        draft_id = str(uuid.uuid4())

        draft_data = {
            "id": draft_id,
            "user_id": user_id,
            "status": "draft",
            "title": blog_result["title"],
            "meta_description": blog_result["meta_description"],
            "content": blog_result["content"],
            "reading_time": blog_result["reading_time"],
            "word_count": blog_result["word_count"],
            "featured_image_url": None,  # Will be generated in background
            "has_image": True,  # Flag that image generation is in progress
            "social_snippets": blog_result["social_snippets"],
            "keywords": blog_result["keywords"],
            "tone": blog_result["tone"],
            "generated_at": datetime.utcnow(),
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }

        # Insert into blog_drafts collection
        blog_drafts_collection = db["blog_drafts"]
        await blog_drafts_collection.insert_one(draft_data)

        print(f"✅ Blog saved to blog_drafts: {draft_id}")

        # Generate featured image in background
        image_ctx = blog_result.get("image_context", {})
        background_tasks.add_task(
            _generate_blog_image_bg,
            draft_id=draft_id,
            title=image_ctx.get("title", request.topic),
            topic=image_ctx.get("topic", request.topic),
            tone=image_ctx.get("tone", request.tone),
            industry=image_ctx.get("industry", ""),
            brand_colors=image_ctx.get("brand_colors", []),
            db=db
        )
        print(f"🎨 Blog featured image generation queued for background")

        # Return response
        response_data = {
            "draft_id": draft_id,
            "title": blog_result["title"],
            "meta_description": blog_result["meta_description"],
            "content": blog_result["content"],
            "reading_time": blog_result["reading_time"],
            "word_count": blog_result["word_count"],
            "featured_image_url": None,  # Will be generated in background
            "has_image": True,  # Frontend can poll to check when ready
            "social_snippets": blog_result["social_snippets"],
            "keywords": blog_result["keywords"],
            "tone": blog_result["tone"],
            "generated_at": blog_result["generated_at"]
        }

        return UriResponse.get_single_data_response("blog_generated", response_data)

    except Exception as e:
        print(f"❌ Blog generation error: {str(e)}")
        traceback.print_exc()
        return UriResponse.error_response(f"Failed to generate blog content: {str(e)}")


@router.get("/blog-drafts")
async def get_blog_drafts(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """Get all blog drafts for the authenticated user"""
    try:
        user_id = _get_user_id(token)
        if not user_id:
            return UriResponse.error_response("User ID not found in token")

        # Fetch all blog drafts for this user, sorted by created_at descending
        blog_drafts = await db["blog_drafts"].find(
            {"user_id": user_id}
        ).sort("created_at", -1).to_list(length=100)

        # Remove MongoDB _id from results
        for draft in blog_drafts:
            draft.pop("_id", None)

        return UriResponse.get_list_data_response("blog_drafts", blog_drafts)

    except Exception as e:
        print(f"❌ Error fetching blog drafts: {str(e)}")
        traceback.print_exc()
        return UriResponse.error_response(f"Failed to fetch blog drafts: {str(e)}")


@router.get("/blog-drafts/{draft_id}")
async def get_blog_draft(
    draft_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """Get a single blog draft by ID (for polling image status)"""
    try:
        user_id = _get_user_id(token)
        if not user_id:
            return UriResponse.error_response("User ID not found in token")

        draft = await db["blog_drafts"].find_one(
            {"id": draft_id, "user_id": user_id},
            {"_id": 0}
        )

        if not draft:
            return UriResponse.error_response("Blog draft not found", response_code=404)

        return UriResponse.get_single_data_response("blog_draft", draft)

    except Exception as e:
        print(f"❌ Error fetching blog draft {draft_id}: {str(e)}")
        traceback.print_exc()
        return UriResponse.error_response(f"Failed to fetch blog draft: {str(e)}")
