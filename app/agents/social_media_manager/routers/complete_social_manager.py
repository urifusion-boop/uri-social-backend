# app/agents/social_media_manager/routers/complete_social_manager.py

import asyncio
import json
import subprocess
import traceback
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Request, UploadFile, File, Form
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, AsyncGenerator
from datetime import datetime
from bson import ObjectId

from app.dependencies import get_db_dependency, get_active_brand_context, get_flexible_brand_context
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

# Strong references to fire-and-forget background image-generation tasks, so they
# aren't garbage-collected mid-flight (asyncio only weakly tracks pending tasks).
# Entries remove themselves via add_done_callback once finished.
_BG_IMAGE_TASKS: set = set()


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


def _brand_scope(user_id: str, brand_id: Optional[str]) -> Dict[str, Any]:
    """
    Brand-aware Mongo filter for Jane data (drafts, performance, etc.).

    - Agency brand → filter strictly by brand_id, fully isolating that client.
    - Personal/solo brand → filter by user_id BUT exclude docs that belong to a
      different (agency) brand. Without this exclusion, agency brand drafts (which
      store both user_id and brand_id) leak into the personal brand view.
    """
    from app.models.brand_account import BrandAccount
    personal_bid = BrandAccount.personal_brand_id(user_id)
    if brand_id and brand_id != personal_bid:
        return {"brand_id": brand_id}
    # Personal brand: match by user_id, but only docs that are NOT stamped with
    # an agency brand_id (legacy docs have no brand_id; personal brand docs have
    # brand_id == personal_bid or brand_id not present).
    return {
        "user_id": user_id,
        "$or": [
            {"brand_id": {"$exists": False}},
            {"brand_id": None},
            {"brand_id": personal_bid},
        ],
    }


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
    override_cta: Optional[str] = None  # One-time CTA for this generation only (not saved to brand playbook)

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
    logo_size: Optional[str] = None  # small | medium | large
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
    target_audience: Optional[str] = None
    ideal_customer_profile: Optional[str] = None
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
    style_rotation_index: Optional[int] = None
    selected_custom_guides: Optional[List[str]] = None  # Custom visual guide V1 IDs (array)
    selected_custom_guides_v2: Optional[List[str]] = None  # Custom visual guide V2 IDs (array)
    # Typography
    font_style: Optional[str] = None
    font_style_prompt: Optional[str] = None
    primary_font: Optional[str] = None
    primary_font_prompt: Optional[str] = None
    secondary_font: Optional[str] = None
    secondary_font_prompt: Optional[str] = None
    custom_font_enabled: Optional[bool] = None
    custom_font_files: Optional[List[Dict[str, str]]] = None
    custom_font_analysis: Optional[Dict[str, Any]] = None
    custom_font_directive: Optional[str] = None
    # Feature flags
    canvas_editor_enabled: Optional[bool] = None
    use_v3_prompts: Optional[bool] = None

# ==============================================================================
# CONTENT GENERATION ENDPOINTS
# ==============================================================================

