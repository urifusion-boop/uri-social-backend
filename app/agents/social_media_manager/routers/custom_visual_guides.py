# app/agents/social_media_manager/routers/custom_visual_guides.py

"""
Custom Visual Guides API Router
Handles reference image upload, analysis, font matching, and guide management.

Fixed: All UriResponse methods now use correct method names
(create_response, get_list_data_response, etc.) - NO .success()
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime
from bson import ObjectId

from app.dependencies import get_db_dependency
from app.core.auth_bearer import JWTBearer
from app.domain.responses.uri_response import UriResponse

from ..services.custom_visual_guide_service import CustomVisualGuideService

router = APIRouter(tags=["Custom Visual Guides"])


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

class UploadReferenceImageRequest(BaseModel):
    image_url: str = Field(..., description="Cloudinary URL of uploaded reference image")
    name: str = Field(..., min_length=1, max_length=100, description="Guide name")
    brand_id: Optional[str] = Field(None, description="Brand ID (optional)")


class UpdateGuideFontRequest(BaseModel):
    matched_font_id: str = Field(..., description="New font ID to use")


class GuideResponse(BaseModel):
    id: str
    name: str
    original_image_url: str
    uploaded_at: str
    aesthetic_summary: Dict[str, Any]
    typography_match: Dict[str, Any]
    match_outcome: str
    metadata_tags: Dict[str, Any]
    times_used: int
    status: str


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/social-media/custom-guides/upload")
async def upload_reference_image(
    request: UploadReferenceImageRequest,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    PRD Step 1-9: Upload and process reference image

    Creates a custom visual guide from uploaded reference image.
    Performs:
    - Safety, quality, copyright screening
    - Aesthetic extraction (GPT-4o-mini Vision)
    - Typography extraction and font matching
    - Prompt assembly
    - 11-dimension metadata tagging

    Returns:
        Guide preview with match outcome for user confirmation
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Check plan limit
        # TODO: Get user's actual plan from database
        user_plan = "starter"  # Placeholder
        can_upload = await CustomVisualGuideService.check_plan_limit(user_id, user_plan, db)

        if not can_upload:
            current_count = await CustomVisualGuideService.get_user_guide_count(user_id, db)
            limit = CustomVisualGuideService.PLAN_LIMITS.get(user_plan, 2)
            raise HTTPException(
                status_code=400,
                detail=f"You've reached the limit of {limit} custom guides. Please delete an existing guide to upload a new one."
            )

        # Process reference image
        print(f"[API] Processing reference image for user {user_id}: {request.name}")

        guide = await CustomVisualGuideService.process_reference_image(
            image_url=request.image_url,
            user_id=user_id,
            brand_id=request.brand_id,
            name=request.name,
            db=db,
        )

        # Build response
        aesthetic_summary = {
            "visual_genre": guide.aesthetic_profile.get("visual_genre"),
            "mood": guide.aesthetic_profile.get("mood"),
            "color_palette": guide.aesthetic_profile.get("color_palette"),
            "lighting": guide.aesthetic_profile.get("lighting", {}).get("specific_style"),
        }

        typography_match = {
            "has_typography": guide.typography_extraction.get("has_typography") if guide.typography_extraction else False,
            "match_outcome": guide.match_outcome,
            "matched_font_id": guide.matched_font_id,
            "matched_font_name": None,  # TODO: Fetch from fonts collection
            "match_confidence": guide.match_confidence,
            "identified_font_name": guide.identified_font_name,
            "next_step_suggestion": guide.next_step_suggestion,
            "alternative_matches": guide.alternative_font_matches,
        }

        response_data = {
            "id": guide.id,
            "name": guide.name,
            "original_image_url": guide.original_image_url,
            "uploaded_at": guide.uploaded_at.isoformat(),
            "aesthetic_summary": aesthetic_summary,
            "typography_match": typography_match,
            "match_outcome": guide.match_outcome,
            "metadata_tags": guide.metadata_tags,
            "times_used": guide.times_used,
            "status": guide.status,
        }

        return UriResponse.create_response("Custom Visual Guide",
            data=response_data,
            message="Custom visual guide created successfully! Review the preview below.",
        )

    except HTTPException:
        # Re-raise HTTPException as-is (preserves status codes like 400, 409)
        raise
    except Exception as e:
        print(f"[API] ❌ Error uploading reference image: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/social-media/custom-guides")
async def get_user_guides(
    status: Optional[str] = Query("active", description="Filter by status: active | archived"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Get all custom visual guides for the authenticated user

    Returns:
        List of guides with preview info
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        query = {"user_id": user_id}
        if status:
            query["status"] = status

        guides_cursor = db["custom_visual_guides"].find(query).sort("uploaded_at", -1)
        guides = await guides_cursor.to_list(length=100)

        # Format for response
        guides_list = []
        for guide in guides:
            aesthetic_summary = {
                "visual_genre": guide.get("aesthetic_profile", {}).get("visual_genre"),
                "mood": guide.get("aesthetic_profile", {}).get("mood"),
            }

            typography_match = {
                "has_typography": guide.get("typography_extraction", {}).get("has_typography", False),
                "match_outcome": guide.get("match_outcome"),
                "matched_font_id": guide.get("matched_font_id"),
                "matched_font_name": None,  # TODO: Fetch from fonts collection
                "match_confidence": guide.get("match_confidence"),
                "identified_font_name": guide.get("identified_font_name"),
                "next_step_suggestion": guide.get("next_step_suggestion"),
                "alternative_matches": guide.get("alternative_font_matches"),
            }

            guides_list.append({
                "id": str(guide["_id"]),
                "name": guide["name"],
                "original_image_url": guide["original_image_url"],
                "uploaded_at": guide["uploaded_at"].isoformat(),
                "aesthetic_summary": aesthetic_summary,
                "typography_match": typography_match,
                "match_outcome": guide["match_outcome"],
                "times_used": guide.get("times_used", 0),
                "status": guide["status"],
            })

        return UriResponse.get_list_data_response(
            "Custom Visual Guide",
            data={"guides": guides_list, "count": len(guides_list)},
            message=f"Found {len(guides_list)} custom guides"
        )

    except Exception as e:
        print(f"[API] ❌ Error fetching guides: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/social-media/custom-guides/{guide_id}")
async def get_guide_detail(
    guide_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Get detailed information for a specific guide

    Returns:
        Complete guide data including aesthetic profile, typography extraction, etc.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        guide = await db["custom_visual_guides"].find_one({
            "_id": ObjectId(guide_id),
            "user_id": user_id,
        })

        if not guide:
            raise HTTPException(status_code=404, detail="Guide not found")

        # Build detailed response
        response_data = {
            "id": str(guide["_id"]),
            "name": guide["name"],
            "original_image_url": guide["original_image_url"],
            "uploaded_at": guide["uploaded_at"].isoformat(),
            "aesthetic_profile": guide["aesthetic_profile"],
            "prompt_fragment": guide["prompt_fragment"],
            "typography_extraction": guide.get("typography_extraction"),
            "match_outcome": guide["match_outcome"],
            "matched_font_id": guide.get("matched_font_id"),
            "match_confidence": guide.get("match_confidence"),
            "alternative_font_matches": guide.get("alternative_font_matches"),
            "identified_font_name": guide.get("identified_font_name"),
            "next_step_suggestion": guide.get("next_step_suggestion"),
            "metadata_tags": guide["metadata_tags"],
            "times_used": guide.get("times_used", 0),
            "times_font_applied": guide.get("times_font_applied", 0),
            "status": guide["status"],
        }

        return UriResponse.get_single_data_response("Custom Visual Guide", data=response_data)

    except Exception as e:
        print(f"[API] ❌ Error fetching guide detail: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/social-media/custom-guides/{guide_id}/font")
async def update_guide_font(
    guide_id: str,
    request: UpdateGuideFontRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Update the matched font for a guide

    User can:
    - Switch from matched font to alternative
    - Switch to brand default (matched_font_id = null)
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        guide = await db["custom_visual_guides"].find_one({
            "_id": ObjectId(guide_id),
            "user_id": user_id,
        })

        if not guide:
            raise HTTPException(status_code=404, detail="Guide not found")

        # Update matched font
        update_data = {
            "matched_font_id": request.matched_font_id if request.matched_font_id != "brand_default" else None,
            "updated_at": datetime.utcnow(),
        }

        await db["custom_visual_guides"].update_one(
            {"_id": ObjectId(guide_id)},
            {"$set": update_data}
        )

        return UriResponse.update_response("Custom Visual Guide",
            message="Font selection updated successfully"
        )

    except Exception as e:
        print(f"[API] ❌ Error updating guide font: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/social-media/custom-guides/{guide_id}")
async def delete_guide(
    guide_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Permanently delete a custom visual guide
    This allows users to re-upload the same image after deletion
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        result = await db["custom_visual_guides"].delete_one(
            {"_id": ObjectId(guide_id), "user_id": user_id}
        )

        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Guide not found")

        return UriResponse.delete_response("Custom Visual Guide",
            is_deleted=True,
            message="Custom visual guide deleted successfully"
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] ❌ Error deleting guide: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/social-media/custom-guides/{guide_id}/rematch")
async def rematch_guide_fonts(
    guide_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Re-run font matching for a guide

    Used when user uploads new custom fonts and wants to check
    if any match better than the original match.

    PRD Section 11.7: Manual rematch trigger
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        guide = await db["custom_visual_guides"].find_one({
            "_id": ObjectId(guide_id),
            "user_id": user_id,
        })

        if not guide:
            raise HTTPException(status_code=404, detail="Guide not found")

        typography_extraction = guide.get("typography_extraction")
        if not typography_extraction:
            raise HTTPException(
                status_code=400,
                detail="This guide has no typography to match"
            )

        # Re-run font matching
        from ..services.custom_visual_guide_service import CustomVisualGuideService

        font_matches, match_outcome = await CustomVisualGuideService._match_fonts(
            typography_extraction, user_id, db
        )

        # Update guide with new match outcome
        await db["custom_visual_guides"].update_one(
            {"_id": ObjectId(guide_id)},
            {
                "$set": {
                    "match_outcome": match_outcome["outcome"],
                    "matched_font_id": match_outcome.get("matched_font_id"),
                    "matched_font_source": match_outcome.get("matched_font_source"),
                    "match_confidence": match_outcome.get("match_confidence"),
                    "alternative_font_matches": match_outcome.get("alternative_matches"),
                    "next_step_suggestion": match_outcome.get("next_step_suggestion"),
                    "updated_at": datetime.utcnow(),
                }
            }
        )

        return UriResponse.update_response("Custom Visual Guide",
            data={
                "match_outcome": match_outcome["outcome"],
                "matched_font_id": match_outcome.get("matched_font_id"),
                "match_confidence": match_outcome.get("match_confidence"),
            },
            message=f"Font matching updated: {match_outcome['outcome']}"
        )

    except Exception as e:
        print(f"[API] ❌ Error rematching fonts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/social-media/custom-guides/auto-rematch")
async def auto_rematch_after_font_upload(
    new_font_id: str = Query(..., description="ID of newly uploaded custom font"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    PRD Section 11.7: Auto-rematch guides when user uploads new custom font

    This endpoint is called automatically after user uploads a custom font.
    Re-runs font matching for all guides with NO_RECOMMENDED_MATCH outcome.

    Returns:
        List of guide IDs that were updated to better matches
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        from ..services.custom_visual_guide_service import CustomVisualGuideService

        updated_guide_ids = await CustomVisualGuideService.auto_rematch_guides_for_new_font(
            user_id, new_font_id, db
        )

        return UriResponse.update_response("Custom Visual Guide",
            data={
                "updated_guide_ids": updated_guide_ids,
                "count": len(updated_guide_ids),
            },
            message=f"Auto-rematch complete: {len(updated_guide_ids)} guides improved"
        )

    except Exception as e:
        print(f"[API] ❌ Error in auto-rematch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/social-media/custom-guides/{guide_id}/track-usage")
async def track_guide_usage(
    guide_id: str,
    applied_font: bool = Query(False, description="Whether matched font was used"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Track when a guide is used for content generation

    Updates usage analytics for the guide.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        update_ops = {
            "$inc": {"times_used": 1},
            "$set": {"last_used_at": datetime.utcnow()}
        }

        if applied_font:
            update_ops["$inc"]["times_font_applied"] = 1

        result = await db["custom_visual_guides"].update_one(
            {"_id": ObjectId(guide_id), "user_id": user_id},
            update_ops
        )

        if result.modified_count == 0:
            raise HTTPException(status_code=404, detail="Guide not found")

        # Also track in guide_usage_events collection
        usage_event = {
            "guide_id": guide_id,
            "applied_matched_font": applied_font,
            "used_at": datetime.utcnow(),
        }
        await db["guide_usage_events"].insert_one(usage_event)

        return UriResponse.create_response("Custom Visual Guide", message="Usage tracked")

    except Exception as e:
        print(f"[API] ❌ Error tracking usage: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/social-media/custom-guides/cleanup-archived")
async def cleanup_archived_guides(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """
    Cleanup endpoint to permanently delete all archived custom visual guides

    This allows users to re-upload the same images after deletion.
    Needed because old code archived guides instead of deleting them.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Delete all archived guides for this user
        result = await db["custom_visual_guides"].delete_many({
            "user_id": user_id,
            "status": "archived"
        })

        return UriResponse.create_response(
            "Custom Visual Guide",
            data={"deleted_count": result.deleted_count},
            message=f"Deleted {result.deleted_count} archived guides. You can now re-upload these images."
        )

    except Exception as e:
        print(f"[API] ❌ Error cleaning up archived guides: {e}")
        raise HTTPException(status_code=500, detail=str(e))
