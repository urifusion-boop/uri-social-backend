# app/agents/social_media_manager/routers/custom_visual_guides_v2.py

"""
Custom Visual Guides V2 API Router
Advanced style transfer using meta-prompts and direct image references.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime
from bson import ObjectId

from app.dependencies import get_db_dependency
from app.core.auth_bearer import JWTBearer
from app.domain.responses.uri_response import UriResponse

from ..services.custom_visual_guide_v2_service import CustomVisualGuideV2Service

router = APIRouter(tags=["Custom Visual Guides V2"])


def _get_user_id(token: dict) -> str | None:
    """Extract user_id from JWT payload"""
    if not isinstance(token, dict):
        return None

    # flat keys
    for k in ("user_id", "userId", "id", "sub"):
        v = token.get(k)
        if v:
            return str(v)

    # nested claims
    claims = token.get("claims") or {}
    if isinstance(claims, dict):
        for k in ("userId", "user_id", "id", "sub"):
            v = claims.get(k)
            if v:
                return str(v)

    return None


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class UploadReferenceImageV2Request(BaseModel):
    image_url: str = Field(..., description="Cloudinary URL of uploaded reference image")
    name: str = Field(..., min_length=1, max_length=100, description="Guide name")
    brand_id: Optional[str] = Field(None, description="Brand ID (optional)")


class GenerateWithV2GuideRequest(BaseModel):
    guide_id: str = Field(..., description="Custom Visual Guide V2 ID")
    seed_content: str = Field(..., description="Content request (e.g., 'Happy Wednesday post')")
    headline: str = Field(..., description="Main headline text")
    subtext: Optional[str] = Field(None, description="Supporting text")
    cta: Optional[str] = Field(None, description="Call to action")
    platform: str = Field("instagram", description="Target platform")


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/social-media/custom-guides-v2/upload")
async def upload_reference_image_v2(
    request: UploadReferenceImageV2Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Upload and process reference image for Custom Visual Guide V2.

    V2 Differences from V1:
    - Extracts comprehensive STYLE PROFILE (not just aesthetic profile)
    - Saves reference image URL (used directly in generation)
    - No prompt fragment generation (uses meta-prompts instead)
    - Uses GPT-4o Vision (more detailed analysis)

    Processing Steps:
    1. Compute image hash for duplicate detection
    2. GPT-4o Vision extracts style profile JSON (medium, layout, colors, typography, etc.)
    3. Identifies identity elements to exclude ("what_to_leave_behind")
    4. Saves guide with version="v2" for distinction

    Returns:
        Guide preview with style profile summary
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        print(f"[V2 API] Processing V2 reference image for user {user_id}: {request.name}")

        # Process reference image
        guide = await CustomVisualGuideV2Service.process_reference_image_v2(
            image_url=request.image_url,
            user_id=user_id,
            brand_id=request.brand_id,
            name=request.name,
            db=db,
        )

        # Build response
        style_profile = guide.get("style_profile", {})

        response_data = {
            "id": guide["id"],
            "name": guide["name"],
            "version": "v2",
            "original_image_url": guide["original_image_url"],
            "uploaded_at": guide["uploaded_at"].isoformat(),

            # V2-specific: style profile summary
            "style_summary": {
                "medium": style_profile.get("medium"),
                "overall_aesthetic": style_profile.get("overall_aesthetic"),
                "mood": style_profile.get("mood"),
                "color_system": style_profile.get("color_system"),
                "typography_character": style_profile.get("typography", {}).get("character"),
            },

            "identity_elements_excluded": len(style_profile.get("what_to_leave_behind", [])),
            "times_used": guide.get("times_used", 0),
            "status": guide["status"],
        }

        return UriResponse.create_response(
            entity_name="Custom Visual Guide V2",
            data=response_data,
            message="Custom Visual Guide V2 created successfully! Style profile extracted and ready for use.",
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[V2 API] ❌ Error uploading V2 reference image: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/social-media/custom-guides-v2")
async def get_user_guides_v2(
    status: Optional[str] = Query("active", description="Filter by status: active | archived"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Get all Custom Visual Guides V2 for the authenticated user.

    Returns:
        List of V2 guides with style profile summaries
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        query = {"user_id": user_id, "version": "v2"}
        if status:
            query["status"] = status

        guides_cursor = db["custom_visual_guides"].find(query).sort("uploaded_at", -1)
        guides = await guides_cursor.to_list(length=100)

        # Format for response
        guides_list = []
        for guide in guides:
            style_profile = guide.get("style_profile", {})
            identity_protection = guide.get("identity_protection", {})

            guides_list.append({
                "id": str(guide["_id"]),
                "name": guide["name"],
                "version": "v2",
                "original_image_url": guide["original_image_url"],
                "uploaded_at": guide["uploaded_at"].isoformat(),
                "style_summary": {
                    "medium": style_profile.get("medium"),
                    "overall_aesthetic": style_profile.get("overall_aesthetic"),
                    "mood": style_profile.get("mood"),
                },
                "identity_elements_excluded": len(identity_protection.get("excluded_elements", [])),
                "times_used": guide.get("times_used", 0),
                "status": guide["status"],
            })

        return UriResponse.get_list_data_response(
            entity_name="Custom Visual Guide V2",
            data=guides_list,
            message=f"Found {len(guides_list)} Custom Visual Guides V2"
        )

    except Exception as e:
        print(f"[V2 API] ❌ Error fetching V2 guides: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/social-media/custom-guides-v2/{guide_id}")
async def get_guide_detail_v2(
    guide_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Get detailed information for a specific Custom Visual Guide V2.

    Returns:
        Complete guide data including full style profile JSON
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        guide = await db["custom_visual_guides"].find_one({
            "_id": ObjectId(guide_id),
            "user_id": user_id,
            "version": "v2",
        })

        if not guide:
            raise HTTPException(status_code=404, detail="Custom Visual Guide V2 not found")

        # Build detailed response
        response_data = {
            "id": str(guide["_id"]),
            "name": guide["name"],
            "version": "v2",
            "original_image_url": guide["original_image_url"],
            "uploaded_at": guide["uploaded_at"].isoformat(),

            # Full style profile
            "style_profile": guide["style_profile"],

            "times_used": guide.get("times_used", 0),
            "last_used_at": guide.get("last_used_at").isoformat() if guide.get("last_used_at") else None,
            "status": guide["status"],
        }

        return UriResponse.create_response(
            entity_name="Custom Visual Guide V2",
            data=response_data
        )

    except Exception as e:
        print(f"[V2 API] ❌ Error fetching V2 guide detail: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/social-media/custom-guides-v2/{guide_id}")
async def archive_guide_v2(
    guide_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Archive (soft delete) a Custom Visual Guide V2.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # 1. Archive the guide in database
        result = await db["custom_visual_guides"].update_one(
            {"_id": ObjectId(guide_id), "user_id": user_id, "version": "v2"},
            {
                "$set": {
                    "status": "archived",
                    "archived_at": datetime.utcnow(),
                }
            }
        )

        if result.modified_count == 0:
            raise HTTPException(status_code=404, detail="Custom Visual Guide V2 not found")

        # 2. Remove from brand profile's selected_custom_guides_v2 array
        await db["brand_profiles"].update_one(
            {"user_id": user_id},
            {"$pull": {"selected_custom_guides_v2": guide_id}}
        )
        print(f"[V2] Removed guide {guide_id} from user {user_id}'s selected_custom_guides_v2")

        return UriResponse.create_response(
            entity_name="Custom Visual Guide V2",
            data={"archived": True},
            message="Custom Visual Guide V2 archived successfully"
        )

    except Exception as e:
        print(f"[V2 API] ❌ Error archiving V2 guide: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/social-media/custom-guides-v2/{guide_id}/reanalyze")
async def reanalyze_guide_v2(
    guide_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Re-run GPT-4o Vision style extraction on an existing guide's reference
    image, replacing its stored style profile in place.

    Re-uploading the same file doesn't do this — duplicate detection (by
    image hash) either 409s or silently restores the guide with its old
    profile, without ever re-running extraction. Use this to pick up an
    extraction-prompt improvement on a guide that was already analyzed, or
    to retry a guide whose style was misclassified the first time.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        guide = await CustomVisualGuideV2Service.reanalyze_style_profile(guide_id, user_id, db)
        style_profile = guide.get("style_profile", {})

        return UriResponse.create_response(
            entity_name="Custom Visual Guide V2",
            data={
                "id": guide["id"],
                "name": guide["name"],
                "style_summary": {
                    "medium": style_profile.get("medium"),
                    "overall_aesthetic": style_profile.get("overall_aesthetic"),
                    "mood": style_profile.get("mood"),
                    "color_system": style_profile.get("color_system"),
                    "typography_character": style_profile.get("typography", {}).get("character"),
                },
            },
            message="Style profile re-analyzed successfully"
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[V2 API] ❌ Error re-analyzing V2 guide: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/social-media/custom-guides-v2/generate")
async def generate_with_v2_guide(
    request: GenerateWithV2GuideRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Generate image using Custom Visual Guide V2 + art director meta-prompt.

    V2 Generation Flow:
    1. Load guide (style_profile + reference_image_url)
    2. Build art director meta-prompt (style + brand + content)
    3. GPT-4o generates final image prompt
    4. Send prompt + reference image → GPT-Image-2 edit mode
    5. Return generated image with text zones and brand overlay info

    This is the KEY endpoint that demonstrates V2's power:
    - Reference image is used DIRECTLY (not just analyzed)
    - Style is transferred while preserving brand identity
    - Meta-prompt ensures identity separation

    Returns:
        {
            "image_url": str,
            "image_prompt": str,  # Generated by GPT-4o
            "reserved_text_zones": list,  # For canvas editor
            "brand_overlay": dict,  # Logo/handle positions
            "style_profile_used": str
        }
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        print(f"[V2 API] Generating with V2 guide: {request.guide_id}")

        # Get brand profile
        brand_profile = await db["brand_profiles"].find_one({"user_id": user_id})
        if not brand_profile:
            raise HTTPException(status_code=404, detail="Brand profile not found")

        # Build brand context
        brand_context = {
            "brand_name": brand_profile.get("brand_name", ""),
            "brand_colors": brand_profile.get("brand_colors", []),
            "logo_description": brand_profile.get("logo_description", "brand logo"),
            "font_style": brand_profile.get("font_style", "modern sans-serif"),
            "tone": brand_profile.get("tone", "professional"),
            "default_link": brand_profile.get("default_link", "@brand"),
        }

        # Generate image with V2 service
        result = await CustomVisualGuideV2Service.generate_image_with_v2_guide(
            guide_id=request.guide_id,
            brand_context=brand_context,
            seed_content=request.seed_content,
            headline=request.headline,
            subtext=request.subtext or "",
            cta=request.cta or "Learn more",
            platform=request.platform,
            db=db,
        )

        return UriResponse.create_response(
            entity_name="Image",
            data=result,
            message="Image generated successfully with Custom Visual Guide V2!"
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[V2 API] ❌ Error generating with V2 guide: {e}")
        raise HTTPException(status_code=500, detail=str(e))