@router.post("/generate-content")
async def generate_content(
    request: ContentGenerationRequest,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Generate AI-powered social media content with optional images.

    **Authentication**: Accepts both JWT (Dashboard/Frontend) and API Key (SDK)

    Text content is always returned immediately.
    When include_images=True, images are generated in the background and
    saved to the draft — the frontend can pick them up via GET /content-calendar.

    PRD Credit System:
    - Deducts 1 credit per campaign generation
    - Blocks if credits = 0 (PRD 8: Credit Exhaustion Behavior)
    """
    user_id = ctx["user_id"]
    active_brand_id = ctx["brand_id"]
    auth_type = ctx.get("auth_type", "jwt")

    # Log auth type for debugging
    print(f"\n{'='*80}")
    print(f"📝 CONTENT GENERATION REQUEST")
    print(f"   Auth Type: {auth_type}")
    print(f"   User ID: {user_id[:12]}...")
    print(f"   Brand ID: {active_brand_id[:12] if active_brand_id else 'None'}...")
    print(f"   Platforms: {request.platforms}")
    print(f"   Include Images: {request.include_images}")
    print(f"{'='*80}\n")

    try:
        # ==================== PRD 7.2 & 8: Credit Check ====================
        # Import services (needed later for credit deduction even if check is skipped)
        from app.services.CreditService import credit_service
        from app.services.TrialService import trial_service

        # Skip credit check for API key authentication (SDK Gateway handles request limits)
        # Only check credits for JWT/dashboard users
        is_trial_user = False

        if auth_type != "api_key":
            # Check trial credits first, then paid credits
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
        # Load the ACTIVE BRAND's profile (source of truth) — scoped by brand_id so
        # agency brands use their own voice/colors, not the personal brand's.
        profile_result = await BrandProfileService.get(user_id, db, brand_id=active_brand_id)
        profile_data = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}

        if not profile_data:
            # For API key users, auto-create minimal profile instead of blocking
            if ctx.get("auth_type") == "api_key":
                minimal_profile_data = {
                    "brand_name": f"API User {user_id[:8]}",
                    "industry": "general_other",
                    "brand_colors": ["#C2185B", "#FFFEF2"],
                    "style_selections": ["lifestyle_natural"],
                    "region": "Global",
                    "onboarding_completed": True,
                    "created_via": "api_key_auto",
                }
                await BrandProfileService.save(user_id, minimal_profile_data, db, brand_id=active_brand_id)
                print(f"✅ Auto-created minimal profile for API key user {user_id}")
                # Reload profile data
                profile_result = await BrandProfileService.get(user_id, db, brand_id=active_brand_id)
                profile_data = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
            else:
                # JWT users must complete onboarding
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
            # API key users auto-acknowledged (they use SDK, not frontend modal)
            acknowledged = getattr(request, 'acknowledged_incomplete_profile', False) or ctx.get("auth_type") == "api_key"

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
        brand_context_dict["brand_id"] = active_brand_id  # needed so style lookup uses correct brand profile
        brand_context_dict["using_fallbacks"] = len(missing_fields) > 0
        brand_context_dict["fallback_fields"] = missing_fields
        print(f"🖼️  LOGO DEBUG user={user_id}: logo_url={repr(profile_data.get('logo_url'))}, logo_position={repr(profile_data.get('logo_position'))} → brand_context logo_position={repr(brand_context_dict.get('logo_position'))}")

        # Add one-time CTA override if provided (doesn't save to brand playbook)
        if request.override_cta:
            brand_context_dict["override_cta"] = request.override_cta

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

        # Stamp the ACTIVE BRAND on the request + every draft so content is isolated
        # per brand (agency brands never show another brand's drafts).
        if result.get("status"):
            _rd = result.get("responseData", {})
            _req_id = _rd.get("request_id")
            _d_ids = [d["id"] for d in _rd.get("drafts", []) if d.get("id")]
            if _req_id:
                await db["content_requests"].update_one(
                    {"id": _req_id}, {"$set": {"brand_id": active_brand_id}}
                )
            if _d_ids:
                await db["content_drafts"].update_many(
                    {"id": {"$in": _d_ids}}, {"$set": {"brand_id": active_brand_id}}
                )
            for d in _rd.get("drafts", []):
                d["brand_id"] = active_brand_id

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
        # Deduct 1 credit after successful generation (skip for API key users)
        # PRD 3.1: First campaign generation = 1 credit
        if result.get("status") and ctx.get("auth_type") != "api_key":
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
                    {"$set": {"has_image": True, "image_failed": False}},
                )
                # Mirror the flag in the response so the frontend sees it immediately
                for d in drafts:
                    d["has_image"] = True

            # PRE-ASSIGN visual styles sequentially to avoid race condition when parallel tasks
            # all read the same rotation_index simultaneously. Each draft gets a unique style,
            # then they can generate images concurrently without conflicts.
            #
            # Custom guides (V1 + V2) take priority over library styles — same priority
            # order the per-draft dynamic-assignment fallback below still uses — so this
            # phase must resolve those first, not just always call pick_next_style().
            from app.agents.social_media_manager.services.style_library import pick_next_style
            from app.agents.social_media_manager.services.custom_visual_guide_service import CustomVisualGuideService
            _bc_brand_id = brand_context_dict.get("brand_id", "")
            _bc_user_id = brand_context_dict.get("user_id", "")
            _style_profile_scope = {"brand_id": _bc_brand_id} if _bc_brand_id else {"user_id": _bc_user_id}

            _style_profile = await db["brand_profiles"].find_one(
                _style_profile_scope,
                {"style_selections": 1, "style_prompt_fragments": 1, "style_rotation_index": 1,
                 "industry": 1, "selected_custom_guides": 1, "selected_custom_guides_v2": 1}
            ) or {}

            _current_rotation_index = int(_style_profile.get("style_rotation_index") or 0)
            _style_selections = _style_profile.get("style_selections") or []
            _style_prompt_fragments = _style_profile.get("style_prompt_fragments") or []
            _industry = _style_profile.get("industry") or brand_context_dict.get("industry", "")
            _custom_guide_ids_v1 = _style_profile.get("selected_custom_guides") or []
            _custom_guide_ids_v2 = _style_profile.get("selected_custom_guides_v2") or []
            _all_custom_guide_ids = _custom_guide_ids_v1 + _custom_guide_ids_v2

            # Pre-assign styles to each non-carousel draft. Each assignment is a dict:
            # {"type": "library"|"custom_v1"|"custom_v2", "slug", "fragment"?,
            #  "custom_guide_id"?, "reference_image"?}
            _assigned_styles: Dict[str, Dict[str, Any]] = {}
            _next_rotation_index = _current_rotation_index

            for draft in drafts:
                if post_type == "carousel":
                    continue  # Carousels handle their own style assignment

                if _all_custom_guide_ids:
                    # Rotate through custom guides (V1 + V2) like library styles
                    _custom_guide_id = _all_custom_guide_ids[_next_rotation_index % len(_all_custom_guide_ids)]
                    _is_v2 = _custom_guide_id in _custom_guide_ids_v2
                    _next_rotation_index = (_next_rotation_index + 1) % (len(_all_custom_guide_ids) + len(_style_selections))

                    if _is_v2:
                        _custom_guide = await db["custom_visual_guides"].find_one(
                            {"_id": ObjectId(_custom_guide_id), "version": "v2"}
                        )
                        if _custom_guide:
                            _assigned_styles[draft["id"]] = {
                                "type": "custom_v2",
                                "slug": f"custom_v2_{_custom_guide_id[:8]}",
                                "custom_guide_id": _custom_guide_id,
                                "reference_image": _custom_guide.get("original_image_url"),
                            }
                            print(f"🎨 Pre-assigned Custom V2 guide [{_custom_guide.get('name')}] to draft {draft['id'][:12]}...")
                    else:
                        _custom_guide = await CustomVisualGuideService.get_guide_detail(_custom_guide_id, db)
                        if _custom_guide:
                            _assigned_styles[draft["id"]] = {
                                "type": "custom_v1",
                                "slug": f"custom_{_custom_guide_id[:8]}",
                                "fragment": _custom_guide.get("prompt_fragment", ""),
                                "custom_guide_id": _custom_guide_id,
                            }
                            print(f"🎨 Pre-assigned Custom V1 guide [{_custom_guide.get('name')}] to draft {draft['id'][:12]}...")
                            await CustomVisualGuideService.track_guide_usage(_custom_guide_id, False, db)
                else:
                    _slug, _fragment, _next_rotation_index = pick_next_style(
                        _style_selections, _next_rotation_index, _industry, _style_prompt_fragments
                    )
                    _assigned_styles[draft["id"]] = {"type": "library", "slug": _slug, "fragment": _fragment}

            # Save the final rotation index after all assignments
            if _assigned_styles:
                await db["brand_profiles"].update_one(
                    _style_profile_scope,
                    {"$set": {"style_rotation_index": _next_rotation_index}}
                )
                print(f"🎨 Pre-assigned {len(_assigned_styles)} styles, rotation index: {_current_rotation_index} → {_next_rotation_index}")

            # FastAPI's BackgroundTasks runs queued tasks one at a time, in order —
            # with several drafts that serializes their image generation instead of
            # overlapping it. Fire each draft's (or slide's) generation as its own
            # asyncio task so they actually run concurrently. Keep references so
            # nothing gets garbage-collected mid-flight.
            _bg_image_tasks: List[asyncio.Task] = []
            for draft in drafts:
                if post_type == "carousel":
                    slides = draft.get("slides") or []
                    # Pass total_slides and carousel_id for visual continuity
                    total_slides = len(slides)
                    carousel_id = draft["id"]  # All slides share same carousel_id
                    for slide_index, slide in enumerate(slides):
                        # Combine slide-specific content with original seed for richer context
                        slide_content = f"{slide.get('headline', '')} {slide.get('body', '')}".strip()
                        # Preserve original seed content for elegant, contextual image generation
                        # Format: "Original context: {seed}. This slide focuses on: {slide headline and body}"
                        enriched_seed = f"{request.seed_content}. This slide: {slide_content}"
                        _bg_image_tasks.append(asyncio.create_task(_generate_image_bg(
                            draft_id=draft["id"],
                            platform=draft["platform"],
                            content=slide_content or draft["content"],
                            seed_content=enriched_seed,  # Use enriched seed with both original context and slide-specific focus
                            brand_context=brand_context_dict,
                            db=db,
                            reference_image=request.reference_image,
                            post_type=post_type,
                            slide_index=slide_index,
                            image_model=request.image_model,
                            total_slides=total_slides,
                            carousel_id=carousel_id,
                        )))
                else:
                    # Get pre-assigned style for this draft
                    _preassigned_style = _assigned_styles.get(draft["id"])
                    print(f"🎨 Creating background image task for draft {draft['id'][:12]}... (auth_type={auth_type})")
                    _bg_image_tasks.append(asyncio.create_task(_generate_image_bg(
                        draft_id=draft["id"],
                        platform=draft["platform"],
                        content=draft["content"],
                        seed_content=request.seed_content,
                        brand_context=brand_context_dict,
                        db=db,
                        reference_image=request.reference_image,
                        post_type=post_type,
                        image_model=request.image_model,
                        preassigned_style=_preassigned_style,
                    )))
            _BG_IMAGE_TASKS.update(_bg_image_tasks)
            for t in _bg_image_tasks:
                t.add_done_callback(_BG_IMAGE_TASKS.discard)

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


@router.post("/upload-user-content")
async def upload_user_content(
    request: Dict[str, Any],
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_active_brand_context),
):
    """
    Upload user-provided media (images/videos) and generate captions using brand playbook.

    User uploads their own photos/videos, AI analyzes them and writes captions.
    No AI image generation - uses uploaded media as-is.

    PRD: User Uploaded Content Workflow
    - Step 1: User uploads images/videos
    - Step 2: AI analyzes uploaded media (vision)
    - Step 3: AI generates captions using brand playbook
    - Step 4: Creates drafts with uploaded media URLs
    - Step 5: User can schedule/publish (existing workflow)
    """
    user_id = ctx["user_id"]
    active_brand_id = ctx["brand_id"]

    try:
        # Parse request
        uploaded_media = request.get("uploaded_media", [])
        context_text = request.get("context_text", "")
        platforms = request.get("platforms", [])
        post_type = request.get("post_type", "feed")

        # New options: logo and CTA overlays
        add_logo = request.get("add_logo", False)
        add_cta = request.get("add_cta", False)
        custom_cta = request.get("custom_cta", "")  # If provided, use this instead of default
        logo_position_override = request.get("logo_position_override", "")  # User can override logo position for this upload

        print(f"📥 Upload content request:")
        print(f"   add_logo: {add_logo}")
        print(f"   add_cta: {add_cta}")
        print(f"   custom_cta: {custom_cta}")
        print(f"   logo_position_override: {logo_position_override}")
        print(f"   num_media: {len(uploaded_media)}")

        if not uploaded_media:
            raise HTTPException(status_code=400, detail="No media uploaded")
        if not platforms:
            raise HTTPException(status_code=400, detail="No platforms selected")

        # Credit check (same as generate-content)
        from app.services.CreditService import credit_service
        from app.services.TrialService import trial_service

        is_trial_user = await trial_service.has_active_trial(user_id)

        if not is_trial_user:
            has_credits = await credit_service.check_sufficient_credits(user_id)
            if not has_credits:
                return JSONResponse(
                    status_code=402,
                    content={
                        "status": False,
                        "responseCode": 402,
                        "responseMessage": "You've run out of credits. Upgrade to continue.",
                        "responseData": {"credits_remaining": 0, "upgrade_url": "/pricing"}
                    }
                )

        # Upload media to Cloudinary
        from ..services.user_media_storage_service import UserMediaStorageService

        print(f"📤 Uploading {len(uploaded_media)} user media files...")
        media_urls = await UserMediaStorageService.upload_user_media(uploaded_media, user_id)
        print(f"✅ All media uploaded successfully: {len(media_urls)} URLs")

        # Load brand profile (needed for logo/CTA overlays)
        profile_result = await BrandProfileService.get(user_id, db, brand_id=active_brand_id)
        profile_data = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        brand_context_dict = BrandProfileService.to_brand_context(profile_data)

        # Apply logo and/or CTA overlays if requested
        if add_logo or add_cta:
            from ..services.image_content_service import ImageContentService
            import base64
            import requests
            import io
            from PIL import Image

            processed_media_urls = []
            for media_url in media_urls:
                try:
                    # Download the uploaded image
                    resp = requests.get(media_url, timeout=10)
                    resp.raise_for_status()
                    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")

                    # Apply logo overlay if requested
                    if add_logo and brand_context_dict.get('logo_url'):
                        logo_size = brand_context_dict.get('logo_size', 'small')

                        # Determine logo position: user override > AI analysis > brand profile default
                        if logo_position_override:
                            # User manually selected position for this upload
                            logo_position = logo_position_override
                            print(f"🎨 Using user-selected logo position: {logo_position}")
                        else:
                            # Use AI to find best logo position (avoids important content)
                            print(f"🤖 Analyzing image to find best logo position...")
                            try:
                                from app.services.AIService import AIService

                                vision_prompt = """Analyze this image and determine the best corner to place a brand logo overlay.

Consider:
1. Which corners have the LEAST important content (text, faces, key visual elements)?
2. Which corners have the most empty/background space?
3. Avoid corners with text, faces, or focal points

Respond with ONLY ONE of these positions:
- top_left
- top_right
- top_center
- bottom_left
- bottom_right
- bottom_center

Choose the position that will cause the LEAST visual disruption."""

                                vision_request = AIService.build_ai_model(
                                    messages=[{
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": vision_prompt},
                                            {"type": "image_url", "image_url": {"url": media_url}}
                                        ]
                                    }],
                                    temperature=0.3,
                                )
                                vision_response = await AIService.chat_completion(vision_request)
                                ai_position = vision_response.choices[0].message.content.strip().lower()

                                # Validate AI response
                                valid_positions = ["top_left", "top_right", "top_center", "bottom_left", "bottom_right", "bottom_center"]
                                if ai_position in valid_positions:
                                    logo_position = ai_position
                                    print(f"✅ AI selected best logo position: {logo_position}")
                                else:
                                    # Fallback to brand profile default
                                    logo_position = brand_context_dict.get('logo_position', 'bottom_right')
                                    print(f"⚠️ AI gave invalid position '{ai_position}', using brand default: {logo_position}")

                            except Exception as vision_err:
                                # If AI analysis fails, use brand profile default
                                logo_position = brand_context_dict.get('logo_position', 'bottom_right')
                                print(f"⚠️ AI position analysis failed: {vision_err}, using brand default: {logo_position}")

                        print(f"🎨 Applying logo overlay: position={logo_position}, size={logo_size}")

                        # Convert image to base64, apply logo, convert back
                        buf = io.BytesIO()
                        img.convert("RGB").save(buf, format="JPEG", quality=95)
                        img_b64 = base64.b64encode(buf.getvalue()).decode()

                        # Use existing logo overlay method
                        img_b64_with_logo = ImageContentService._overlay_logo(
                            img_b64,
                            brand_context_dict['logo_url'],
                            logo_position,
                            logo_size
                        )

                        # Convert back to PIL for CTA overlay
                        img = Image.open(io.BytesIO(base64.b64decode(img_b64_with_logo))).convert("RGBA")
                        print(f"✅ Logo overlay applied successfully")
                    elif add_logo and not brand_context_dict.get('logo_url'):
                        print(f"⚠️ Logo overlay requested but no logo_url in brand profile")

                    # Apply CTA overlay if requested
                    if add_cta:
                        # Determine CTA text (same logic as normal image generation)
                        cta_text = None
                        if custom_cta:
                            # Use custom CTA if provided
                            cta_text = custom_cta
                        else:
                            # Use brand playbook CTA logic (same as normal generation)
                            cta_styles_list = brand_context_dict.get("cta_styles", [])
                            if isinstance(cta_styles_list, list) and cta_styles_list:
                                import random
                                cta_text = random.choice(cta_styles_list)
                            else:
                                # Fallback to default_link if cta_styles is empty
                                default_link = brand_context_dict.get("default_link", "")
                                if default_link:
                                    cta_text = default_link
                                    # Remove https:// prefix for cleaner display
                                    if isinstance(cta_text, str):
                                        cta_text = cta_text.replace('https://', '').replace('http://', '')
                                else:
                                    # Final fallback
                                    cta_text = "Link in bio"

                        print(f"🎯 CTA text determined: '{cta_text}'")

                        if cta_text:
                            # Use AI vision to find best position, then PIL to draw text
                            print(f"🔄 Using AI vision to find best CTA position: '{cta_text}'")
                            try:
                                from app.services.AIService import AIService
                                from PIL import ImageDraw, ImageFont

                                # Use AI vision to determine best position for CTA
                                vision_prompt = """Analyze this image to find the best location to add a small call-to-action text.

Consider:
1. Where is there the MOST empty/background space?
2. Avoid areas with text, faces, or important visual elements
3. Look for solid color areas or minimal content zones

Respond with ONLY ONE of these positions:
- top_center
- bottom_center
- top_left
- top_right
- bottom_left
- bottom_right

Choose the position that will cause the LEAST visual disruption."""

                                vision_request = AIService.build_ai_model(
                                    messages=[{
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": vision_prompt},
                                            {"type": "image_url", "image_url": {"url": media_url}}
                                        ]
                                    }],
                                    temperature=0.3,
                                )
                                vision_response = await AIService.chat_completion(vision_request)
                                cta_position = vision_response.choices[0].message.content.strip().lower()

                                # Validate position
                                valid_positions = ["top_center", "bottom_center", "top_left", "top_right", "bottom_left", "bottom_right"]
                                if cta_position not in valid_positions:
                                    cta_position = "bottom_center"  # Fallback

                                print(f"✅ AI selected CTA position: {cta_position}")

                                # Draw CTA text using PIL
                                width, height = img.size
                                draw = ImageDraw.Draw(img)

                                # Calculate font size (4-5% of image width for better visibility)
                                font_size = int(width * 0.045)
                                try:
                                    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
                                except:
                                    try:
                                        # Fallback to Arial
                                        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", font_size)
                                    except:
                                        font = ImageFont.load_default()

                                # Get text bounding box
                                bbox = draw.textbbox((0, 0), cta_text, font=font)
                                text_width = bbox[2] - bbox[0]
                                text_height = bbox[3] - bbox[1]

                                # Calculate position based on AI selection
                                margin = int(width * 0.05)  # 5% margin
                                if "top" in cta_position:
                                    y = margin
                                else:  # bottom
                                    y = height - text_height - margin

                                if "center" in cta_position:
                                    x = (width - text_width) // 2
                                elif "left" in cta_position:
                                    x = margin
                                else:  # right
                                    x = width - text_width - margin

                                # Draw text with subtle drop shadow for depth without looking squeezed
                                # First draw shadow (offset by a few pixels)
                                shadow_offset = 3
                                shadow_color = (0, 0, 0, 120)  # Semi-transparent black shadow

                                # Draw shadow
                                draw.text(
                                    (x + shadow_offset, y + shadow_offset),
                                    cta_text,
                                    font=font,
                                    fill=shadow_color
                                )

                                # Draw main text in white - clean and crisp
                                draw.text(
                                    (x, y),
                                    cta_text,
                                    font=font,
                                    fill="white"
                                )

                                print(f"✅ CTA text drawn at {cta_position}: '{cta_text}'")

                            except Exception as cta_err:
                                print(f"⚠️ CTA overlay failed: {cta_err}, skipping CTA")

                    # Upload processed image back to Cloudinary
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="JPEG", quality=95)
                    buf.seek(0)

                    # Re-upload to Cloudinary
                    from app.utils.cloudinary_upload import upload_bytes
                    folder = f"uri-social/user-uploads/{user_id}/processed"
                    processed_url = await upload_bytes(buf.getvalue(), folder=folder, resource_type="image")
                    processed_media_urls.append(processed_url)
                    print(f"✅ Applied overlays to image {len(processed_media_urls)}/{len(media_urls)}")

                except Exception as e:
                    print(f"⚠️ Overlay processing failed for image, using original: {e}")
                    processed_media_urls.append(media_url)

            # Use processed URLs
            media_urls = processed_media_urls

        # Analyze uploaded media with vision
        # Build comprehensive analysis of all uploaded media
        vision_analyses = []
        for idx, media_url in enumerate(media_urls):
            print(f"🔍 Analyzing media {idx + 1}/{len(media_urls)}...")
            try:
                vision_prompt = f"""Analyze this uploaded image/video for social media content generation.

USER'S CONTEXT: {context_text if context_text else 'No additional context provided'}

Provide a detailed description including:
1. Main subject/content (what's shown?)
2. Visual style (colors, mood, setting, composition)
3. Key features or details that stand out
4. Target audience (who would this appeal to?)
5. Emotional tone (what feeling does it evoke?)
6. Any text/branding visible in the media
7. Suggested content angle (how should we talk about this?)

Be specific and descriptive to help write engaging captions."""

                from app.services.AIService import AIService
                vision_request = AIService.build_ai_model(
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": vision_prompt},
                            {"type": "image_url", "image_url": {"url": media_url}}
                        ]
                    }],
                    temperature=0.5,
                )
                vision_response = await AIService.chat_completion(vision_request)

                if isinstance(vision_response, dict) and "error" in vision_response:
                    print(f"⚠️ Vision analysis failed for media {idx + 1}: {vision_response['error']}")
                else:
                    analysis = vision_response.choices[0].message.content.strip()
                    vision_analyses.append(f"[Media {idx + 1}]: {analysis}")
                    print(f"✅ Analyzed media {idx + 1}")

            except Exception as e:
                print(f"⚠️ Vision analysis error for media {idx + 1}: {e}")

        # Build enriched seed content
        enriched_seed_content = f"""USER'S UPLOADED CONTENT:

{chr(10).join(vision_analyses)}

USER PROVIDED CONTEXT: {context_text if context_text else 'None provided'}

Create engaging social media captions for THIS UPLOADED CONTENT. Base your writing on what's actually shown in the images/videos above."""

        # Generate captions for each platform (reuse existing content generation)
        result = await ContentGenerationService.generate_multi_platform_content(
            user_id=user_id,
            seed_content=enriched_seed_content,
            platforms=platforms,
            seed_type="uploaded_media",
            brand_context=brand_context_dict,
            db=db,
        )

        # Tag drafts with uploaded media and brand
        if result.get("status"):
            _rd = result.get("responseData", {})
            _req_id = _rd.get("request_id")
            _d_ids = [d["id"] for d in _rd.get("drafts", []) if d.get("id")]

            if _req_id:
                await db["content_requests"].update_one(
                    {"id": _req_id},
                    {"$set": {"brand_id": active_brand_id, "content_source": "user_uploaded"}}
                )

            if _d_ids:
                # Attach uploaded images to drafts so they can be published
                if post_type == "carousel":
                    # For carousel: create slides from uploaded images
                    carousel_images = [{"image_url": url} for url in media_urls]
                    update_fields = {
                        "brand_id": active_brand_id,
                        "content_source": "user_uploaded",
                        "uploaded_media_urls": media_urls,
                        "post_type": post_type,
                        "carousel_images": carousel_images,
                    }
                else:
                    # For single post: use first uploaded image as main image
                    update_fields = {
                        "brand_id": active_brand_id,
                        "content_source": "user_uploaded",
                        "uploaded_media_urls": media_urls,
                        "post_type": post_type,
                        "image_url": media_urls[0] if media_urls else None,
                    }

                # Mark drafts as user-uploaded and attach media URLs
                await db["content_drafts"].update_many(
                    {"id": {"$in": _d_ids}},
                    {"$set": update_fields}
                )

                # Update in-memory draft objects
                for d in _rd.get("drafts", []):
                    d.update(update_fields)

        # Deduct credits (cheaper than full generation since no image gen)
        if result.get("status"):
            request_id = result.get("responseData", {}).get("request_id")
            credits_to_deduct = 1  # 1 credit - no AI image generation cost (cheaper than full generation)

            if request_id:
                if is_trial_user:
                    await trial_service.deduct_trial_credit(
                        user_id=user_id,
                        campaign_id=request_id,
                        reason="upload_user_content",
                    )
                    print(f"✅ Deducted 1 trial credit from user {user_id}")
                else:
                    await credit_service.deduct_credit(
                        user_id=user_id,
                        campaign_id=request_id,
                        reason="upload_user_content",
                        retry_count=0,
                    )
                    print(f"✅ Deducted 1 credit from user {user_id}")

            # Notification
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

        return result

    except HTTPException:
        raise
    except Exception as e:
        error_detail = str(e) or repr(e)
        print(f"❌ upload_user_content error for user={user_id}: {error_detail}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=error_detail)

# ==============================================================================
# SOCIAL ACCOUNT CONNECTION ENDPOINTS
# ==============================================================================

@router.post("/connect/initiate")
async def initiate_social_connections(
    request: SocialConnectionRequest,
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Step 1 of the social connection flow (onboarding step 2).

    Returns Outstand OAuth URLs for each requested platform.
    The frontend opens each auth_url so the user can authorise.
    After authorisation, Outstand redirects back to /connect/callback/outstand.

    Supported platforms: facebook, instagram, linkedin, x/twitter,
    tiktok, youtube, pinterest, threads, bluesky, google_business
    """
    return await SocialAccountService.initiate_connection_flow(
        user_id=ctx["user_id"],
        platforms=request.platforms,
        source=request.source,
        brand_id=ctx["brand_id"],
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
        "read_insights",
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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Called by the frontend after the Facebook direct OAuth callback to
    associate the pending connection with the authenticated user and active brand.
    """
    from app.models.brand_account import BrandAccount
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)

    update_fields: dict = {"user_id": user_id, "connection_status": "active", "updated_at": datetime.utcnow().isoformat()}
    if not is_personal:
        update_fields["brand_id"] = brand_id

    result = await db["social_connections"].update_one(
        {"id": f"fb_{fb_page_id}"},
        {"$set": update_fields},
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
                    # Store the user's long-lived token instead of page token
                    # The long_token has instagram_content_publish permission
                    page_token = long_token
                    # Step 5: fetch Instagram profile
                    profile_resp = await client.get(
                        f"https://graph.facebook.com/v20.0/{ig_user_id}",
                        params={"fields": "id,username,name,profile_picture_url", "access_token": long_token},
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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Called by the frontend after the Instagram direct OAuth callback to
    associate the pending connection with the authenticated user and active brand.
    """
    from app.models.brand_account import BrandAccount
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)

    update_fields: dict = {"user_id": user_id, "connection_status": "active", "updated_at": datetime.utcnow().isoformat()}
    if not is_personal:
        update_fields["brand_id"] = brand_id

    result = await db["social_connections"].update_one(
        {"ig_user_id": ig_user_id},
        {"$set": update_fields},
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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Step 3 of the social connection flow (completes onboarding step 2).

    Finalises the OAuth connection for the selected pages/accounts.
    Stores the connected account IDs locally for publishing.
    Call GET /connections after this to see all connected accounts.
    """
    return await SocialAccountService.finalize_connection(
        db=db,
        user_id=ctx["user_id"],
        session_token=request.session_token,
        selected_page_ids=request.selected_page_ids,
        brand_id=ctx["brand_id"],
    )


@router.get("/connections")
async def get_user_connections(
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Get social accounts connected for the active brand (isolated per brand)."""
    return await SocialAccountService.get_user_connections(
        db=db, user_id=ctx["user_id"], brand_id=ctx["brand_id"]
    )


@router.delete("/connections/account/{outstand_account_id}")
async def disconnect_social_account(
    outstand_account_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Permanently disconnect a social account for the active brand.
    Use the outstand_account_id returned by GET /connections.
    This revokes OAuth tokens — the user must reconnect via /connect/initiate.
    """
    return await SocialAccountService.disconnect_account(
        db=db,
        user_id=ctx["user_id"],
        outstand_account_id=outstand_account_id,
        brand_id=ctx["brand_id"],
    )


@router.delete("/connections/instagram-direct/{ig_user_id}")
async def disconnect_instagram_direct(
    ig_user_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Disconnect an Instagram account connected via direct OAuth for the active brand.
    """
    from app.models.brand_account import BrandAccount
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)
    personal_bid = BrandAccount.personal_brand_id(user_id)

    if is_personal:
        delete_filter = {
            "ig_user_id": ig_user_id,
            "user_id": user_id,
            "$or": [
                {"brand_id": {"$exists": False}},
                {"brand_id": None},
                {"brand_id": personal_bid},
            ],
        }
    else:
        delete_filter = {"ig_user_id": ig_user_id, "brand_id": brand_id}

    result = await db["social_connections"].delete_one(delete_filter)
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Instagram connection not found")

    return {"status": True, "responseMessage": "Instagram account disconnected"}


@router.delete("/connections/facebook-direct")
async def disconnect_facebook_direct(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Disconnect a Facebook Page connected via direct OAuth for the active brand.
    """
    from app.models.brand_account import BrandAccount
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)
    personal_bid = BrandAccount.personal_brand_id(user_id)

    if is_personal:
        delete_filter = {
            "user_id": user_id,
            "platform": "facebook",
            "connected_via": "facebook_direct_oauth",
            "$or": [
                {"brand_id": {"$exists": False}},
                {"brand_id": None},
                {"brand_id": personal_bid},
            ],
        }
    else:
        delete_filter = {"brand_id": brand_id, "platform": "facebook", "connected_via": "facebook_direct_oauth"}

    result = await db["social_connections"].delete_one(delete_filter)
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
                "image_failed": False,
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
    image_url = body.get("image_url")

    if not headline and not body_text and image_url is None:
        raise HTTPException(status_code=400, detail="headline, body, or image_url is required")

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
    if image_url is not None:
        update_fields[f"slides.{slide_index}.image_url"] = image_url
        update_fields[f"slides.{slide_index}.image_failed"] = False
        update_fields["has_image"] = True
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
    """
    Copy images from a source draft to one or more target drafts (same user only).
    For carousel drafts: syncs all slide image_urls (targets must have the same slide count).
    For feed/story drafts: syncs the single image_url.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    from app.domain.responses.uri_response import UriResponse

    source = await db["content_drafts"].find_one(
        {"id": request.source_draft_id, "user_id": user_id},
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source draft not found")

    if not request.target_draft_ids:
        raise HTTPException(status_code=422, detail="No target draft IDs provided")

    is_carousel = source.get("post_type") == "carousel"
    updated_count = 0
    skipped = []

    if is_carousel:
        source_slides = source.get("slides") or []
        if not source_slides or not any(s.get("image_url") for s in source_slides):
            raise HTTPException(status_code=422, detail="Source carousel has no slide images yet")

        source_image_urls = [s.get("image_url") for s in source_slides]

        targets = await db["content_drafts"].find(
            {"id": {"$in": request.target_draft_ids}, "user_id": user_id}
        ).to_list(length=50)

        for target in targets:
            target_slides = target.get("slides") or []
            if len(target_slides) != len(source_slides):
                skipped.append({"id": target.get("id"), "reason": f"slide count mismatch ({len(target_slides)} vs {len(source_slides)})"})
                continue

            updated_slides = []
            for i, slide in enumerate(target_slides):
                updated_slides.append({**slide, "image_url": source_image_urls[i]})

            await db["content_drafts"].update_one(
                {"id": target.get("id"), "user_id": user_id},
                {"$set": {"slides": updated_slides, "has_image": True, "updated_at": datetime.utcnow()}},
            )
            updated_count += 1
    else:
        image_url = source.get("image_url")
        if not image_url:
            raise HTTPException(status_code=422, detail="Source draft has no image yet")

        result = await db["content_drafts"].update_many(
            {"id": {"$in": request.target_draft_ids}, "user_id": user_id},
            {"$set": {"image_url": image_url, "has_image": True, "updated_at": datetime.utcnow()}},
        )
        updated_count = result.modified_count

    return UriResponse.get_single_data_response("sync_image", {
        "updated_count": updated_count,
        "source_draft_id": request.source_draft_id,
        "is_carousel": is_carousel,
        "skipped": skipped,
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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """Move a scheduled draft back to draft status."""
    user_id = ctx["user_id"]
    scope = _brand_scope(user_id, ctx["brand_id"])

    draft = await db["content_drafts"].find_one({**scope, "id": draft_id})
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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """Get all scheduled content for the active brand"""
    user_id = ctx["user_id"]
    scope = _brand_scope(user_id, ctx["brand_id"])

    try:
        # Scoped to the active brand (agency brands isolated; personal brand by user_id).
        requests = await db["content_requests"].find(scope, {"id": 1}).to_list(length=200)
        request_ids = [req["id"] for req in requests if req.get("id")]

        import os as _os
        _scheduled_statuses = ["scheduled", "staging_scheduled", "publish_failed"]
        scheduled_drafts = await db["content_drafts"].find({
            "$or": [
                {**scope, "status": {"$in": _scheduled_statuses}},
                {"request_id": {"$in": request_ids}, "status": {"$in": _scheduled_statuses}},
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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """Return the active 7-day plan for this week, or 404 if none exists."""
    plan = await cal_svc.get_active_plan(ctx["user_id"], db, brand_id=ctx["brand_id"])
    if not plan:
        raise HTTPException(status_code=404, detail="No active plan for this week")
    return UriResponse.get_single_data_response("calendar_plan", plan)


@router.post("/content-calendar/plan/generate")
async def generate_calendar_plan(
    request: CalendarGenerateRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_flexible_brand_context),
):
    """Generate (or force-regenerate) the 7-day content plan for this week."""
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    try:
        profile_result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
        brand = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        plan = await cal_svc.generate_plan(
            user_id=user_id,
            platforms=request.platforms,
            brand=brand,
            db=db,
            force=request.force_regenerate,
            brand_id=brand_id,
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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """Regenerate the content idea for a single day."""
    try:
        updated_plan = await cal_svc.regenerate_day(plan_id, day_index, ctx["user_id"], db, brand_id=ctx["brand_id"])
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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """Create a full content draft from a calendar day's idea."""
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    try:
        from app.agents.social_media_manager.services.content_calendar_service import _cal_scope
        plan = await db["content_calendar_plans"].find_one(
            {**_cal_scope(user_id, brand_id), "plan_id": plan_id}, {"_id": 0}
        )
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        day = next((d for d in plan["days"] if d["day_index"] == day_index), None)
        if not day:
            raise HTTPException(status_code=404, detail=f"Day {day_index} not found")

        seed_content = f"{day['title']}. {day['description']}"
        profile_result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
        brand = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        brand_context = BrandProfileService.to_brand_context(brand) if brand else {}
        brand_context["brand_id"] = brand_id

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
            await cal_svc.mark_acted_on(plan_id, day_index, draft_ids, user_id, db, brand_id=brand_id)

            if request.include_images:
                draft_ids = [d.get("draft_id") or d.get("id") for d in drafts if d]
                if draft_ids:
                    await db["content_drafts"].update_many(
                        {"id": {"$in": draft_ids}},
                        {"$set": {"has_image": True, "image_failed": False}},
                    )
                    for d in drafts:
                        d["has_image"] = True

                # Fire concurrently — BackgroundTasks would run these one at a time.
                _bg_image_tasks = [
                    asyncio.create_task(_generate_image_bg(
                        draft_id=d.get("draft_id") or d.get("id"),
                        platform=d.get("platform", "facebook"),
                        content=d.get("content", seed_content),
                        seed_content=seed_content,
                        brand_context=brand_context,
                        db=db,
                        reference_image=None,
                    ))
                    for d in drafts
                ]
                _BG_IMAGE_TASKS.update(_bg_image_tasks)
                for t in _bg_image_tasks:
                    t.add_done_callback(_BG_IMAGE_TASKS.discard)

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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """Return today's content suggestion from the active brand's plan."""
    result = await cal_svc.get_today_suggestion(ctx["user_id"], db, brand_id=ctx["brand_id"])
    return UriResponse.get_single_data_response("today_suggestion", result)


@router.get("/content-calendar/performance")
async def get_calendar_performance(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_flexible_brand_context),
):
    """Return aggregated post performance data for the active brand."""
    user_id = ctx["user_id"]
    from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService
    data = await PerformanceAnalyticsService.get_user_performance(user_id, db, brand_id=ctx["brand_id"])
    return UriResponse.get_single_data_response("performance", data)


@router.get("/content-calendar/trends")
async def get_calendar_trends(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_flexible_brand_context),
):
    """Return trending keywords for the active brand's industry."""
    user_id = ctx["user_id"]
    from app.services.TrendDataService import TrendDataService
    brand = await BrandProfileService.get(user_id, db, brand_id=ctx["brand_id"])
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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Get the active brand's complete content calendar

    Shows all content across different statuses:
    - draft, pending_approval, approved, scheduled, published
    """
    user_id = ctx["user_id"]

    try:
        # Brand-scoped: agency brands see only their own drafts
        query: Dict[str, Any] = _brand_scope(user_id, ctx["brand_id"])

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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Fetch real-time post analytics from Outstand for the active brand's published drafts.
    Returns aggregated summary + per-post breakdown + per-platform summary.
    """
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    _conn_scope = _brand_scope(user_id, brand_id)

    from datetime import timedelta
    import asyncio as _asyncio
    import httpx as _httpx

    try:
        date_filter = datetime.utcnow() - timedelta(days=days)

        # Fetch published drafts for the ACTIVE BRAND only
        all_published = await db["content_drafts"].find({
            **_brand_scope(user_id, brand_id),
            "status": "published",
        }).sort("published_date", -1).to_list(length=200)

        def _is_recent(d):
            pd = d.get("published_date")
            if pd is None:
                return False
            if isinstance(pd, datetime):
                return pd.replace(tzinfo=None) >= date_filter
            try:
                from dateutil.parser import parse as _dp
                return _dp(str(pd)).replace(tzinfo=None) >= date_filter
            except Exception:
                return True

        published = [d for d in all_published if _is_recent(d)]

        if not published:
            return UriResponse.get_single_data_response("performance", {
                "has_data": False,
                "total_published": 0,
                "date_range_days": days,
                "summary": {},
                "by_platform": {},
                "top_posts": [],
            })

        # Load direct connections — for platform routing
        direct_conns = await db["social_connections"].find(
            {**_conn_scope, "connected_via": {"$ne": "outstand"}},
            {"_id": 0, "platform": 1, "connected_via": 1, "page_access_token": 1,
             "ig_user_id": 1, "linkedin_access_token": 1},
        ).to_list(length=20)
        direct_conn_map = {c["platform"]: c for c in direct_conns}

        # Load ANY Instagram connection that has a page_access_token (incl. Outstand-linked)
        # so we can look up media from Instagram even for Outstand-published posts.
        all_ig_conns = await db["social_connections"].find(
            {**_conn_scope, "platform": "instagram", "page_access_token": {"$exists": True}},
            {"_id": 0, "ig_user_id": 1, "page_access_token": 1},
        ).to_list(length=5)
        ig_direct = next((c for c in all_ig_conns if c.get("ig_user_id") and c.get("page_access_token")), None)

        # Load Facebook direct connection for per-post analytics (same pattern as ig_direct)
        all_fb_conns = await db["social_connections"].find(
            {**_conn_scope, "platform": "facebook", "page_access_token": {"$exists": True}},
            {"_id": 0, "page_id": 1, "page_access_token": 1},
        ).to_list(length=5)
        fb_direct = next((c for c in all_fb_conns if c.get("page_id") and c.get("page_access_token")), None)

        outstand = OutstandService()
        _graph_base = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"

        async def _fetch_instagram_by_timestamp(draft, conn):
            """Find a published IG post by matching the draft's published_date to media timestamps.
            Used for Outstand posts where platform_post_id is an Outstand UUID, not an IG media ID."""
            ig_user_id = conn.get("ig_user_id")
            token = conn.get("page_access_token")
            pub_date = draft.get("published_date")
            if not ig_user_id or not token or not pub_date:
                return None
            try:
                from dateutil.parser import parse as _dp
                pub_dt = _dp(str(pub_date)) if isinstance(pub_date, str) else pub_date
                pub_dt = pub_dt.replace(tzinfo=None)
            except Exception:
                return None
            try:
                async with _httpx.AsyncClient(timeout=20) as _c:
                    media_resp = await _c.get(
                        f"{_graph_base}/{ig_user_id}/media",
                        params={"fields": "id,timestamp,like_count,comments_count,media_type,media_product_type", "limit": 100, "access_token": token},
                    )
                    media_list = media_resp.json().get("data", [])

                    best_match = None
                    best_diff = float("inf")
                    for m in media_list:
                        try:
                            m_dt = _dp(m["timestamp"]).replace(tzinfo=None)
                            diff = abs((pub_dt - m_dt).total_seconds())
                            if diff < 3600 and diff < best_diff:  # within 1 hour
                                best_diff = diff
                                best_match = m
                        except Exception:
                            pass

                    if not best_match:
                        return None

                    media_id = best_match["id"]
                    media_type = best_match.get("media_type", "IMAGE")
                    media_product_type = best_match.get("media_product_type", "")
                    is_reel = media_product_type == "REELS" or media_type == "REELS"
                    is_video = media_type == "VIDEO" and not is_reel

                    if is_reel:
                        metrics = "plays,reach,likes,comments,shares,saved,total_interactions"
                    elif is_video:
                        metrics = "reach,saved,video_views,total_interactions"
                    else:
                        metrics = "reach,saved,total_interactions"

                    impressions = reach = views = 0
                    try:
                        ins_resp = await _c.get(
                            f"{_graph_base}/{media_id}/insights",
                            params={"metric": metrics, "period": "lifetime", "access_token": token},
                        )
                        ins_data_ts = ins_resp.json()
                        if "error" in ins_data_ts:
                            print(f"⚠️ IG insights error for {media_id}: {ins_data_ts['error'].get('message')} (code {ins_data_ts['error'].get('code')})")
                        else:
                            for item in ins_data_ts.get("data", []):
                                val = item.get("total_value", {}).get("value") or \
                                      (item.get("values") or [{}])[0].get("value", 0)
                                name = item["name"]
                                if name == "impressions":
                                    impressions = val
                                elif name == "reach":
                                    reach = val
                                    impressions = impressions or val
                                elif name in ("plays", "video_views"):
                                    views = val
                    except Exception as ins_e:
                        print(f"⚠️ IG timestamp-lookup insights failed for {media_id}: {ins_e}")

                    likes = best_match.get("like_count", 0)
                    comments = best_match.get("comments_count", 0)
                    effective_reach = reach or impressions
                    print(f"[IG timestamp match] draft={draft.get('id')} media_id={media_id} diff={best_diff:.0f}s imp={impressions} reach={reach}")
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
                        "engagement_rate": round(((likes + comments) / effective_reach) * 100, 2) if effective_reach else None,
                    }
            except Exception as e:
                print(f"⚠️ Instagram timestamp lookup failed: {e}")
                return None

        async def _fetch_facebook_by_timestamp(draft, conn):
            """Find a published Facebook post by matching the draft's published_date to page post timestamps.
            Used for Outstand posts where platform_post_id is an Outstand UUID, not a FB post ID."""
            page_id = conn.get("page_id")
            token = conn.get("page_access_token")
            pub_date = draft.get("published_date")
            if not page_id or not token or not pub_date:
                return None
            try:
                from dateutil.parser import parse as _dp
                pub_dt = _dp(str(pub_date)) if isinstance(pub_date, str) else pub_date
                pub_dt = pub_dt.replace(tzinfo=None)
            except Exception:
                return None
            try:
                async with _httpx.AsyncClient(timeout=20) as _c:
                    posts_resp = await _c.get(
                        f"{_graph_base}/{page_id}/posts",
                        params={
                            "fields": "id,created_time,reactions.summary(total_count),comments.summary(total_count),shares",
                            "limit": 100,
                            "access_token": token,
                        },
                    )
                    posts_data = posts_resp.json().get("data", [])

                    best_match = None
                    best_diff = float("inf")
                    for p in posts_data:
                        try:
                            p_dt = _dp(p["created_time"]).replace(tzinfo=None)
                            diff = abs((pub_dt - p_dt).total_seconds())
                            if diff < 3600 and diff < best_diff:
                                best_diff = diff
                                best_match = p
                        except Exception:
                            pass

                    if not best_match:
                        return None

                    fb_post_id = best_match["id"]
                    likes = (best_match.get("reactions") or {}).get("summary", {}).get("total_count", 0)
                    comments = (best_match.get("comments") or {}).get("summary", {}).get("total_count", 0)
                    shares = (best_match.get("shares") or {}).get("count", 0)

                    impressions = reach = 0
                    try:
                        ins_resp = await _c.get(
                            f"{_graph_base}/{fb_post_id}/insights",
                            params={"metric": "post_impressions,post_impressions_unique", "access_token": token},
                        )
                        for item in ins_resp.json().get("data", []):
                            val = (item.get("values") or [{}])[-1].get("value", 0)
                            if item.get("name") == "post_impressions":
                                impressions = val
                            elif item.get("name") == "post_impressions_unique":
                                reach = val
                    except Exception as ins_e:
                        print(f"⚠️ FB post insights failed for {fb_post_id}: {ins_e}")

                    effective = reach or impressions or 1
                    print(f"[FB timestamp match] draft={draft.get('id')} post_id={fb_post_id} diff={best_diff:.0f}s imp={impressions} reach={reach}")
                    return {
                        "draft_id": draft.get("id"),
                        "platform_post_id": fb_post_id,
                        "platform": "facebook",
                        "content_preview": (draft.get("content") or "")[:120],
                        "published_at": draft.get("published_date", ""),
                        "image_url": draft.get("image_url"),
                        "likes": likes,
                        "comments": comments,
                        "shares": shares,
                        "views": impressions,
                        "impressions": impressions,
                        "reach": reach,
                        "engagement_rate": round(((likes + comments + shares) / effective) * 100, 2) if effective else None,
                    }
            except Exception as e:
                print(f"⚠️ Facebook timestamp lookup failed: {e}")
                return None

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

                    # Instagram Graph API v22+ removed "impressions" from per-media insights.
                    # Use "reach" + "total_interactions" for images/carousels.
                    # Reels still support "plays" and "reach".
                    if is_reel:
                        metrics = "plays,reach,likes,comments,shares,saved,total_interactions"
                    elif is_video:
                        metrics = "reach,saved,video_views,total_interactions"
                    else:
                        # IMAGE / CAROUSEL_ALBUM — impressions removed in v22+
                        metrics = "reach,saved,total_interactions"

                    impressions = reach = views = total_interactions = 0
                    try:
                        ins_resp = await _c.get(
                            f"{_graph_base}/{media_id}/insights",
                            params={"metric": metrics, "period": "lifetime", "access_token": token},
                        )
                        ins_data = ins_resp.json()
                        if "error" in ins_data:
                            print(f"⚠️ IG insights error for {media_id}: {ins_data['error'].get('message')} (code {ins_data['error'].get('code')})")
                        else:
                            for item in ins_data.get("data", []):
                                val = item.get("total_value", {}).get("value") or \
                                      (item.get("values") or [{}])[0].get("value", 0)
                                name = item["name"]
                                if name == "impressions":
                                    impressions = val
                                elif name == "reach":
                                    reach = val
                                    impressions = impressions or val  # use reach as impressions fallback
                                elif name in ("plays", "video_views"):
                                    views = val
                                elif name == "total_interactions":
                                    total_interactions = val
                    except Exception as ins_err:
                        print(f"⚠️ IG insights fetch failed for {media_id}: {ins_err}")

                likes = media_data.get("like_count", 0)
                comments = media_data.get("comments_count", 0)
                effective_reach = reach or impressions
                # Engagement rate is only meaningful when we have reach/impressions.
                # Return null when unavailable so the frontend shows "N/A" not "0%".
                if effective_reach:
                    eng_rate = round(((likes + comments) / effective_reach) * 100, 2)
                else:
                    eng_rate = None
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
                    "engagement_rate": eng_rate,
                    "insights_available": effective_reach > 0,
                }
            except Exception as e:
                print(f"⚠️ Instagram direct analytics failed for {media_id}: {e}")
                return None

        async def _analytics_fallback(draft):
            """Read stored content_analytics as fallback when live API isn't available.
            Always returns a result (with 0s if no stored data) so every published post
            is counted in the by_platform breakdown."""
            draft_id = draft.get("id")
            if not draft_id:
                return None
            ana = await db["content_analytics"].find_one({"draft_id": draft_id})
            likes       = int((ana or {}).get("likes", 0) or 0)
            comments    = int((ana or {}).get("comments", 0) or 0)
            shares      = int((ana or {}).get("shares", 0) or 0)
            impressions = int((ana or {}).get("impressions", 0) or 0)
            views       = int((ana or {}).get("views", 0) or 0)
            reach       = int((ana or {}).get("reach", 0) or 0)
            effective   = impressions or reach or 0
            platform    = (ana or {}).get("platform") or draft.get("platform", "unknown")
            return {
                "draft_id": draft_id,
                "platform_post_id": draft.get("platform_post_id") or "",
                "platform": platform,
                "content_preview": (draft.get("content") or "")[:120],
                "published_at": draft.get("published_date", ""),
                "image_url": draft.get("image_url"),
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "views": views,
                "impressions": impressions,
                "reach": reach,
                "engagement_rate": round(((likes + comments) / effective) * 100, 2) if effective else None,
            }

        import re as _re

        async def _fetch(draft):
            post_id = draft.get("platform_post_id")
            platform = draft.get("platform", "unknown")
            # Outstand stores the NATIVE social media ID as platform_post_id (e.g. Instagram
            # media IDs are 15-18 digit numbers).  Outstand's own internal post IDs are short
            # base64 strings (EgFFA, ZxDCu) or LinkedIn URNs.
            # Route numeric IG IDs directly to Instagram Graph API — Outstand analytics fails on them.
            is_ig_media_id = bool(post_id and platform == "instagram" and _re.match(r'^\d{15,}$', str(post_id)))
            # "queued" means Outstand accepted the post and will publish it (or already has).
            # Treat queued + any publish_response as an Outstand-routed post.
            is_outstand_post = bool(
                draft.get("outstand_post_status")  # "queued", "published", etc.
                or draft.get("publish_response")
                or (post_id and isinstance(post_id, str) and post_id.startswith("urn:li:"))  # LinkedIn URN
            )

            live_result = None
            print(f"[_fetch] platform={platform} post_id={repr(post_id)} is_ig={is_ig_media_id} is_outstand={is_outstand_post}", flush=True)

            # Strategy 1: real Instagram media ID — use Graph API directly.
            # Always run this when post_id looks like a real IG media ID (15+ digits),
            # regardless of outstand_post_status — Outstand queues real IG media IDs too.
            if post_id and platform == "instagram" and is_ig_media_id:
                conn = ig_direct or direct_conn_map.get(platform)
                if conn and conn.get("page_access_token"):
                    live_result = await _fetch_instagram_direct(draft, conn)

            # Strategy 2: Outstand-published post — get what Outstand has (likes/comments)
            if live_result is None and post_id and is_outstand_post and not str(post_id).startswith("urn:li:"):
                # Pre-check: if Outstand says the post failed to publish, skip analytics
                # and mark the result so the frontend can surface the failure.
                try:
                    post_status_data = await outstand.get_post(post_id)
                    outstand_post = post_status_data.get("post", {})
                    _outstand_containers = outstand_post.get("socialAccounts") or []
                    _any_failed = any(c.get("status") == "failed" for c in _outstand_containers)
                    if _any_failed and not outstand_post.get("publishedAt"):
                        _err = next((c.get("error") for c in _outstand_containers if c.get("status") == "failed"), "")
                        print(f"[Outstand] post {post_id} failed to publish: {_err[:120]}")
                        return {
                            "draft_id": draft.get("id"),
                            "platform_post_id": post_id,
                            "platform": platform,
                            "content_preview": (draft.get("content") or "")[:120],
                            "published_at": draft.get("published_date", ""),
                            "image_url": draft.get("image_url"),
                            "likes": 0, "comments": 0, "shares": 0,
                            "views": 0, "impressions": 0, "reach": 0,
                            "engagement_rate": None,
                            "publish_failed": True,
                            "publish_error": _err[:200] if _err else "Post failed to publish",
                        }
                except Exception:
                    pass  # non-critical — continue to analytics fetch
                try:
                    data = await outstand.get_post_analytics(post_id)
                    agg = data.get("aggregated_metrics") or {}
                    by_account = data.get("metrics_by_account") or []
                    # Log the full response so we can see what fields Outstand actually returns
                    print(f"[Outstand analytics] post_id={post_id} agg_keys={list(agg.keys())} agg={agg}")
                    if by_account:
                        print(f"[Outstand by_account] sample={by_account[0]}")
                    network = by_account[0]["social_account"]["network"] if by_account else platform

                    # Try multiple field name variants — Outstand may differ by plan/version
                    def _pick(*keys):
                        for k in keys:
                            v = agg.get(k)
                            if v:
                                return int(v)
                        # Also check per-account metrics
                        for acc in by_account:
                            m = acc.get("metrics") or acc.get("aggregated_metrics") or {}
                            for k in keys:
                                v = m.get(k)
                                if v:
                                    return int(v)
                        return 0

                    live_result = {
                        "draft_id": draft.get("id"),
                        "platform_post_id": post_id,
                        "platform": network or platform,
                        "content_preview": (draft.get("content") or "")[:120],
                        "published_at": draft.get("published_date", ""),
                        "image_url": draft.get("image_url"),
                        "likes":       _pick("total_likes", "likes", "reactions_count"),
                        "comments":    _pick("total_comments", "comments", "comments_count"),
                        "shares":      _pick("total_shares", "shares", "reposts"),
                        "views":       _pick("total_views", "views", "video_views", "plays"),
                        "impressions": _pick("total_impressions", "impressions", "total_reach", "reach"),
                        "reach":       _pick("total_reach", "reach", "unique_reach"),
                        "engagement_rate": round(agg.get("average_engagement_rate", 0) * 100, 2),
                    }
                    print(f"[Outstand analytics mapped] likes={live_result['likes']} imp={live_result['impressions']} reach={live_result['reach']}")
                except Exception as e:
                    print(f"⚠️ Outstand analytics failed for post {post_id}: {e}")

            # Strategy 2b: LinkedIn URN — query LinkedIn socialActions API directly
            if live_result is None and platform == "linkedin" and post_id and str(post_id).startswith("urn:li:"):
                li_conn = direct_conn_map.get("linkedin")
                if not li_conn:
                    li_conn_raw = await db["social_connections"].find_one(
                        {"user_id": user_id, "platform": "linkedin", "connection_status": "active"},
                        {"_id": 0, "linkedin_access_token": 1},
                    )
                    li_conn = li_conn_raw
                if li_conn and li_conn.get("linkedin_access_token"):
                    try:
                        from app.agents.social_media_manager.services.linkedin_direct_service import LinkedInDirectService
                        _li_svc = LinkedInDirectService()
                        sa = await _li_svc.get_post_social_actions(li_conn["linkedin_access_token"], post_id)
                        likes = sa.get("likes", 0)
                        comments = sa.get("comments", 0)
                        print(f"[LinkedIn direct] urn={post_id} likes={likes} comments={comments}", flush=True)
                        live_result = {
                            "draft_id": draft.get("id"),
                            "platform_post_id": post_id,
                            "platform": "linkedin",
                            "content_preview": (draft.get("content") or "")[:120],
                            "published_at": draft.get("published_date", ""),
                            "image_url": draft.get("image_url"),
                            "likes": likes,
                            "comments": comments,
                            "shares": 0,
                            "views": 0,
                            "impressions": 0,
                            "reach": 0,
                            "engagement_rate": None,
                        }
                    except Exception as _li_err:
                        print(f"⚠️ LinkedIn direct analytics failed for {post_id}: {_li_err}", flush=True)

            # Strategy 3: Instagram timestamp lookup — fills in impressions/reach that
            # Outstand doesn't sync, by finding the actual IG media via published_date.
            if platform == "instagram" and ig_direct:
                needs_impressions = live_result is None or (live_result.get("impressions") or 0) == 0
                if needs_impressions:
                    ts_result = await _fetch_instagram_by_timestamp(draft, ig_direct)
                    if ts_result:
                        if live_result:
                            # Merge: keep Outstand engagement counts, use IG for impressions/reach
                            live_result["impressions"] = ts_result["impressions"]
                            live_result["reach"] = ts_result.get("reach", 0)
                            live_result["views"] = ts_result.get("views", 0)
                            # Use IG likes/comments if Outstand had none
                            if not live_result.get("likes"):
                                live_result["likes"] = ts_result.get("likes", 0)
                            if not live_result.get("comments"):
                                live_result["comments"] = ts_result.get("comments", 0)
                            effective = live_result["impressions"] or live_result["reach"] or 1
                            live_result["engagement_rate"] = round(
                                ((live_result["likes"] + live_result["comments"]) / effective) * 100, 2
                            )
                        else:
                            live_result = ts_result

            # Strategy 3b: Facebook timestamp lookup — same pattern as Instagram above.
            if platform == "facebook" and fb_direct:
                needs_fb_metrics = live_result is None or (live_result.get("impressions") or 0) == 0
                if needs_fb_metrics:
                    ts_result = await _fetch_facebook_by_timestamp(draft, fb_direct)
                    if ts_result:
                        if live_result:
                            live_result["impressions"] = ts_result["impressions"]
                            live_result["reach"] = ts_result.get("reach", 0)
                            live_result["views"] = ts_result.get("impressions", 0)
                            if not live_result.get("likes"):
                                live_result["likes"] = ts_result.get("likes", 0)
                            if not live_result.get("comments"):
                                live_result["comments"] = ts_result.get("comments", 0)
                            if not live_result.get("shares"):
                                live_result["shares"] = ts_result.get("shares", 0)
                            effective = live_result["impressions"] or live_result["reach"] or 1
                            live_result["engagement_rate"] = round(
                                ((live_result["likes"] + live_result["comments"] + live_result["shares"]) / effective) * 100, 2
                            )
                        else:
                            live_result = ts_result

            # Fall back to content_analytics when live API not available
            return live_result or await _analytics_fallback(draft)

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

        def _build_insights_note(post_list):
            platforms = {p.get("platform") for p in post_list}
            if platforms == {"linkedin"} or platforms == {"linkedin", None}:
                return (
                    "LinkedIn's standard API does not provide impressions or engagement stats "
                    "for personal profiles. Upgrade to LinkedIn Marketing API for full analytics."
                )
            if "instagram" in platforms:
                return (
                    "Impressions and reach require an Instagram Business or Creator account. "
                    "Go to Instagram → Profile → Edit Profile → Switch to Professional Account."
                )
            return "Analytics will appear here once your posts have been live for at least 24 hours."
        total_posts = len(posts)
        insights_available = any(p.get("insights_available") or (p.get("impressions") or 0) > 0 for p in posts)
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
            "insights_available": insights_available,
            "insights_note": None if insights_available else _build_insights_note(posts),
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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Fetch account-level metrics (followers, engagement totals) for the active brand's
    connected social accounts via Outstand.
    """
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    # Outstand tenant = brand_id for agency brands, user_id for personal brand
    from app.models.brand_account import BrandAccount as _BA
    _outstand_tenant = brand_id if brand_id and brand_id != _BA.personal_brand_id(user_id) else user_id
    _conn_scope = _brand_scope(user_id, brand_id)

    import asyncio as _asyncio
    import time as _time
    import httpx as _httpx
    from datetime import timedelta

    try:
        outstand = OutstandService()
        until_ts = int(_time.time())
        since_ts = int((datetime.utcnow() - timedelta(days=days)).timestamp())

        # ── Outstand accounts (scoped to this brand's tenant) ─────────────────
        try:
            result = await outstand.list_accounts(tenant_id=_outstand_tenant)
            outstand_accounts = result.get("data", [])
        except Exception as _os_err:
            print(f"⚠️ Outstand list_accounts failed in account-metrics (non-fatal): {_os_err}")
            outstand_accounts = []

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

        # ── Direct (non-Outstand) connections scoped to active brand ──────────
        direct_conns = await db["social_connections"].find(
            {**_conn_scope, "connected_via": {"$ne": "outstand"}},
            {"_id": 0, "platform": 1, "connected_via": 1, "page_access_token": 1,
             "ig_user_id": 1, "page_name": 1, "page_id": 1, "account_name": 1},
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

                    # Account-level insights — Graph API v21+ rules:
                    #  • views + profile_views: period=day + metric_type=total_value (sums the period)
                    #  • reach: period=day without metric_type (returns daily array we sum)
                    # "views" counts reels + stories + posts, matching Instagram's Professional Dashboard.
                    total_views = total_impressions = total_reach = total_profile_views = 0
                    try:
                        ins_resp = await _c.get(
                            f"{_graph_base}/{ig_user_id}/insights",
                            params={
                                "metric": "views,profile_views",
                                "period": "day",
                                "metric_type": "total_value",
                                "since": since_ts,
                                "until": until_ts,
                                "access_token": token,
                            },
                        )
                        ins_json = ins_resp.json()
                        if ins_json.get("error"):
                            raise ValueError(ins_json["error"].get("message", "unknown error"))
                        for item in ins_json.get("data", []):
                            name = item.get("name", "")
                            val = item.get("total_value", {}).get("value") or 0
                            if name == "views":
                                total_views = val
                            elif name == "profile_views":
                                total_profile_views = val
                        total_impressions = total_views

                        # reach uses a different parameter set — fetch separately
                        reach_resp = await _c.get(
                            f"{_graph_base}/{ig_user_id}/insights",
                            params={
                                "metric": "reach",
                                "period": "day",
                                "since": since_ts,
                                "until": until_ts,
                                "access_token": token,
                            },
                        )
                        for item in reach_resp.json().get("data", []):
                            if item.get("name") == "reach":
                                total_reach = sum(v.get("value", 0) for v in item.get("values", []))

                        print(f"[IG insights v21] user={ig_user_id} views={total_views} reach={total_reach} pv={total_profile_views}")
                    except Exception as ig_ins_err:
                        print(f"⚠️ IG account insights failed: {ig_ins_err}")

                    # Aggregate per-post engagement in the period
                    posts_resp = await _c.get(
                        f"{_graph_base}/{ig_user_id}/media",
                        params={"fields": "like_count,comments_count,timestamp,media_type,media_product_type", "limit": 100, "access_token": token},
                    )
                    posts = posts_resp.json().get("data", [])
                    from datetime import timezone as _tz
                    since_dt = datetime.utcfromtimestamp(since_ts).replace(tzinfo=_tz.utc)
                    total_likes = total_comments = 0
                    post_views_sum = 0
                    post_ids_in_period = []
                    for post in posts:
                        try:
                            from datetime import datetime as _dt
                            post_dt = _dt.fromisoformat(post["timestamp"].replace("Z", "+00:00"))
                            if post_dt >= since_dt:
                                total_likes += post.get("like_count", 0)
                                total_comments += post.get("comments_count", 0)
                                post_ids_in_period.append(post.get("id"))
                        except Exception:
                            pass

                    # If Insights API returned 0 impressions, fall back to summing
                    # per-post reach via live IG API calls (same source as Top Posts)
                    if total_impressions == 0 and post_ids_in_period:
                        try:
                            async def _fetch_post_reach(pid):
                                try:
                                    r = await _c.get(
                                        f"{_graph_base}/{pid}/insights",
                                        params={"metric": "reach,total_interactions", "access_token": token},
                                    )
                                    items = r.json().get("data", [])
                                    for item in items:
                                        if item.get("name") == "reach":
                                            vals = item.get("values", [])
                                            return sum(v.get("value", 0) for v in vals) if vals else item.get("total_value", {}).get("value", 0)
                                except Exception:
                                    pass
                                return 0
                            reach_results = await _asyncio.gather(*[_fetch_post_reach(pid) for pid in post_ids_in_period[:20]])
                            post_views_sum = sum(r for r in reach_results if isinstance(r, (int, float)))
                            if post_views_sum:
                                total_impressions = post_views_sum
                                print(f"[IG] Insights API=0 → summed per-post reach: {total_impressions}")
                        except Exception as _fb_err:
                            print(f"[IG] per-post reach fallback failed: {_fb_err}")

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
                    "engagement_note": f"Total views (posts + reels + stories) for the last {days} days via Instagram Insights API",
                    "platform_specific": {
                        "username": profile.get("username"),
                        "biography": profile.get("biography"),
                        "profile_picture_url": profile.get("profile_picture_url"),
                        "views": total_views or total_impressions,
                        "reach": total_reach,
                        "profile_views": total_profile_views,
                    },
                    "period": {"since": since_ts, "until": until_ts},
                }
            except Exception as e:
                print(f"⚠️ Instagram direct account metrics failed: {e}")
                return None

        # ── LinkedIn direct connections (scoped to active brand) ─────────────
        linkedin_conns = await db["social_connections"].find(
            {**_conn_scope, "platform": "linkedin", "connection_status": "active"},
            {"_id": 0, "linkedin_access_token": 1, "person_urn": 1, "active_author_urn": 1,
             "account_name": 1, "username": 1, "pages": 1, "followers_count": 1},
        ).to_list(length=5)

        async def _fetch_linkedin_direct_metrics(conn):
            access_token = conn.get("linkedin_access_token")
            person_urn = conn.get("active_author_urn") or conn.get("person_urn")
            if not access_token or not person_urn:
                return None
            try:
                from app.agents.social_media_manager.services.linkedin_direct_service import LinkedInDirectService
                svc = LinkedInDirectService()

                # /v2/userinfo works with openid+profile scope (always granted)
                display_name = conn.get("account_name") or conn.get("username") or "LinkedIn"
                needs_reconnect = False
                try:
                    async with _httpx.AsyncClient(timeout=10) as _c:
                        ui = await _c.get("https://api.linkedin.com/v2/userinfo",
                                          headers={"Authorization": f"Bearer {access_token}"})
                        if ui.status_code == 200:
                            display_name = ui.json().get("name") or display_name
                except Exception:
                    pass

                # followers: networkSizes requires r_network (restricted scope) — not available
                # Use cached value from DB if present, otherwise 0
                followers_count = int(conn.get("followers_count", 0) or 0)

                # post count from DB (always accurate) — scoped to active brand
                li_drafts = await db["content_drafts"].find(
                    {**_brand_scope(user_id, brand_id), "status": "published",
                     "platform": "linkedin",
                     "platform_post_id": {"$exists": True, "$ne": None}},
                    {"_id": 0, "platform_post_id": 1},
                ).to_list(length=50)
                post_count = len(li_drafts)

                # LinkedIn does not allow reading likes/comments for personal posts
                # without Marketing Developer Platform (MDP) access.
                # Return None so the frontend can display "-" instead of misleading "0".
                total_likes = None
                total_comments = None

                return {
                    "account_id": person_urn,
                    "network": "linkedin",
                    "page_name": display_name,
                    "category": None,
                    "followers_count": followers_count,
                    "following_count": None,
                    "posts_count": post_count,
                    "engagement": {
                        "views": None,
                        "likes": total_likes,
                        "comments": total_comments,
                        "shares": None,
                        "reposts": None,
                        "quotes": None,
                    },
                    "engagement_note": "LinkedIn's standard API does not provide engagement stats for personal profiles. Upgrade to LinkedIn Marketing API for full analytics.",
                    "needs_reconnect": False,
                    "platform_specific": {
                        "person_urn": person_urn,
                        "pages": conn.get("pages", []),
                    },
                    "period": {"since": since_ts, "until": until_ts},
                }
            except Exception as e:
                print(f"⚠️ LinkedIn direct account metrics failed for {person_urn}: {e}", flush=True)
                return None

        async def _fetch_facebook_direct_metrics(conn):
            page_id = conn.get("page_id")
            token = conn.get("page_access_token")
            if not page_id or not token:
                return None
            try:
                page_name = conn.get("account_name") or "Facebook Page"
                followers_count = int(conn.get("followers_count", 0) or 0)
                category = conn.get("category")
                total_impressions = total_reach = 0
                total_likes = total_comments = total_shares = 0
                posts_count = 0

                async with _httpx.AsyncClient(timeout=30) as _c:
                    # 1. Page profile — followers, name, category
                    profile_resp = await _c.get(
                        f"{_graph_base}/{page_id}",
                        params={"fields": "name,fan_count,followers_count,category", "access_token": token},
                    )
                    profile = profile_resp.json()
                    if "error" not in profile:
                        page_name = profile.get("name") or page_name
                        followers_count = profile.get("fan_count") or profile.get("followers_count") or followers_count
                        category = profile.get("category") or category
                    else:
                        print(f"⚠️ FB page profile error: {profile.get('error', {}).get('message')}")

                    # 2. Page-level insights — impressions, reach
                    try:
                        ins_resp = await _c.get(
                            f"{_graph_base}/{page_id}/insights",
                            params={
                                "metric": "page_impressions,page_reach",
                                "period": "day",
                                "since": since_ts,
                                "until": until_ts,
                                "access_token": token,
                            },
                        )
                        for item in ins_resp.json().get("data", []):
                            daily_total = sum(v.get("value", 0) for v in (item.get("values") or []))
                            if item.get("name") == "page_impressions":
                                total_impressions = daily_total
                            elif item.get("name") == "page_reach":
                                total_reach = daily_total
                    except Exception as ins_err:
                        print(f"⚠️ FB insights failed: {ins_err}")

                    # 3. Real post engagement — reactions (likes), comments, shares from Graph API
                    try:
                        posts_resp = await _c.get(
                            f"{_graph_base}/{page_id}/posts",
                            params={
                                "fields": "id,created_time,reactions.summary(total_count),comments.summary(total_count),shares",
                                "limit": 100,
                                "since": since_ts,
                                "until": until_ts,
                                "access_token": token,
                            },
                        )
                        posts_data = posts_resp.json().get("data", [])
                        posts_count = len(posts_data)
                        for post in posts_data:
                            reactions = post.get("reactions") or {}
                            total_likes += (reactions.get("summary") or {}).get("total_count", 0)
                            comments = post.get("comments") or {}
                            total_comments += (comments.get("summary") or {}).get("total_count", 0)
                            shares = post.get("shares") or {}
                            total_shares += shares.get("count", 0)
                        print(f"[FB] {posts_count} posts in period → likes={total_likes} comments={total_comments} shares={total_shares}")
                    except Exception as posts_err:
                        print(f"⚠️ FB posts engagement failed: {posts_err}")

                return {
                    "account_id": page_id,
                    "network": "facebook",
                    "page_name": page_name,
                    "category": category,
                    "followers_count": followers_count,
                    "following_count": None,
                    "posts_count": posts_count or None,
                    "engagement": {
                        "views": total_impressions,
                        "likes": total_likes,
                        "comments": total_comments,
                        "shares": total_shares,
                        "reposts": 0,
                        "quotes": 0,
                        "reach": total_reach,
                    },
                    "engagement_note": f"Facebook Page metrics for the last {days} days (live Graph API)",
                    "platform_specific": {
                        "page_id": page_id,
                        "category": category,
                        "impressions": total_impressions,
                        "reach": total_reach,
                    },
                    "period": {"since": since_ts, "until": until_ts},
                }
            except Exception as e:
                print(f"⚠️ Facebook direct account metrics failed: {e}")
                return None

        async def _fetch_direct_metrics(conn):
            platform = conn.get("platform", "")
            connected_via = conn.get("connected_via", "")
            if connected_via in ("instagram_direct", "instagram_direct_oauth") or platform == "instagram":
                return await _fetch_instagram_direct_metrics(conn)
            elif platform == "facebook" or connected_via == "facebook_direct_oauth":
                return await _fetch_facebook_direct_metrics(conn)
            return None

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
    """Fetch the live status of an Outstand post by its ID."""
    try:
        outstand = OutstandService()
        data = await outstand.get_post(post_id)
        return UriResponse.get_single_data_response("outstand_post", data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/debug/outstand-analytics/{post_id}")
async def debug_outstand_analytics(
    post_id: str,
    token: dict = Depends(JWTBearer())
):
    """Return the raw Outstand analytics response for a post — shows exactly what fields are available."""
    try:
        outstand = OutstandService()
        data = await outstand.get_post_analytics(post_id)
        return UriResponse.get_single_data_response("outstand_analytics", data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/debug/performance-raw")
async def debug_performance_raw(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Show raw published drafts + their platform_post_id and outstand markers for this user."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    drafts = await db["content_drafts"].find(
        {"user_id": user_id, "status": "published"},
        {"_id": 0, "id": 1, "platform": 1, "platform_post_id": 1,
         "outstand_post_status": 1, "published_date": 1,
         "publish_response": {"$exists": True}},
    ).sort("published_date", -1).to_list(length=20)
    conns = await db["social_connections"].find(
        {"user_id": user_id},
        {"_id": 0, "platform": 1, "connected_via": 1, "ig_user_id": 1,
         "has_page_token": {"$cond": [{"$ifNull": ["$page_access_token", False]}, True, False]}},
    ).to_list(length=10)
    return UriResponse.get_single_data_response("debug_performance", {
        "drafts": drafts,
        "connections": conns,
    })

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
    ctx: dict = Depends(get_flexible_brand_context),
):
    """Get a single draft by ID — scoped to the active brand.
    **Authentication**: Accepts both JWT (Dashboard/Frontend) and API Key (SDK)
    """
    user_id = ctx["user_id"]
    scope = _brand_scope(user_id, ctx["brand_id"])

    draft = await db["content_drafts"].find_one({**scope, "id": draft_id}, {"_id": 0})
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
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Get the brand profile (onboarding data) for the active brand."""
    try:
        return await BrandProfileService.get(ctx["user_id"], db, brand_id=ctx["brand_id"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/brand-profile/logo")
async def upload_brand_logo(
    file: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Upload a brand logo image. Saves to local static storage and stores the URL
    in the user's brand profile. Accepted formats: PNG, JPG, WEBP, SVG.
    """
    import os, uuid

    user_id = ctx["user_id"]
    brand_id = ctx.get("brand_id")

    allowed_types = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/svg+xml"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}. Use PNG, JPG, WEBP, or SVG.")

    try:
        contents = await file.read()
        if len(contents) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Logo file must be under 5 MB.")

        from app.utils.cloudinary_upload import upload_bytes
        logo_url = await upload_bytes(contents, folder="uri-social/logos")

        # Scope the write to the ACTIVE brand. This previously updated
        # {"user_id": user_id} with no brand_id, so for a multi-brand account
        # update_one wrote the logo onto whichever profile matched first — a
        # DIFFERENT brand than the one being edited (observed live: two brands
        # under one account ended up sharing a single logo).
        from app.models.brand_account import BrandAccount
        personal_bid = BrandAccount.personal_brand_id(user_id)
        if brand_id and brand_id != personal_bid:
            scope = {"brand_id": brand_id}
            set_fields = {"logo_url": logo_url, "brand_id": brand_id,
                          "user_id": user_id, "updated_at": datetime.utcnow()}
        else:
            # personal/solo brand — prefer the brand_id-keyed doc, else the
            # legacy user_id-only doc (from before brand_id existed).
            legacy = await db["brand_profiles"].find_one(
                {"user_id": user_id, "brand_id": {"$exists": False}}, {"_id": 1})
            if legacy:
                scope = {"user_id": user_id, "brand_id": {"$exists": False}}
                set_fields = {"logo_url": logo_url, "updated_at": datetime.utcnow()}
            else:
                scope = {"brand_id": personal_bid}
                set_fields = {"logo_url": logo_url, "brand_id": personal_bid,
                              "user_id": user_id, "updated_at": datetime.utcnow()}

        await db["brand_profiles"].update_one(scope, {"$set": set_fields}, upsert=True)

        return UriResponse.get_single_data_response("logo_upload", {"logo_url": logo_url})

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/brand-profile/sample-template")
async def upload_sample_template(
    file: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_flexible_brand_context),
):
    """
    Upload a sample design or content template. Saves to local static storage and
    appends the URL to the user's brand profile sample_template_urls list.
    Accepted formats: PNG, JPG, WEBP, PDF. Max 10 MB per file.
    """
    import os, uuid

    user_id = ctx["user_id"]

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
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Save or update the brand profile for the active brand."""
    user_id = ctx["user_id"]
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
        return await BrandProfileService.save(user_id, payload, db, brand_id=ctx["brand_id"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/brand-profile/analyze-voice-samples")
async def analyze_voice_samples(
    request: Dict[str, Any],
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_flexible_brand_context),
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
    user_id = ctx["user_id"]

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
    preassigned_style: Optional[Dict[str, Any]] = None,
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

    print(f"🚀 BG IMAGE TASK STARTED for draft {draft_id[:12]}... platform={platform}")

    try:
        from app.agents.social_media_manager.services.style_library import pick_next_style

        # Build the correct brand profile scope for style lookups.
        # Using user_id alone returns the personal brand profile even when an agency
        # brand is active — use brand_id when available so the right style/font is used.
        _bc_brand_id = brand_context.get("brand_id", "")
        _bc_user_id = brand_context.get("user_id", "")
        _style_profile_scope = {"brand_id": _bc_brand_id} if _bc_brand_id else {"user_id": _bc_user_id}

        # ── Visual style rotation ─────────────────────────────────────────────
        # For carousel posts: lock style to first slide, reuse for all subsequent slides
        # For regular posts: rotate style normally
        if post_type == "carousel" and carousel_id and slide_index is not None:
            if slide_index == 0:
                # First slide: pick style and cache it for this carousel
                _bp = await db["brand_profiles"].find_one(
                    _style_profile_scope,
                    {"style_selections": 1, "style_prompt_fragments": 1, "style_rotation_index": 1, "industry": 1, "selected_custom_guides": 1, "selected_custom_guides_v2": 1},
                ) or {}

                # Check if user has selected custom guides (V1 or V2)
                _custom_guide_ids_v1 = _bp.get("selected_custom_guides") or []
                _custom_guide_ids_v2 = _bp.get("selected_custom_guides_v2") or []
                _all_custom_guide_ids = _custom_guide_ids_v1 + _custom_guide_ids_v2
                _custom_guide_id = None  # Initialize to avoid UnboundLocalError

                if _all_custom_guide_ids:
                    # Rotate through custom guides (V1 + V2) like library styles
                    from app.agents.social_media_manager.services.custom_visual_guide_service import CustomVisualGuideService
                    from app.agents.social_media_manager.services.custom_visual_guide_v2_service import CustomVisualGuideV2Service

                    _rotation_index = int(_bp.get("style_rotation_index") or 0)
                    _custom_guide_id = _all_custom_guide_ids[_rotation_index % len(_all_custom_guide_ids)]

                    # Determine if this is V1 or V2
                    is_v2 = _custom_guide_id in _custom_guide_ids_v2

                    if is_v2:
                        # V2: Load guide and apply reference image
                        custom_guide = await db["custom_visual_guides"].find_one({"_id": ObjectId(_custom_guide_id), "version": "v2"})
                        if custom_guide:
                            _fragment = ""  # V2 doesn't use prompt fragments
                            _slug = f"custom_v2_{_custom_guide_id[:8]}"
                            _next_index = (_rotation_index + 1) % (len(_all_custom_guide_ids) + len(_bp.get("style_selections") or []))
                            brand_context = {
                                **brand_context,
                                "style_slug": _slug,
                                "custom_guide_v2_id": _custom_guide_id,
                                "custom_guide_v2_reference_image": custom_guide.get("original_image_url")
                            }
                            print(f"🎨 Custom V2 guide [{custom_guide.get('name')}] applied for all {total_slides} carousel slides")
                    else:
                        # V1: Use prompt fragment
                        custom_guide = await CustomVisualGuideService.get_guide_detail(_custom_guide_id, db)
                        if custom_guide:
                            _fragment = custom_guide.get("prompt_fragment", "")
                            _slug = f"custom_{_custom_guide_id[:8]}"
                            _next_index = (_rotation_index + 1) % (len(_all_custom_guide_ids) + len(_bp.get("style_selections") or []))
                            brand_context = {**brand_context, "style_prompt_fragment": _fragment, "style_slug": _slug, "custom_guide_id": _custom_guide_id}
                            print(f"🎨 Custom V1 guide [{custom_guide.get('name')}] applied for all {total_slides} carousel slides")

                            # Track V1 usage
                            await CustomVisualGuideService.track_guide_usage(_custom_guide_id, False, db)
                else:
                    # Fallback to library style rotation
                    _style_selections = _bp.get("style_selections") or []
                    _style_prompt_fragments = _bp.get("style_prompt_fragments") or []
                    _rotation_index = int(_bp.get("style_rotation_index") or 0)
                    _industry = _bp.get("industry") or brand_context.get("industry", "")

                    _slug, _fragment, _next_index = pick_next_style(
                        _style_selections, _rotation_index, _industry, _style_prompt_fragments
                    )

                if _fragment:
                    # Cache style in draft document for subsequent slides
                    await db["content_drafts"].update_one(
                        {"id": carousel_id},
                        {"$set": {
                            "carousel_style_slug": _slug,
                            "carousel_style_fragment": _fragment
                        }}
                    )

                    # Only increment rotation index ONCE per carousel (not per slide) - skip for custom guides
                    if not _custom_guide_id:
                        print(f"🎨 Carousel style [{_slug}] applied for all {total_slides} slides (next index: {_next_index})")
                        await db["brand_profiles"].update_one(
                            _style_profile_scope,
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
            # Regular post: use preassigned style if provided (avoids race condition),
            # otherwise check for custom guides (V1 + V2) first, then fallback to style rotation
            if preassigned_style:
                # Use pre-assigned style/guide from the sequential assignment phase.
                # Mirrors the three branches the dynamic fallback below handles.
                _ptype = preassigned_style.get("type")
                _slug = preassigned_style.get("slug", "")

                if _ptype == "custom_v2":
                    brand_context = {
                        **brand_context,
                        "style_slug": _slug,
                        "custom_guide_v2_id": preassigned_style.get("custom_guide_id"),
                        "custom_guide_v2_reference_image": preassigned_style.get("reference_image"),
                    }
                    print(f"🎨 Custom V2 guide [{_slug}] applied (pre-assigned to avoid race condition)")
                elif _ptype == "custom_v1":
                    _fragment = preassigned_style.get("fragment", "")
                    brand_context = {
                        **brand_context,
                        "style_prompt_fragment": _fragment,
                        "style_slug": _slug,
                        "custom_guide_id": preassigned_style.get("custom_guide_id"),
                    }
                    print(f"🎨 Custom V1 guide [{_slug}] applied (pre-assigned to avoid race condition)")
                else:
                    _fragment = preassigned_style.get("fragment", "")
                    if _fragment:
                        brand_context = {**brand_context, "style_prompt_fragment": _fragment, "style_slug": _slug}
                        print(f"🎨 Style [{_slug}] applied (pre-assigned to avoid race condition)")
            else:
                # Fallback to dynamic assignment (used by regenerate_image and other paths)
                _bp = await db["brand_profiles"].find_one(
                    _style_profile_scope,
                    {"style_selections": 1, "style_prompt_fragments": 1, "style_rotation_index": 1, "industry": 1, "selected_custom_guides": 1, "selected_custom_guides_v2": 1},
                ) or {}

                # Check if user has selected custom guides (V1 or V2)
                _custom_guide_ids_v1 = _bp.get("selected_custom_guides") or []
                _custom_guide_ids_v2 = _bp.get("selected_custom_guides_v2") or []
                _all_custom_guide_ids = _custom_guide_ids_v1 + _custom_guide_ids_v2
                _custom_guide_id = None  # Initialize to avoid UnboundLocalError

                if _all_custom_guide_ids:
                    # Rotate through custom guides (V1 + V2) like library styles
                    from app.agents.social_media_manager.services.custom_visual_guide_service import CustomVisualGuideService
                    from app.agents.social_media_manager.services.custom_visual_guide_v2_service import CustomVisualGuideV2Service

                    _rotation_index = int(_bp.get("style_rotation_index") or 0)
                    _custom_guide_id = _all_custom_guide_ids[_rotation_index % len(_all_custom_guide_ids)]

                    # Determine if this is V1 or V2
                    is_v2 = _custom_guide_id in _custom_guide_ids_v2

                    if is_v2:
                        # V2: Load guide and apply reference image
                        custom_guide = await db["custom_visual_guides"].find_one({"_id": ObjectId(_custom_guide_id), "version": "v2"})
                        if custom_guide:
                            _fragment = ""  # V2 doesn't use prompt fragments
                            _slug = f"custom_v2_{_custom_guide_id[:8]}"
                            _next_index = (_rotation_index + 1) % (len(_all_custom_guide_ids) + len(_bp.get("style_selections") or []))
                            brand_context = {
                                **brand_context,
                                "style_slug": _slug,
                                "custom_guide_v2_id": _custom_guide_id,
                                "custom_guide_v2_reference_image": custom_guide.get("original_image_url")
                            }
                            print(f"🎨 Custom V2 guide [{custom_guide.get('name')}] applied for this image (next index: {_next_index})")

                            # Update rotation index
                            await db["brand_profiles"].update_one(
                                _style_profile_scope,
                                {"$set": {"style_rotation_index": _next_index}}
                            )
                    else:
                        # V1: Use prompt fragment
                        custom_guide = await CustomVisualGuideService.get_guide_detail(_custom_guide_id, db)
                        if custom_guide:
                            _fragment = custom_guide.get("prompt_fragment", "")
                            _slug = f"custom_{_custom_guide_id[:8]}"
                            _next_index = (_rotation_index + 1) % (len(_all_custom_guide_ids) + len(_bp.get("style_selections") or []))
                            brand_context = {**brand_context, "style_prompt_fragment": _fragment, "style_slug": _slug, "custom_guide_id": _custom_guide_id}
                            print(f"🎨 Custom V1 guide [{custom_guide.get('name')}] applied for this image (next index: {_next_index})")

                            # Track V1 usage
                            await CustomVisualGuideService.track_guide_usage(_custom_guide_id, False, db)

                            # Update rotation index
                            await db["brand_profiles"].update_one(
                                _style_profile_scope,
                                {"$set": {"style_rotation_index": _next_index}}
                            )
                else:
                    # Fallback to library style rotation
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
                            _style_profile_scope,
                            {"$set": {"style_rotation_index": _next_index}},
                        )

        # For story posts pass image_type="story" so we get 1080x1920 dimensions
        image_type = "story" if post_type == "story" else "post_image"

        # ========== REFERENCE IMAGE vs V2 GUIDE PRIORITY ==========
        # Priority: User-uploaded reference image > V2 guide > Standard generation.
        # This check must run BEFORE ever touching custom_guide_v2_reference_image —
        # assigning the V2 guide's own image into `reference_image` first (as this
        # code used to) made every V2 guide selection immediately look like "the
        # user uploaded their own image", permanently discarding the V2 guide and
        # falling through to the standard generation flow instead — the guide's
        # reference image was detected, then thrown away in the very next check.
        v2_guide_id = brand_context.get("custom_guide_v2_id")

        if reference_image:
            # User explicitly uploaded reference image - highest priority
            # Use standard generation flow with reference image (skip V2 guide)
            print(f"📸 User reference image detected - using standard generation (V2 guide ignored)")
            v2_guide_id = None  # Override V2 guide when reference image is provided

        if v2_guide_id:
            print(f"🎨 V2 CUSTOM GUIDE DETECTED - Using pure style cloning")
            print(f"V2 Guide ID: {v2_guide_id}")

            from app.agents.social_media_manager.services.custom_visual_guide_v2_service import CustomVisualGuideV2Service

            # Style cloning with the brand's real identity — the art-director
            # meta-prompt maps the reference's color STRATEGY onto these colors
            # (and describes the logo/font/tone), so omitting them here doesn't
            # mean "no color guidance" — it means the template's hardcoded
            # fallback colors (#000000/#FFFFFF/#FF0000) get used instead, which
            # is why V2 renders used to come out black/white/red regardless of
            # the brand's actual palette.
            minimal_brand_context = {
                "brand_name": brand_context.get("brand_name", ""),
                "logo_url": brand_context.get("logo_url", ""),
                "logo_position": brand_context.get("logo_position", "bottom_right"),
                "logo_description": brand_context.get("logo_description", "brand logo"),
                "brand_colors": brand_context.get("brand_colors") or [],
                "font_style": brand_context.get("font_style") or brand_context.get("primary_font", ""),
                "tone": brand_context.get("brand_voice") or brand_context.get("tone", "professional"),
                "default_link": brand_context.get("default_link", ""),
            }

            # Extract headline/subtext/cta from content if available
            headline = content.split("\n")[0] if content else seed_content[:50]
            subtext = content.split("\n")[1] if "\n" in content else ""

            # Get CTA - check override_cta first, then cta_styles with round-robin, then default_link
            override_cta = brand_context.get("override_cta")
            if override_cta:
                cta = override_cta
            else:
                cta_styles_list = brand_context.get("cta_styles", [])
                if isinstance(cta_styles_list, list) and cta_styles_list:
                    # Use round-robin rotation for even CTA distribution
                    cta_rotation_index = brand_context.get("cta_rotation_index", 0)
                    if cta_rotation_index >= len(cta_styles_list):
                        cta_rotation_index = 0
                    cta = cta_styles_list[cta_rotation_index]
                    # Update for next time (will be saved by main flow)
                    next_index = (cta_rotation_index + 1) % len(cta_styles_list)
                    brand_context["cta_rotation_index"] = next_index
                    print(f"🔄 V2 Guide CTA rotation: using '{cta}' (index {cta_rotation_index}/{len(cta_styles_list)-1}), next: {next_index}")
                else:
                    cta = brand_context.get("default_link", "Learn more")

            _v2_result = await CustomVisualGuideV2Service.generate_image_with_v2_guide(
                guide_id=v2_guide_id,
                seed_content=seed_content,
                brand_context=minimal_brand_context,  # Minimal context for pure cloning
                platform=platform,
                headline=headline,
                subtext=subtext,
                cta=cta,
                db=db,
            )
            # generate_image_with_v2_guide() returns its own {"success", "image_url",
            # ...} shape (also used as-is by the standalone /custom-guides-v2/generate
            # endpoint) — normalize it to the {"status", "responseData", "responseMessage"}
            # shape the rest of this function expects from the standard generation flow.
            # Without this, every successful V2 generation read as a failure below
            # (status was never set on the raw V2 dict), so the real image was silently
            # discarded and the draft was marked image_failed even though generation
            # had already succeeded (confirmed live: "[V2] ✅ Image generated
            # successfully" immediately followed by "BG image gen failed... None").
            image_result = {
                "status": _v2_result.get("success", False),
                "responseData": {"image_url": _v2_result.get("image_url")},
                "responseMessage": None if _v2_result.get("success") else "V2 guide image generation failed",
            }
        else:
            # Standard generation flow (V1 guides or no custom guide)
            # Extract V2 reference image from brand_context if present (legacy fallback)
            # BUT: Don't override user's uploaded reference image
            if not reference_image:
                v2_reference_image = brand_context.get("custom_guide_v2_reference_image")
                if v2_reference_image:
                    reference_image = v2_reference_image
                    print(f"📸 Using V2 reference image (legacy): {reference_image[:80]}...")

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
            if db is not None:
                if post_type == "carousel" and slide_index is not None:
                    await db["content_drafts"].update_one(
                        {"id": draft_id},
                        {"$set": {f"slides.{slide_index}.image_failed": True}},
                    )
                else:
                    await db["content_drafts"].update_one(
                        {"id": draft_id},
                        {"$set": {"image_failed": True, "updated_at": datetime.utcnow()}},
                    )
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

        print(f"[Canvas Editor DEBUG] final_url exists: {final_url is not None}, db exists: {db is not None}")
        if final_url:
            print(f"[Canvas Editor DEBUG] final_url value: {final_url[:100]}...")

        if final_url and db is not None:
            # ========== CANVAS EDITOR: LAYERED DOCUMENT GENERATION ==========
            # Check if canvas editor is enabled for this user
            canvas_doc = None
            try:
                user_id = brand_context.get("user_id", "")
                print(f"[Canvas Editor] Checking if enabled for user: {user_id}")

                _bp = await db["brand_profiles"].find_one(
                    {"user_id": user_id},
                    {"canvas_editor_enabled": 1}
                )
                canvas_enabled = _bp.get("canvas_editor_enabled", False) if _bp else False

                print(f"[Canvas Editor] Profile found: {_bp is not None}, Enabled: {canvas_enabled}")

                if canvas_enabled:
                    from app.agents.social_media_manager.services.layer_extraction_service import LayerExtractionService

                    print(f"[Canvas Editor] Feature enabled - extracting layers for draft {draft_id}")

                    # Build metadata to help GPT-4 Vision extract layers accurately
                    prompt_metadata = {
                        "seed_content": seed_content,
                        "brand_name": brand_context.get("brand_name", ""),
                        "logo_position": brand_context.get("logo_position", ""),
                        "visual_style": brand_context.get("style_slug", ""),
                    }

                    # Extract layers and create layered document
                    canvas_doc = await LayerExtractionService.extract_and_create_document(
                        image_url=final_url,
                        prompt_metadata=prompt_metadata,
                        canvas_width=1080,
                        canvas_height=1080
                    )

                    print(f"[Canvas Editor] ✅ Extracted {len(canvas_doc.get('layers', []))} layers for draft {draft_id}")

            except Exception as canvas_err:
                print(f"[Canvas Editor] ⚠️ Layer extraction failed for draft {draft_id}: {canvas_err}")
                # Continue without canvas document - not a critical failure
                canvas_doc = None

            # ========== SAVE TO DATABASE ==========
            if post_type == "carousel" and slide_index is not None:
                # Update the specific slide's image_url in the slides array
                update_fields = {
                    f"slides.{slide_index}.image_url": final_url,
                    f"slides.{slide_index}.image_failed": False,
                    "has_image": True,
                }

                # Add canvas document to slide if generated
                if canvas_doc:
                    update_fields[f"slides.{slide_index}.document"] = canvas_doc
                    update_fields[f"slides.{slide_index}.document_version"] = 1

                result = await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {"$set": update_fields}
                )
                print(f"✅ BG carousel slide {slide_index} image saved for draft {draft_id}: matched={result.matched_count}")

                # Save updated CTA rotation index to brand profile (for round-robin CTA rotation)
                # Only save once per carousel (not for every slide) - check if first slide
                if slide_index == 0 and brand_context.get("cta_rotation_index") is not None:
                    user_id = brand_context.get("user_id", "")
                    brand_id = brand_context.get("brand_id")
                    _cta_profile_scope = {"brand_id": brand_id} if brand_id else {"user_id": user_id}

                    await db["brand_profiles"].update_one(
                        _cta_profile_scope,
                        {"$set": {"cta_rotation_index": brand_context["cta_rotation_index"]}}
                    )
                    print(f"🔄 CTA rotation index updated to {brand_context['cta_rotation_index']}")
            else:
                # Regular post - save both image_url and document
                update_fields = {
                    "image_url": final_url,
                    "has_image": True
                }

                # Add canvas document if generated
                if canvas_doc:
                    update_fields["document"] = canvas_doc
                    update_fields["document_version"] = 1
                    update_fields["preview_url"] = final_url  # Use generated image as preview

                result = await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {"$set": update_fields}
                )
                print(f"✅ BG IMAGE TASK COMPLETED for draft {draft_id[:12]}... matched={result.matched_count}")
                print(f"   Image URL: {final_url[:80]}...")
                if canvas_doc:
                    print(f"✅ Canvas document saved for draft {draft_id} with {len(canvas_doc.get('layers', []))} layers")

                # Save updated CTA rotation index to brand profile (for round-robin CTA rotation)
                if brand_context.get("cta_rotation_index") is not None:
                    user_id = brand_context.get("user_id", "")
                    brand_id = brand_context.get("brand_id")
                    _cta_profile_scope = {"brand_id": brand_id} if brand_id else {"user_id": user_id}

                    await db["brand_profiles"].update_one(
                        _cta_profile_scope,
                        {"$set": {"cta_rotation_index": brand_context["cta_rotation_index"]}}
                    )
                    print(f"🔄 CTA rotation index updated to {brand_context['cta_rotation_index']}")
        else:
            if post_type == "carousel" and slide_index is not None:
                # Mark slide as failed
                await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {"$set": {f"slides.{slide_index}.image_failed": True}},
                )
            elif db is not None:
                await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {"$set": {"image_failed": True, "updated_at": datetime.utcnow()}},
                )
            print(f"⚠️  BG image not saved for draft {draft_id} (no public URL)")

    except Exception as e:
        # Mark slide (or whole draft) as failed on exception, so the UI stops
        # showing the shimmer forever instead of silently leaving has_image=True.
        if db is not None:
            try:
                if post_type == "carousel" and slide_index is not None:
                    await db["content_drafts"].update_one(
                        {"id": draft_id},
                        {"$set": {f"slides.{slide_index}.image_failed": True}},
                    )
                else:
                    await db["content_drafts"].update_one(
                        {"id": draft_id},
                        {"$set": {"image_failed": True, "updated_at": datetime.utcnow()}},
                    )
            except Exception:
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


# ---------------------------------------------------------------------------
# URI Agent chat — in-app assistant
# ---------------------------------------------------------------------------

_AGENT_SYSTEM_PROMPT_TEMPLATE = """You are URI Agent, the built-in AI assistant for URI Social — a social media management platform. You help users navigate the platform, answer questions about their brand, understand features, and troubleshoot issues.

## This user's brand profile
{brand_context}

## Platform sections (use these exact keys when returning a navigate action)
- "workspace"     → URI Agent chat (current section)
- "schedule"      → Posting Schedule — drafts waiting for review, scheduled posts, content calendar
- "performance"   → Performance — post analytics, engagement metrics, reach per platform
- "intel"         → Market Intel — competitor analysis, trending topics, audience insights
- "playbook"      → Brand Playbook — tone of voice, brand colours, content guidelines
- "blog"          → Blog Generator — AI-generated long-form blog posts
- "blog-drafts"   → Blog Drafts — saved and published blog posts
- "connections"   → Connected Accounts — connect/disconnect Facebook, Instagram, WhatsApp, LinkedIn
- "settings"      → Settings — account settings, approval workflow, auto-scheduling preferences
- "billing"       → Billing — subscription plans, credits, payment history
- "notifications" → Notifications — activity feed and alerts

## Key features to know
- **Content generation**: Users describe a topic/campaign and URI generates posts for all connected platforms simultaneously. Posts appear in Posting Schedule → Needs Review.
- **Platforms supported**: Instagram (feed, carousel, story), Facebook (feed, carousel), WhatsApp, LinkedIn. Twitter/X is coming soon.
- **Approval workflow**: "Auto-approve" publishes immediately; "Manual review" sends drafts to Needs Review first. Configurable in Settings.
- **Scheduling**: Pick a date/time per post. The cron job publishes every 5 minutes — posts go live within 5 min of their scheduled time.
- **Images**: Generate AI images per post. Instagram requires an image to publish. Carousel posts have per-slide images.
- **Connected Accounts**: Facebook uses Outstand OAuth; Instagram uses Meta direct OAuth. They connect independently.
- **Credits**: Each content generation costs 1 credit. Credits can be topped up in Billing.
- **Blog Generator**: Generates long-form SEO blog posts separately from social posts.
- **Brand Playbook**: Stores brand voice, tone, colours, and audience — used to personalise generated content.
- **Market Intel**: Shows trending topics and competitor post analysis for your industry.
- **Auto-scheduling**: URI Agent can auto-generate and schedule posts on a recurring basis. Configure in Settings.

## Common issues
- "Instagram not publishing" → Check Connected Accounts. Instagram requires a Business/Creator account linked to a Facebook page.
- "Post failed" → Check Connected Accounts to confirm the platform is still connected. Token may have expired — reconnect.
- "No drafts showing" → Go to Posting Schedule. If approval is set to Auto-approve they appear in Scheduled, otherwise in Needs Review.
- "Image not generating" → Images generate in the background after draft creation. Refresh the draft after ~30 seconds.
- "Can't schedule" → Ensure the platform account is connected first. Then select drafts and click Schedule All.

## Navigation rules — read carefully
ALWAYS set navigate (never null) when the user says "show me", "take me", "go to", "open", "where is", or names a section.

Phrase → key mapping:
- "show me billing" / "billing" / "credits" / "plans" / "subscription" → "billing"
- "connect my instagram" / "connect accounts" / "connected accounts" → "connections"
- "show me my drafts" / "drafts" / "posting schedule" / "scheduled posts" / "needs review" → "schedule"
- "show me performance" / "analytics" / "stats" / "engagement" → "performance"
- "market intel" / "competitor" / "trends" → "intel"
- "brand playbook" / "brand voice" / "tone" → "playbook"
- "blog" / "blog generator" / "write a blog" → "blog"
- "settings" / "approval workflow" / "auto-schedule" → "settings"
- "notifications" → "notifications"
- "generate posts" / "create posts" / "write posts" / "create content" → "schedule"

When the user asks to generate, create, or write social media posts, reply with something like "Head over to Posting Schedule where you can generate posts for your brand." and set navigate to "schedule".

Set navigate to null ONLY when the user is asking a general question with no navigation intent.

## Response rules
- Be concise and friendly — 1-4 sentences max for simple questions.
- When answering questions about the user's brand, use the brand profile above. Be specific — use their actual brand name, industry, voice, and audience.
- If a brand profile field is empty, say you don't have that info yet and suggest they complete their Brand Playbook.
- Never make up features that don't exist.

## Response format
Your ENTIRE response must be a single valid JSON object — no text before it, no text after it, no markdown fences.
Return ONLY this raw JSON:
{{"reply": "<your plain-text reply>", "navigate": "<section key or null>"}}
"""


def _build_agent_system_prompt(brand_context: dict) -> str:
    """Inject brand profile into the system prompt."""
    if not brand_context:
        brand_str = "No brand profile set up yet. Suggest the user completes their Brand Playbook."
    else:
        lines = []
        if brand_context.get("brand_name"):
            lines.append(f"- Brand name: {brand_context['brand_name']}")
        if brand_context.get("industry"):
            lines.append(f"- Industry: {brand_context['industry']}")
        if brand_context.get("business_description"):
            lines.append(f"- Business: {brand_context['business_description']}")
        if brand_context.get("tagline"):
            lines.append(f"- Tagline: {brand_context['tagline']}")
        if brand_context.get("brand_voice"):
            lines.append(f"- Brand voice: {brand_context['brand_voice']}")
        if brand_context.get("target_audience"):
            lines.append(f"- Target audience: {brand_context['target_audience']}")
        if brand_context.get("key_products_services"):
            lines.append(f"- Key products/services: {', '.join(brand_context['key_products_services'])}")
        if brand_context.get("content_pillars"):
            lines.append(f"- Content pillars: {', '.join(brand_context['content_pillars'])}")
        if brand_context.get("region"):
            lines.append(f"- Market/region: {brand_context['region']}")
        if brand_context.get("primary_goal"):
            lines.append(f"- Primary goal: {brand_context['primary_goal']}")
        brand_str = "\n".join(lines) if lines else "Brand profile is incomplete."
    return _AGENT_SYSTEM_PROMPT_TEMPLATE.format(brand_context=brand_str)


_AGENT_SYSTEM_PROMPT_STREAM_TEMPLATE = _AGENT_SYSTEM_PROMPT_TEMPLATE.replace(
    '{{"reply": "<your plain-text reply>", "navigate": "<section key or null>"}}',
    """Start your response with NAVIGATE:<key>| where <key> is the section to navigate to, or NAVIGATE:null| if no navigation is needed. Then write your plain-text reply immediately after the pipe — no newline between the prefix and the reply.

Examples:
NAVIGATE:billing|I'll take you to the Billing section where you can view plans and credits.
NAVIGATE:null|Here's how the Posting Schedule works: after generating content, posts land in Needs Review.
NAVIGATE:connections|To connect Instagram, head to Connected Accounts and follow the OAuth steps.

Do NOT include any text before the NAVIGATE: prefix. Do NOT add a newline after the pipe."""
)


def _build_agent_system_prompt_stream(brand_context: dict) -> str:
    """Build brand-aware system prompt for the streaming endpoint."""
    if not brand_context:
        brand_str = "No brand profile set up yet. Suggest the user completes their Brand Playbook."
    else:
        lines = []
        if brand_context.get("brand_name"):
            lines.append(f"- Brand name: {brand_context['brand_name']}")
        if brand_context.get("industry"):
            lines.append(f"- Industry: {brand_context['industry']}")
        if brand_context.get("business_description"):
            lines.append(f"- Business: {brand_context['business_description']}")
        if brand_context.get("brand_voice"):
            lines.append(f"- Brand voice: {brand_context['brand_voice']}")
        if brand_context.get("target_audience"):
            lines.append(f"- Target audience: {brand_context['target_audience']}")
        if brand_context.get("key_products_services"):
            lines.append(f"- Key products/services: {', '.join(brand_context['key_products_services'])}")
        brand_str = "\n".join(lines) if lines else "Brand profile is incomplete."
    return _AGENT_SYSTEM_PROMPT_STREAM_TEMPLATE.format(brand_context=brand_str)


class AgentChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class AgentChatRequest(BaseModel):
    messages: List[AgentChatMessage]  # full conversation history, latest message last
    image_url: Optional[str] = None   # Cloudinary URL of an attached image for vision


def _build_oai_messages(system_prompt: str, request: AgentChatRequest) -> list:
    """Build the OpenAI messages list, injecting an image_url into the last user turn when provided."""
    msgs = [{"role": "system", "content": system_prompt}]
    for i, m in enumerate(request.messages):
        is_last_user = i == len(request.messages) - 1 and m.role == "user"
        if is_last_user and request.image_url:
            msgs.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": m.content or "What do you see in this image?"},
                    {"type": "image_url", "image_url": {"url": request.image_url, "detail": "low"}},
                ],
            })
        else:
            msgs.append({"role": m.role, "content": m.content})
    return msgs


