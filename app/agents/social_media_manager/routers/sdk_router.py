"""
SDK Router - /api/v1/* endpoints

Enterprise-grade REST API for URI Social SDK.
Maps SDK-friendly routes to internal service handlers with API key authentication.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Query, Body
from fastapi.responses import JSONResponse
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime

from app.models.api_key import APIKeyScope
from app.domain.responses.uri_response import UriResponse
from app.dependencies import get_db_dependency, get_sdk_context
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..services.content_generation_service import ContentGenerationService
from ..services.image_content_service import ImageContentService
from ..services.social_account_service import SocialAccountService
from ..services.brand_profile_service import BrandProfileService

router = APIRouter(prefix="/api/v1", tags=["SDK"])


# =====================================================
# REQUEST/RESPONSE MODELS
# =====================================================

class ContentGenerationRequest(BaseModel):
    """SDK request for content generation"""
    seedContent: str = Field(..., min_length=1, description="Description of content to generate", alias="seed_content")
    platforms: List[str] = Field(..., min_items=1, description="Target platforms")
    referenceImage: Optional[str] = Field(None, description="Product/reference image URL", alias="reference_image")
    tone: Optional[str] = Field("professional", description="Content tone")
    includeHashtags: Optional[bool] = Field(True, alias="include_hashtags")
    includeEmojis: Optional[bool] = Field(True, alias="include_emojis")

    class Config:
        populate_by_name = True


class ImageGenerationRequest(BaseModel):
    """SDK request for image generation"""
    prompt: str = Field(..., min_length=1)
    referenceImage: Optional[str] = Field(None, alias="reference_image")
    style: Optional[str] = Field("immersive", pattern="^(immersive|standard|minimalist)$")
    aspectRatio: Optional[str] = Field("1:1", alias="aspect_ratio", pattern="^(1:1|4:5|16:9)$")

    class Config:
        populate_by_name = True


class PublishRequest(BaseModel):
    """SDK request for publishing"""
    draftId: str = Field(..., alias="draft_id")
    platforms: List[str] = Field(..., min_items=1)

    class Config:
        populate_by_name = True


class ScheduleRequest(BaseModel):
    """SDK request for scheduling"""
    draftId: str = Field(..., alias="draft_id")
    platforms: List[str] = Field(..., min_items=1)
    scheduleTime: str = Field(..., alias="schedule_time", description="ISO 8601 datetime")

    class Config:
        populate_by_name = True


async def _get_brand_profile_dict(user_id: str, brand_id: str, db: AsyncIOMotorDatabase) -> Dict[str, Any]:
    """
    Shared helper: fetch the brand profile for this (developer-or-end-user-scoped)
    brand_id, falling back to a minimal profile if none exists yet — mirrors the
    fallback the /content/generate endpoint always had, just brand_id-scoped now
    instead of always reading the developer's own profile regardless of caller.
    """
    profile_result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
    profile = profile_result.get("responseData") if profile_result.get("status") else None
    if profile:
        return profile
    user = await db.users.find_one({"_id": user_id})
    return {
        "user_id": user_id,
        "brand_id": brand_id,
        "brand_name": (user or {}).get("name", "Your Brand"),
        "industry": "general_other",
        "region": "West Africa, Nigeria",
        "brand_colors": ["#C41E3A", "#FFFEF2"],
        "style_selections": ["lifestyle_natural"],
    }


# =====================================================
# CONTENT GENERATION ENDPOINTS
# =====================================================

@router.post("/content/generate", status_code=201)
async def generate_content(
    request: Request,
    body: ContentGenerationRequest,
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate multi-platform social media content with AI

    **Required Scopes**: content:write, images:generate (if reference_image provided)

    **Rate Limit**: 100 requests/hour

    **Credits**: Deducts credits from user account
    """
    api_key = ctx["api_key_obj"]
    brand_id = ctx["brand_id"]

    try:
        if not api_key.has_scope(APIKeyScope.CONTENT_WRITE):
            raise HTTPException(status_code=403, detail="Insufficient permissions: content:write required")
        if not api_key.has_scope(APIKeyScope.IMAGES_GENERATE):
            raise HTTPException(status_code=403, detail="Insufficient permissions: images:generate required")

        user = await db.users.find_one({"_id": api_key.user_id})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        brand_profile = await _get_brand_profile_dict(api_key.user_id, brand_id, db)
        brand_context = {**brand_profile, "brand_id": brand_id}

        # Single call generates text + images together and persists drafts
        # (content_drafts, keyed by their own "id" field, not Mongo _id) —
        # same real pipeline the main app's /generate-content uses, rather
        # than the previous two-step call into a generate_image() method
        # that never existed on ImageContentService.
        result = await ImageContentService.generate_content_with_images(
            user_id=api_key.user_id,
            seed_content=body.seedContent,
            platforms=body.platforms,
            include_images=True,
            brand_context=brand_context,
            db=db,
            reference_image=body.referenceImage,
        )

        if not result.get("status"):
            raise HTTPException(status_code=500, detail=result.get("responseMessage", "Content generation failed"))

        response_data = result.get("responseData", {})
        drafts = response_data.get("drafts", [])

        # Stamp brand_id on the request + every draft, same pattern used by
        # /social-media/generate-content — this is the isolation boundary
        # drafts/content are later fetched by, not just the auth layer.
        request_id = response_data.get("request_id")
        draft_ids = [d["id"] for d in drafts if d.get("id")]
        if request_id:
            await db["content_requests"].update_one({"id": request_id}, {"$set": {"brand_id": brand_id}})
        if draft_ids:
            await db["content_drafts"].update_many({"id": {"$in": draft_ids}}, {"$set": {"brand_id": brand_id}})
        for d in drafts:
            d["brand_id"] = brand_id

        sdk_response = {
            "id": draft_ids[0] if draft_ids else "",
            "request_id": request_id,
            "platforms": [
                {
                    "platform": d.get("platform"),
                    "text": d.get("content", ""),
                    "hashtags": d.get("hashtags", []),
                    "character_count": len(d.get("content", "")),
                    "image_url": d.get("image_url"),
                }
                for d in drafts
            ],
            "created_at": datetime.utcnow().isoformat(),
            "status": "completed",
        }

        return JSONResponse(content=sdk_response, status_code=201)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Content generation failed: {str(e)}")


