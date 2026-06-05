"""
V3 Toggle Endpoint - Production Integration
Adds V3 support to existing content generation with frontend toggle.

This modifies the production flow to check brand_profile.use_v3_prompts flag
and route to V3 system if enabled.
"""

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import Optional

from app.database import get_db
from app.dependencies import get_current_user
from app.domain.responses.uri_response import UriResponse
from app.agents.social_media_manager.services.image_content_service import ImageContentService
from app.agents.social_media_manager.services.v3_image_content_service import V3ImageContentService
from app.services.PostHogService import track_event


router = APIRouter(prefix="/v3", tags=["V3 Production"])


@router.post("/toggle")
async def toggle_v3_for_user(
    enabled: bool,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """
    Enable or disable V3 prompt system for this user.
    Sets brand_profile.use_v3_prompts flag.

    Args:
        enabled: true to use V3, false to use V2 (production)

    Returns:
        {"use_v3_prompts": true/false}
    """
    try:
        user_id = current_user.get("userId") or current_user.get("user_id")

        # Update brand profile
        result = await db["brand_profiles"].update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "use_v3_prompts": enabled,
                    "v3_enabled_at": None if not enabled else db.get_collection("brand_profiles").database.client.get_database().get_collection("$cmd").aggregate([{"$currentDate": {}}]).next()["_created"],
                }
            },
            upsert=False
        )

        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Brand profile not found. Complete onboarding first.")

        # Track in PostHog
        track_event(user_id, "v3_toggle_changed", {
            "enabled": enabled,
            "action": "enabled" if enabled else "disabled"
        })

        print(f"[V3 TOGGLE] User {user_id} {'enabled' if enabled else 'disabled'} V3 prompts")

        return UriResponse.get_single_data_response("v3_toggle", {
            "use_v3_prompts": enabled,
            "message": f"V3 prompt system {'enabled' if enabled else 'disabled'} successfully!",
            "info": "Your next image generations will use the " + ("V3 10-block" if enabled else "V2 6-section") + " prompt system."
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to toggle V3: {str(e)}")


@router.get("/status")
async def get_v3_status(
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """
    Check if V3 is enabled for the current user.

    Returns:
        {
            "use_v3_prompts": true/false,
            "prompt_system": "V3 10-Block" or "V2 6-Section"
        }
    """
    try:
        user_id = current_user.get("userId") or current_user.get("user_id")

        brand_profile = await db["brand_profiles"].find_one({"user_id": user_id})

        if not brand_profile:
            raise HTTPException(status_code=404, detail="Brand profile not found")

        use_v3 = brand_profile.get("use_v3_prompts", False)

        return UriResponse.get_single_data_response("v3_status", {
            "use_v3_prompts": use_v3,
            "prompt_system": "V3 10-Block" if use_v3 else "V2 6-Section",
            "message": f"Currently using {('V3' if use_v3 else 'V2')} prompt system"
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get V3 status: {str(e)}")


# ========== HELPER FUNCTION FOR MAIN ROUTER ==========

async def generate_image_with_v3_check(
    seed_content: str,
    brand_context: dict,
    platform: str,
    reference_image: Optional[str],
    user_id: str,
    db,
) -> dict:
    """
    Helper function to check use_v3_prompts flag and route to appropriate service.

    This should be called from the main content generation router.

    Args:
        seed_content: User's content/request
        brand_context: Brand profile dict
        platform: Target platform
        reference_image: Product image URL (optional)
        user_id: User ID
        db: Database connection

    Returns:
        Image generation result from V2 or V3 service
    """
    # Check if user has V3 enabled
    use_v3 = brand_context.get("use_v3_prompts", False)

    if use_v3:
        print(f"[V3 ROUTING] User {user_id} has V3 enabled - using V3 service")

        # Track usage
        track_event(user_id, "v3_generation_production", {
            "platform": platform,
            "has_reference": bool(reference_image),
        })

        # Use V3 service
        result = await V3ImageContentService.generate_image_for_draft_v3(
            seed_content=seed_content,
            brand_context=brand_context,
            platform=platform,
            reference_image=reference_image,
            user_id=user_id,
            image_model="gpt-image-2",  # Always GPT-Image-2
        )

        # Extract image URL from V3 response format
        if result.get("status"):
            response_data = result.get("responseData", {})
            return {
                "image_url": response_data.get("image_url"),
                "prompt_used": response_data.get("v3_metadata", {}).get("prompt"),
                "v3_metadata": response_data.get("v3_metadata"),
                "system_used": "v3",
            }
        else:
            raise Exception(result.get("responseMessage", "V3 generation failed"))

    else:
        print(f"[V3 ROUTING] User {user_id} using V2 (production) service")

        # Use V2 (production) service
        result = await ImageContentService.generate_image_for_draft(
            seed_content=seed_content,
            brand_context=brand_context,
            platform=platform,
            reference_image=reference_image,
        )

        # Extract image URL from V2 response format
        if result.get("status"):
            response_data = result.get("responseData", {})
            return {
                "image_url": response_data.get("image_url"),
                "prompt_used": response_data.get("prompt_used"),
                "system_used": "v2",
            }
        else:
            raise Exception(result.get("responseMessage", "V2 generation failed"))
