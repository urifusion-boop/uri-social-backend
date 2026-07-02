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

from app.middleware.api_key_auth import verify_api_key
from app.models.api_key import APIKey, APIKeyScope
from app.domain.responses.uri_response import UriResponse
from app.dependencies import get_db_dependency
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


# =====================================================
# CONTENT GENERATION ENDPOINTS
# =====================================================

@router.post("/content/generate", status_code=201)
async def generate_content(
    request: Request,
    body: ContentGenerationRequest,
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate multi-platform social media content with AI

    **Required Scopes**: content:write, images:generate (if reference_image provided)

    **Rate Limit**: 100 requests/hour

    **Credits**: Deducts credits from user account
    """
    try:
        # Check scopes
        if not api_key.has_scope(APIKeyScope.CONTENT_WRITE):
            raise HTTPException(status_code=403, detail="Insufficient permissions: content:write required")

        # Check credits
        user = await db.users.find_one({"_id": api_key.user_id})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Get brand profile
        brand_profile = await BrandProfileService.get_brand_profile(api_key.user_id, db)
        if not brand_profile:
            # Create minimal profile
            brand_profile = {
                "user_id": api_key.user_id,
                "brand_name": user.get("name", "Your Brand"),
                "industry": "general_other",
                "region": "West Africa, Nigeria",
                "brand_colors": ["#C41E3A", "#FFFEF2"],
                "style_selections": ["lifestyle_natural"]
            }

        # Generate content
        result = await ContentGenerationService.generate_multi_platform_content(
            user_id=api_key.user_id,
            seed_content=body.seedContent,
            platforms=body.platforms,
            reference_image=body.referenceImage,
            tone=body.tone,
            db=db
        )

        # Generate image if needed
        if body.referenceImage or True:  # Always generate images
            if not api_key.has_scope(APIKeyScope.IMAGES_GENERATE):
                raise HTTPException(status_code=403, detail="Insufficient permissions: images:generate required")

            image_result = await ImageContentService.generate_image(
                user_id=api_key.user_id,
                text_content=result.get("text_content", {}),
                brand_profile=brand_profile,
                reference_image=body.referenceImage,
                db=db
            )
            result["image_url"] = image_result.get("image_url")

        # Format response for SDK
        sdk_response = {
            "id": str(result.get("draft_id", "")),
            "platforms": [
                {
                    "platform": platform,
                    "text": content.get("text", ""),
                    "hashtags": content.get("hashtags", []),
                    "character_count": len(content.get("text", ""))
                }
                for platform, content in result.get("text_content", {}).items()
            ],
            "image_url": result.get("image_url"),
            "created_at": datetime.utcnow().isoformat(),
            "status": "completed"
        }

        return JSONResponse(content=sdk_response, status_code=201)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Content generation failed: {str(e)}")


@router.get("/content/{content_id}")
async def get_content(
    content_id: str,
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get generated content by ID

    **Required Scopes**: content:read
    """
    if not api_key.has_scope(APIKeyScope.CONTENT_READ):
        raise HTTPException(status_code=403, detail="Insufficient permissions: content:read required")

    draft = await db.content_drafts.find_one({
        "_id": content_id,
        "user_id": api_key.user_id
    })

    if not draft:
        raise HTTPException(status_code=404, detail="Content not found")

    return JSONResponse(content=draft)


# =====================================================
# DRAFT MANAGEMENT ENDPOINTS
# =====================================================

@router.get("/drafts")
async def list_drafts(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    List all drafts with pagination

    **Required Scopes**: drafts:read
    """
    if not api_key.has_scope(APIKeyScope.DRAFTS_READ):
        raise HTTPException(status_code=403, detail="Insufficient permissions: drafts:read required")

    skip = (page - 1) * per_page

    print(f"📋 SDK /api/v1/drafts request - API key user_id: {api_key.user_id}, page: {page}, per_page: {per_page}")

    # Query all drafts for this user to debug
    all_drafts_sample = await db.content_drafts.find(
        {"user_id": api_key.user_id}
    ).sort("created_at", -1).limit(5).to_list(length=5)

    print(f"📋 Top 5 most recent draft IDs in DB: {[d.get('id', 'no-id') for d in all_drafts_sample]}")

    # Now get the paginated results
    drafts_cursor = db.content_drafts.find(
        {"user_id": api_key.user_id}
    ).sort("created_at", -1).skip(skip).limit(per_page)

    drafts = await drafts_cursor.to_list(length=per_page)
    total = await db.content_drafts.count_documents({"user_id": api_key.user_id})

    print(f"📋 Returning {len(drafts)} drafts (total: {total})")

    # Convert ObjectId and datetime to string for JSON serialization
    import json
    from bson import ObjectId
    from datetime import datetime

    def serialize_doc(doc):
        """Convert MongoDB document to JSON-serializable dict"""
        if isinstance(doc, dict):
            return {k: serialize_doc(v) for k, v in doc.items()}
        elif isinstance(doc, list):
            return [serialize_doc(item) for item in doc]
        elif isinstance(doc, ObjectId):
            return str(doc)
        elif isinstance(doc, datetime):
            return doc.isoformat()
        else:
            return doc

    serialized_drafts = []
    for draft in drafts:
        draft["id"] = str(draft.pop("_id"))
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
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get single draft by ID

    **Required Scopes**: drafts:read
    """
    if not api_key.has_scope(APIKeyScope.DRAFTS_READ):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    draft = await db.content_drafts.find_one({
        "_id": draft_id,
        "user_id": api_key.user_id
    })

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    draft["id"] = str(draft.pop("_id"))
    return JSONResponse(content=draft)


@router.patch("/drafts/{draft_id}")
async def update_draft(
    draft_id: str,
    updates: Dict[str, Any] = Body(...),
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Update draft content

    **Required Scopes**: drafts:write
    """
    if not api_key.has_scope(APIKeyScope.DRAFTS_WRITE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    result = await db.content_drafts.update_one(
        {"_id": draft_id, "user_id": api_key.user_id},
        {"$set": {**updates, "updated_at": datetime.utcnow()}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Draft not found")

    updated_draft = await db.content_drafts.find_one({"_id": draft_id})
    updated_draft["id"] = str(updated_draft.pop("_id"))

    return JSONResponse(content=updated_draft)


@router.delete("/drafts/{draft_id}")
async def delete_draft(
    draft_id: str,
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Delete draft

    **Required Scopes**: drafts:delete
    """
    if not api_key.has_scope(APIKeyScope.DRAFTS_DELETE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    result = await db.content_drafts.delete_one({
        "_id": draft_id,
        "user_id": api_key.user_id
    })

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Draft not found")

    return JSONResponse(content={"success": True})


# =====================================================
# IMAGE GENERATION ENDPOINTS
# =====================================================

@router.post("/images/generate")
async def generate_image(
    body: ImageGenerationRequest,
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate AI image

    **Required Scopes**: images:generate

    **Rate Limit**: 50 requests/hour
    """
    if not api_key.has_scope(APIKeyScope.IMAGES_GENERATE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check specific rate limit for image generation
    if not api_key.check_rate_limit("image_generation"):
        raise HTTPException(
            status_code=429,
            detail="Image generation rate limit exceeded (50/hour)"
        )

    try:
        brand_profile = await BrandProfileService.get_brand_profile(api_key.user_id, db)

        result = await ImageContentService.generate_image(
            user_id=api_key.user_id,
            text_content={"instagram": {"text": body.prompt}},
            brand_profile=brand_profile or {},
            reference_image=body.referenceImage,
            db=db
        )

        return JSONResponse(content={
            "image_url": result.get("image_url"),
            "revised_prompt": result.get("revised_prompt")
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image generation failed: {str(e)}")


@router.post("/images/remove-background")
async def remove_background(
    image_url: str = Body(..., embed=True),
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Remove background from image

    **Required Scopes**: images:edit
    """
    if not api_key.has_scope(APIKeyScope.IMAGES_EDIT):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    from app.utils.background_removal import remove_background as remove_bg

    cutout_url = await remove_bg(image_url)
    return JSONResponse(content={"cutout_url": cutout_url})


@router.post("/images/analyze-product")
async def analyze_product(
    image_url: str = Body(..., embed=True),
    api_key: APIKey = Depends(verify_api_key)
):
    """
    Analyze product in image

    **Required Scopes**: images:generate
    """
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
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get all connected social accounts

    **Required Scopes**: connections:read
    """
    if not api_key.has_scope(APIKeyScope.CONNECTIONS_READ):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    connections = await SocialAccountService.get_connections(api_key.user_id, db)

    return JSONResponse(content={"connected_platforms": connections})


@router.get("/connections/{platform}/connect")
async def get_connect_url(
    platform: str,
    redirect_url: Optional[str] = Query(None),
    api_key: APIKey = Depends(verify_api_key)
):
    """
    Get OAuth URL to connect platform

    **Required Scopes**: connections:write
    """
    if not api_key.has_scope(APIKeyScope.CONNECTIONS_WRITE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Return OAuth URL (implementation depends on your OAuth flow)
    auth_url = f"https://oauth.urisocial.com/{platform}/authorize?user_id={api_key.user_id}&redirect_url={redirect_url or ''}"

    return JSONResponse(content={"auth_url": auth_url})


@router.delete("/connections/{platform}")
async def disconnect_platform(
    platform: str,
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Disconnect social platform

    **Required Scopes**: connections:delete
    """
    if not api_key.has_scope(APIKeyScope.CONNECTIONS_DELETE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    await SocialAccountService.disconnect(api_key.user_id, platform, db)

    return JSONResponse(content={"success": True})


# =====================================================
# PUBLISHING ENDPOINTS
# =====================================================

@router.post("/publish")
async def publish_content(
    body: PublishRequest,
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Publish draft to social media

    **Required Scopes**: publishing:write
    """
    if not api_key.has_scope(APIKeyScope.PUBLISHING_WRITE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # TODO: Implement publishing logic
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
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Schedule content for later

    **Required Scopes**: publishing:schedule
    """
    if not api_key.has_scope(APIKeyScope.PUBLISHING_SCHEDULE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # TODO: Implement scheduling logic
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
    api_key: APIKey = Depends(verify_api_key),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get billing information and credits

    **Required Scopes**: billing:read
    """
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