@router.post("/agent/chat/upload")
async def upload_chat_image(
    file: UploadFile = File(...),
    token: dict = Depends(JWTBearer()),
):
    """Upload an image for use in the agent chat. Returns a Cloudinary URL."""
    from app.utils.cloudinary_upload import upload_bytes

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    filename = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()
    allowed_types = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic"}
    allowed_exts = (".jpg", ".jpeg", ".png", ".webp", ".heic")

    if content_type not in allowed_types and not any(filename.endswith(e) for e in allowed_exts):
        return UriResponse.error_response("Only JPEG, PNG, WebP, and HEIC images are supported.")

    file_bytes = await file.read()
    size_mb = len(file_bytes) / (1024 * 1024)
    if size_mb > 10:
        return UriResponse.error_response(f"File too large ({size_mb:.1f}MB). Maximum is 10MB.")

    try:
        url = await upload_bytes(file_bytes, folder=f"uri-social/chat-images/{user_id}")
        return UriResponse.get_single_data_response("image_uploaded", {"url": url})
    except Exception as e:
        print(f"❌ chat image upload error: {e}")
        return UriResponse.error_response("Image upload failed. Please try again.")


@router.post("/agent/chat/stream")
async def agent_chat_stream(
    request: AgentChatRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Streaming version of agent chat — returns SSE tokens as they arrive."""
    from openai import AsyncOpenAI
    from app.core.config import settings
    from fastapi.responses import StreamingResponse as _StreamingResponse

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    brand_profile_doc = await db["brand_profiles"].find_one({"user_id": user_id})
    brand_ctx = BrandProfileService.to_brand_context(brand_profile_doc or {})
    stream_system_prompt = _build_agent_system_prompt_stream(brand_ctx)
    messages = _build_oai_messages(stream_system_prompt, request)

    async_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def generate():
        full_text = ""
        navigate = None
        nav_extracted = False
        nav_buffer = ""

        try:
            stream = await async_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.4,
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ""
                if not delta:
                    continue
                full_text += delta

                if not nav_extracted:
                    nav_buffer += delta
                    if "|" in nav_buffer:
                        prefix, rest = nav_buffer.split("|", 1)
                        nav_raw = prefix.replace("NAVIGATE:", "").strip()
                        navigate = None if nav_raw in ("null", "") else nav_raw
                        nav_extracted = True
                        if rest:
                            yield f"data: {json.dumps({'token': rest})}\n\n"
                else:
                    yield f"data: {json.dumps({'token': delta})}\n\n"

        except Exception as e:
            print(f"❌ agent_chat_stream error: {e}")
            yield f"data: {json.dumps({'error': 'Stream failed. Please try again.'})}\n\n"
            return

        # Extract just the reply text (strip the NAVIGATE: prefix)
        reply_text = full_text.split("|", 1)[1] if "|" in full_text else full_text

        # Persist to DB
        user_msg = request.messages[-1] if request.messages else None
        if user_msg and reply_text.strip():
            now = datetime.utcnow()
            try:
                await db["agent_chat_messages"].insert_many([
                    {"user_id": user_id, "role": "user", "content": user_msg.content, "created_at": now, "surface": "web"},
                    {"user_id": user_id, "role": "assistant", "content": reply_text.strip(), "created_at": now, "surface": "web"},
                ])
            except Exception:
                pass

        yield f"data: {json.dumps({'done': True, 'navigate': navigate})}\n\n"

    return _StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/agent/chat/history")
async def get_agent_chat_history(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Return the last 100 persisted messages for this user."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    messages = await db["agent_chat_messages"].find(
        {"user_id": user_id},
        {"_id": 0, "user_id": 0},
    ).sort("created_at", 1).to_list(length=100)

    return UriResponse.get_list_data_response("agent_chat_history", messages)


@router.delete("/agent/chat/history")
async def clear_agent_chat_history(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Archive (delete) the current conversation so the user starts fresh."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    await db["agent_chat_messages"].delete_many({"user_id": user_id})
    return UriResponse.get_single_data_response("cleared", {"cleared": True})


@router.post("/agent/chat")
async def agent_chat(
    request: AgentChatRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """URI Agent in-app assistant — answers questions and navigates the user."""
    from app.services.AIService import AIService
    from app.domain.models.chat_model import ChatMessage, ChatModel

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Fetch brand profile to personalise the system prompt
        brand_profile_doc = await db["brand_profiles"].find_one({"user_id": user_id})
        brand_context = BrandProfileService.to_brand_context(brand_profile_doc or {})
        system_prompt = _build_agent_system_prompt(brand_context)

        # Build message list — use raw dicts for vision, ChatMessage objects otherwise
        if request.image_url:
            from openai import AsyncOpenAI
            from app.core.config import settings
            request_with_prompt = type("R", (), {"messages": request.messages, "image_url": request.image_url})()
            oai_messages = _build_oai_messages(system_prompt, request_with_prompt)
            _ac = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            result = await _ac.chat.completions.create(
                model="gpt-4o-mini", messages=oai_messages, temperature=0.4
            )
        else:
            messages = [ChatMessage(role="system", content=system_prompt)]
            for m in request.messages:
                messages.append(ChatMessage(role=m.role, content=m.content))
            result = await AIService.chat_completion(ChatModel(model="gpt-4o-mini", messages=messages, temperature=0.4))

        if isinstance(result, dict) and "error" in result:
            return UriResponse.error_response(result["error"])

        raw = result.choices[0].message.content.strip()

        parsed = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            import re
            m = re.search(r'\{[\s\S]*?"reply"[\s\S]*?\}', raw)
            if m:
                try:
                    parsed = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        if parsed:
            reply = parsed.get("reply") or raw
            navigate = parsed.get("navigate") or None
        else:
            reply = raw
            navigate = None

        # Persist the latest user turn and the AI reply
        user_msg = request.messages[-1] if request.messages else None
        if user_msg:
            now = datetime.utcnow()
            await db["agent_chat_messages"].insert_many([
                {"user_id": user_id, "role": "user", "content": user_msg.content, "created_at": now, "surface": "web"},
                {"user_id": user_id, "role": "assistant", "content": reply, "created_at": now, "surface": "web"},
            ])

        return UriResponse.get_single_data_response("agent_chat", {
            "reply": reply,
            "navigate": navigate,
        })

    except Exception as e:
        print(f"❌ agent_chat error: {e}")
        traceback.print_exc()
        return UriResponse.error_response("Agent is unavailable right now. Please try again.")


# ─────────────────────────────────────────────────────────────────────────────
# Video-to-Video editing (PRD §3 — Level 1 FFmpeg pipeline)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/edit-video")
async def edit_video(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    platform: str = Form("instagram_reels"),
    enhancements: str = Form("{}"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Accept a raw video upload, run the Level 1 FFmpeg editing pipeline
    (crop 9:16, colour grade, trim, text overlays, export H.264),
    and save the result as a reel ContentDraft.
    Returns job_id immediately — poll GET /edit-video-job/{job_id} for status.
    """
    from app.agents.social_media_manager.services.video_edit_service import VideoEditService

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    ALLOWED_TYPES = {"video/mp4", "video/quicktime", "video/webm", "video/x-m4v", "video/x-matroska"}
    if video.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported format. Please upload MP4, MOV, or WebM.")

    video_bytes = await video.read()
    if len(video_bytes) > 200 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 200MB.")
    if len(video_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file received.")

    try:
        enhancements_dict = json.loads(enhancements)
    except Exception:
        enhancements_dict = {}

    # Load brand data for intro/outro/logo
    brand_name    = ""
    brand_cta     = ""
    brand_colors  = []
    logo_url      = ""
    logo_position = "bottom_right"
    tagline       = ""
    try:
        profile_result = await BrandProfileService.get(user_id, db)
        profile_data   = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        brand_name    = profile_data.get("brand_name", "")
        tagline       = profile_data.get("tagline", "")
        cta_styles    = profile_data.get("cta_styles") or []
        brand_cta     = cta_styles[0] if cta_styles else (profile_data.get("default_link") or "")
        brand_colors  = profile_data.get("brand_colors") or []
        logo_url      = profile_data.get("logo_url") or ""
        logo_position = profile_data.get("logo_position") or "bottom_right"
    except Exception:
        pass

    # Create job immediately and return — original upload happens inside run_job
    job_id = await VideoEditService.create_job(user_id, "", platform, enhancements_dict)

    background_tasks.add_task(
        VideoEditService.run_job,
        job_id,
        user_id,
        video_bytes,
        platform,
        enhancements_dict,
        brand_name,
        brand_cta,
        brand_colors,
        logo_url,
        logo_position,
        tagline,
    )

    return UriResponse.get_single_data_response("edit_video", {"job_id": job_id, "status": "processing"})


@router.get("/edit-video-job/{job_id}")
async def get_edit_video_job(
    job_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Poll the status of a video editing job."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    job = await db["video_edit_jobs"].find_one(
        {"job_id": job_id, "user_id": user_id},
        {"_id": 0},
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return UriResponse.get_single_data_response("edit_video_job", job)


# ─────────────────────────────────────────────────────────────────────────────
# Video Polish — Clipping-API-first pipeline (Video Polish PRD)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/video-polish-styles")
async def list_video_polish_styles(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Return all available style presets for Video Polish."""
    from app.agents.social_media_manager.services.video_polish_service import VideoPolishService
    styles = await VideoPolishService.list_styles(db)
    return UriResponse.get_single_data_response("video_polish_styles", styles)


@router.get("/video-polish-caption-presets")
async def list_caption_presets(
    token: dict = Depends(JWTBearer()),
):
    """Return all Reap caption style presets (system + user-created)."""
    from app.agents.social_media_manager.services.video_polish_service import VideoPolishService
    presets = await VideoPolishService.list_caption_presets()
    return UriResponse.get_single_data_response("caption_presets", presets)


@router.post("/polish-video")
async def polish_video(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    style_preset: str = Form("clean_professional"),
    language: str = Form("en-NG"),
    captions_preset: str = Form("system_beasty"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Accept a raw video upload, run the Video Polish clipping-API pipeline
    (ingest + quality check + Reap), and return a job_id immediately.
    Poll GET /polish-video-job/{job_id} for status and output clips.
    """
    print(f"[PolishVideo] handler entered — content_type={video.content_type} filename={video.filename}", flush=True)
    from app.agents.social_media_manager.services.video_polish_service import VideoPolishService
    from app.core.config import settings

    if not settings.REAP_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Video Polish is not yet configured. Please add REAP_API_KEY to the server environment."
        )

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    ALLOWED_TYPES = {
        "video/mp4", "video/quicktime", "video/webm",
        "video/x-m4v", "video/x-matroska", "video/3gpp",
    }
    if video.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported format. Please upload MP4 or MOV."
        )

    video_bytes = await video.read()
    if len(video_bytes) > 500 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum is 500MB.")
    if len(video_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file received.")

    job_id = await VideoPolishService.create_job(user_id, style_preset, language, db)

    background_tasks.add_task(
        VideoPolishService.run_job,
        job_id,
        user_id,
        video_bytes,
        style_preset,
        language,
        db,
        captions_preset,
    )

    return UriResponse.get_single_data_response(
        "polish_video", {"job_id": job_id, "status": "ingesting"}
    )


@router.get("/polish-video-job/{job_id}")
async def get_polish_video_job(
    job_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Poll the status of a Video Polish job."""
    from app.agents.social_media_manager.services.video_polish_service import VideoPolishService

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    job = await VideoPolishService.get_job(job_id, user_id, db)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return UriResponse.get_single_data_response("polish_video_job", job)


@router.post("/polish-video-restyle")
async def restyle_polish_video(
    background_tasks: BackgroundTasks,
    original_job_id: str = Form(...),
    new_style_preset: str = Form(...),
    language: str = Form("en-NG"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Re-polish an already-processed video with a different style preset.
    Costs 0.5 credits (PRD §8.2). Uses the same source video — no re-upload.
    """
    from app.agents.social_media_manager.services.video_polish_service import VideoPolishService

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    original = await VideoPolishService.get_job(original_job_id, user_id, db)
    if not original:
        raise HTTPException(status_code=404, detail="Original job not found")
    if original["status"] != "ready":
        raise HTTPException(status_code=400, detail="Original job is not ready yet")

    new_job_id = await VideoPolishService.restyle_job(
        original_job_id, user_id, new_style_preset, language, db
    )

    background_tasks.add_task(
        VideoPolishService.run_restyle_job,
        new_job_id,
        user_id,
        original["source_video_url"],
        new_style_preset,
        language,
        db,
    )

    return UriResponse.get_single_data_response(
        "polish_video_restyle", {"job_id": new_job_id, "status": "processing"}
    )


@router.post("/polish-video-clip-action")
async def polish_video_clip_action(
    background_tasks: BackgroundTasks,
    job_id: str = Form(...),
    clip_idx: int = Form(...),
    action: str = Form(...),        # "reframe" | "dub"
    orientation: str = Form("landscape"),
    source_language: str = Form("en"),
    target_language: str = Form("es"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Trigger a secondary action (reframe or dub) on a specific clip.
    Long-running — returns an action_job_id to poll via GET /polish-video-clip-action/{id}.
    """
    from app.agents.social_media_manager.services.video_polish_service import VideoPolishService
    import uuid as _uuid

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if action not in ("reframe", "dub"):
        raise HTTPException(status_code=400, detail="action must be 'reframe' or 'dub'")

    action_job_id = str(_uuid.uuid4())
    params = {"orientation": orientation, "source_language": source_language, "target_language": target_language}

    async def _run():
        try:
            result = await VideoPolishService.clip_action(job_id, clip_idx, action, params, user_id, db)
            await db["clip_action_jobs"].update_one(
                {"action_job_id": action_job_id},
                {"$set": {**result, "action_job_id": action_job_id}},
                upsert=True,
            )
        except Exception as exc:
            await db["clip_action_jobs"].update_one(
                {"action_job_id": action_job_id},
                {"$set": {"status": "failed", "error": str(exc), "action_job_id": action_job_id}},
                upsert=True,
            )

    await db["clip_action_jobs"].insert_one({"action_job_id": action_job_id, "status": "processing"})
    background_tasks.add_task(_run)

    return UriResponse.get_single_data_response(
        "clip_action", {"action_job_id": action_job_id, "status": "processing"}
    )


@router.get("/polish-video-clip-action/{action_job_id}")
async def get_polish_video_clip_action(
    action_job_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Poll a clip action job (reframe / dub) by its action_job_id."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    doc = await db["clip_action_jobs"].find_one({"action_job_id": action_job_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Action job not found")

    return UriResponse.get_single_data_response("clip_action", doc)


# ── Video Production (Phase 1 composed pipeline) ──────────────────────────────

@router.post("/produce-video")
async def produce_video(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    video_type: str = Form("founder"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Start a full video production job.
    video_type: tiktok | product | founder
    Poll GET /produce-video-job/{job_id} for status and output_url.
    """
    import uuid
    from app.agents.social_media_manager.services.video_production_service import run_production_job

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    video_bytes = await video.read()
    if len(video_bytes) < 1000:
        raise HTTPException(status_code=400, detail="Invalid video file")

    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    await db.video_production_jobs.insert_one({
        "job_id": job_id,
        "user_id": user_id,
        "video_type": video_type,
        "status": "processing",
        "status_message": "Starting…",
        "progress": 0,
        "output_url": None,
        "render_id": None,
        "cuts": [],
        "zooms": [],
        "pacing_note": "",
        "srt": "",
        "created_at": now,
        "completed_at": None,
    })

    background_tasks.add_task(run_production_job, job_id, video_bytes, video_type, db)

    return UriResponse.get_single_data_response(
        "produce_video", {"job_id": job_id, "status": "processing"}
    )


@router.get("/produce-video-job/{job_id}")
async def get_produce_video_job(
    job_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Poll a video production job."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    doc = await db.video_production_jobs.find_one(
        {"job_id": job_id, "user_id": user_id}, {"_id": 0}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Job not found")

    return UriResponse.get_single_data_response("produce_video_job", doc)