@router.get("/content/{content_id}")
async def get_content(
    content_id: str,
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get generated content by ID

    **Required Scopes**: content:read
    """
    api_key = ctx["api_key_obj"]
    brand_id = ctx["brand_id"]

    if not api_key.has_scope(APIKeyScope.CONTENT_READ):
        raise HTTPException(status_code=403, detail="Insufficient permissions: content:read required")

    # content_drafts documents are keyed by their own "id" string field, not
    # Mongo's auto _id (every real writer — approval_workflow_service,
    # carousel_generation_service, etc. — sets "id" explicitly and lets Mongo
    # assign an unrelated _id). Querying by _id here never matched a real draft.
    draft = await db.content_drafts.find_one({"id": content_id, "brand_id": brand_id})

    if not draft:
        raise HTTPException(status_code=404, detail="Content not found")

    draft.pop("_id", None)
    return JSONResponse(content=draft)


# =====================================================
# DRAFT MANAGEMENT ENDPOINTS
# =====================================================

@router.get("/drafts")
async def list_drafts(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    List all drafts with pagination

    **Required Scopes**: drafts:read
    """
    api_key = ctx["api_key_obj"]
    brand_id = ctx["brand_id"]

    if not api_key.has_scope(APIKeyScope.DRAFTS_READ):
        raise HTTPException(status_code=403, detail="Insufficient permissions: drafts:read required")

    skip = (page - 1) * per_page
    query = {"brand_id": brand_id}

    drafts_cursor = db.content_drafts.find(query).sort("created_at", -1).skip(skip).limit(per_page)
    drafts = await drafts_cursor.to_list(length=per_page)
    total = await db.content_drafts.count_documents(query)

    from bson import ObjectId as _ObjectId

    def serialize_doc(doc):
        """Convert MongoDB document to JSON-serializable dict"""
        if isinstance(doc, dict):
            return {k: serialize_doc(v) for k, v in doc.items()}
        elif isinstance(doc, list):
            return [serialize_doc(item) for item in doc]
        elif isinstance(doc, _ObjectId):
            return str(doc)
        elif isinstance(doc, datetime):
            return doc.isoformat()
        else:
            return doc

    serialized_drafts = []
    for draft in drafts:
        # Drafts already carry their own "id" field from creation — don't
        # overwrite it with the unrelated Mongo _id, just drop _id.
        draft.pop("_id", None)
        serialized_drafts.append(serialize_doc(draft))

    return JSONResponse(content={
        "data": serialized_drafts,
        "total": total,
        "page": page,
        "per_page": per_page,
        "has_more": (skip + len(drafts)) < total
    })


@router.get("/drafts/{draft_id}")
async def get_draft(
    draft_id: str,
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get single draft by ID

    **Required Scopes**: drafts:read
    """
    api_key = ctx["api_key_obj"]
    brand_id = ctx["brand_id"]

    if not api_key.has_scope(APIKeyScope.DRAFTS_READ):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    draft = await db.content_drafts.find_one({"id": draft_id, "brand_id": brand_id})

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    draft.pop("_id", None)
    return JSONResponse(content=draft)


@router.patch("/drafts/{draft_id}")
async def update_draft(
    draft_id: str,
    updates: Dict[str, Any] = Body(...),
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Update draft content

    **Required Scopes**: drafts:write
    """
    api_key = ctx["api_key_obj"]
    brand_id = ctx["brand_id"]

    if not api_key.has_scope(APIKeyScope.DRAFTS_WRITE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    result = await db.content_drafts.update_one(
        {"id": draft_id, "brand_id": brand_id},
        {"$set": {**updates, "updated_at": datetime.utcnow()}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Draft not found")

    updated_draft = await db.content_drafts.find_one({"id": draft_id})
    updated_draft.pop("_id", None)

    return JSONResponse(content=updated_draft)


@router.delete("/drafts/{draft_id}")
async def delete_draft(
    draft_id: str,
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Delete draft

    **Required Scopes**: drafts:delete
    """
    api_key = ctx["api_key_obj"]
    brand_id = ctx["brand_id"]

    if not api_key.has_scope(APIKeyScope.DRAFTS_DELETE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    result = await db.content_drafts.delete_one({"id": draft_id, "brand_id": brand_id})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Draft not found")

    return JSONResponse(content={"success": True})


# =====================================================
# IMAGE GENERATION ENDPOINTS
# =====================================================

@router.post("/images/generate")
async def generate_image(
    body: ImageGenerationRequest,
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate AI image

    **Required Scopes**: images:generate

    **Rate Limit**: 50 requests/hour
    """
    api_key = ctx["api_key_obj"]
    brand_id = ctx["brand_id"]

    if not api_key.has_scope(APIKeyScope.IMAGES_GENERATE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    if not api_key.check_rate_limit("image_generation"):
        raise HTTPException(
            status_code=429,
            detail="Image generation rate limit exceeded (50/hour)"
        )

    try:
        brand_profile = await _get_brand_profile_dict(api_key.user_id, brand_id, db)

        # Standalone image generation (no draft/platform-content context) —
        # ImageContentService has no generate_image() method; this is the real
        # single-image primitive the rest of the app's generation paths call.
        image_result = await ImageContentService._generate_platform_image(
            platform="instagram",
            content=body.prompt,
            seed_content=body.prompt,
            brand_context=brand_profile,
            reference_image=body.referenceImage,
        )

        if not image_result.get("status"):
            raise HTTPException(status_code=500, detail=image_result.get("responseMessage", "Image generation failed"))

        image_data = image_result.get("responseData", {})
        raw_image_url = image_data.get("image_url")

        stored_url = raw_image_url
        if raw_image_url and raw_image_url.startswith("data:"):
            from app.utils.cloudinary_upload import upload_base64
            stored_url = await upload_base64(raw_image_url, folder="uri-social/sdk-images")

        return JSONResponse(content={
            "image_url": stored_url,
            "specs": image_data.get("specs"),
        })

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image generation failed: {str(e)}")


@router.post("/images/remove-background")
async def remove_background(
    image_url: str = Body(..., embed=True),
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Remove background from image

    **Required Scopes**: images:edit
    """
    api_key = ctx["api_key_obj"]

    if not api_key.has_scope(APIKeyScope.IMAGES_EDIT):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    from app.utils.background_removal import remove_background as remove_bg

    cutout_url = await remove_bg(image_url)
    return JSONResponse(content={"cutout_url": cutout_url})


@router.post("/images/analyze-product")
async def analyze_product(
    image_url: str = Body(..., embed=True),
    ctx: dict = Depends(get_sdk_context)
):
    """
    Analyze product in image

    **Required Scopes**: images:generate
    """
    api_key = ctx["api_key_obj"]

    if not api_key.has_scope(APIKeyScope.IMAGES_GENERATE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    from app.agents.social_media_manager.services.product_analysis_service import ProductAnalysisService

    analysis = await ProductAnalysisService.analyze_product_forensically(image_url)
    return JSONResponse(content=analysis)


# =====================================================
# CONNECTIONS ENDPOINTS
# =====================================================

@router.get("/connections")
async def list_connections(
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get all connected social accounts

    **Required Scopes**: connections:read
    """
    api_key = ctx["api_key_obj"]
    brand_id = ctx["brand_id"]

    if not api_key.has_scope(APIKeyScope.CONNECTIONS_READ):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    result = await SocialAccountService.get_user_connections(db, api_key.user_id, brand_id=brand_id)
    data = result.get("responseData", {}) if result.get("status") else {}

    return JSONResponse(content={"connected_platforms": data.get("connected_platforms", [])})


@router.get("/connections/{platform}/connect")
async def get_connect_url(
    platform: str,
    redirect_url: Optional[str] = Query(None),
    ctx: dict = Depends(get_sdk_context)
):
    """
    Get OAuth URL to connect platform

    **Required Scopes**: connections:write
    """
    api_key = ctx["api_key_obj"]

    if not api_key.has_scope(APIKeyScope.CONNECTIONS_WRITE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Return OAuth URL (implementation depends on your OAuth flow)
    auth_url = f"https://oauth.urisocial.com/{platform}/authorize?user_id={api_key.user_id}&redirect_url={redirect_url or ''}"

    return JSONResponse(content={"auth_url": auth_url})


@router.delete("/connections/{platform}")
async def disconnect_platform(
    platform: str,
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Disconnect social platform

    **Required Scopes**: connections:delete
    """
    api_key = ctx["api_key_obj"]
    brand_id = ctx["brand_id"]

    if not api_key.has_scope(APIKeyScope.CONNECTIONS_DELETE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # disconnect_account() takes Outstand's own account id, not a bare platform
    # name — look the connected account up first to get it.
    connections_result = await SocialAccountService.get_user_connections(db, api_key.user_id, brand_id=brand_id)
    connections = connections_result.get("responseData", {}).get("connections", {}) if connections_result.get("status") else {}
    platform_accounts = connections.get(platform, [])

    if not platform_accounts:
        raise HTTPException(status_code=404, detail=f"No connected {platform} account found")

    outstand_account_id = platform_accounts[0].get("outstand_account_id")
    if not outstand_account_id:
        raise HTTPException(status_code=404, detail=f"No disconnectable {platform} account found")

    await SocialAccountService.disconnect_account(db, api_key.user_id, outstand_account_id, brand_id=brand_id)

    return JSONResponse(content={"success": True})


# =====================================================
# PUBLISHING ENDPOINTS
# =====================================================

@router.post("/publish")
async def publish_content(
    body: PublishRequest,
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Publish draft to social media

    **Required Scopes**: publishing:write
    """
    api_key = ctx["api_key_obj"]

    if not api_key.has_scope(APIKeyScope.PUBLISHING_WRITE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # TODO: Implement publishing logic — thread ctx["brand_id"] through once
    # this calls the real posting pipeline, so it stays isolated per end-user.
    results = []
    for platform in body.platforms:
        results.append({
            "platform": platform,
            "status": "success",
            "post_id": f"post_{platform}_123"
        })

    return JSONResponse(content={
        "results": results,
        "overall_status": "success"
    })


@router.post("/publish/schedule")
async def schedule_content(
    body: ScheduleRequest,
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Schedule content for later

    **Required Scopes**: publishing:schedule
    """
    api_key = ctx["api_key_obj"]

    if not api_key.has_scope(APIKeyScope.PUBLISHING_SCHEDULE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # TODO: Implement scheduling logic — thread ctx["brand_id"] through once
    # this calls the real scheduling pipeline, so it stays isolated per end-user.
    scheduled_id = f"scheduled_{api_key.user_id}_{datetime.utcnow().timestamp()}"

    return JSONResponse(content={
        "scheduled_id": scheduled_id,
        "scheduled_for": body.scheduleTime
    })


# =====================================================
# BILLING ENDPOINTS
# =====================================================

@router.get("/billing/info")
async def get_billing_info(
    ctx: dict = Depends(get_sdk_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get billing information and credits

    **Required Scopes**: billing:read

    Deliberately keyed on the developer's own user_id, not the resolved
    brand/end-user — credits are developer-level (SDKClientProfile's
    shared_credits_with_developer), billing must never be per-end-user.
    """
    api_key = ctx["api_key_obj"]

    if not api_key.has_scope(APIKeyScope.BILLING_READ):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    user = await db.users.find_one({"_id": api_key.user_id})

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return JSONResponse(content={
        "credits_remaining": user.get("credits", 0),
        "subscription_tier": user.get("subscription_tier", "free"),
        "billing_cycle_end": user.get("billing_cycle_end", datetime.utcnow().isoformat())
    })
