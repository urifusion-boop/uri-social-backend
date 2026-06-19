"""
V3 Test Router - A/B Comparison Endpoints
Isolated router for testing V3 image generation system against V2 (production).

Endpoints:
- POST /v3/test/compare-generation - Generate with both V2 and V3, return both
- POST /v3/test/generate-v3-only - Generate with V3 only
- POST /v3/test/record-choice - Record which version user chose
- GET /v3/test/stats - Get V3 testing statistics
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any, Optional, List
from datetime import datetime
from bson import ObjectId

from app.database import get_db
from app.dependencies import get_current_user
from app.domain.responses.uri_response import UriResponse
from app.agents.social_media_manager.services.image_content_service import ImageContentService
from app.agents.social_media_manager.services.v3_image_content_service import V3ImageContentService
from app.services.PostHogService import track_event


router = APIRouter(prefix="/v3/test", tags=["V3 Testing"])


@router.post("/compare-generation")
async def compare_v2_vs_v3(
    seed_content: str,
    platform: str = "instagram",
    reference_image: Optional[str] = None,
    image_model: str = "gpt-image-2",
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """
    Generate image with BOTH V2 (production) and V3 systems for side-by-side comparison.

    Returns:
        {
            "comparison_id": "uuid",
            "v2_result": {...},
            "v3_result": {...},
            "prompt_diff": {
                "v2_length": 856,
                "v3_length": 1847,
                "v3_new_blocks": [...]
            }
        }
    """
    try:
        user_id = current_user.get("userId") or current_user.get("user_id")

        print(f"\n{'='*70}")
        print(f"V2 vs V3 COMPARISON TEST")
        print(f"{'='*70}")
        print(f"User: {user_id}")
        print(f"Platform: {platform}")
        print(f"Seed: {seed_content[:100]}...")

        # Load user's brand profile
        brand_profile = await db["brand_profiles"].find_one({"user_id": user_id})
        if not brand_profile:
            raise HTTPException(status_code=404, detail="Brand profile not found. Please complete onboarding.")

        brand_context = {
            "user_id": user_id,
            "brand_name": brand_profile.get("brand_name"),
            "brand_colors": brand_profile.get("brand_colors", []),
            "industry": brand_profile.get("industry"),
            "region": brand_profile.get("region"),
            "style_slug": brand_profile.get("style_slug"),
            "logo_url": brand_profile.get("logo_url"),
            "logo_position": brand_profile.get("logo_position", "bottom-right"),
            "logo_size": brand_profile.get("logo_size", "small"),
            "tagline": brand_profile.get("tagline"),
            "cta_styles": brand_profile.get("cta_styles", []),
            "default_link": brand_profile.get("default_link"),
        }

        # Generate with V2 (production system)
        print(f"\n[COMPARISON] Generating with V2 (production)...")
        v2_result = await ImageContentService.generate_image_for_draft(
            seed_content=seed_content,
            brand_context=brand_context,
            platform=platform,
            reference_image=reference_image,
            image_model=image_model,
        )

        # Generate with V3 (test system)
        print(f"\n[COMPARISON] Generating with V3 (test)...")
        v3_result = await V3ImageContentService.generate_image_for_draft_v3(
            seed_content=seed_content,
            brand_context=brand_context,
            platform=platform,
            reference_image=reference_image,
            user_id=user_id,
            image_model=image_model,
        )

        # Create comparison record
        comparison_id = str(ObjectId())

        # Extract prompt lengths for diff
        v2_prompt = v2_result.get("responseData", {}).get("prompt_used", "")
        v3_metadata = v3_result.get("responseData", {}).get("v3_metadata", {})
        v3_prompt = v3_metadata.get("prompt", "")

        prompt_diff = {
            "v2_length": len(v2_prompt),
            "v3_length": len(v3_prompt),
            "length_increase_pct": int(((len(v3_prompt) - len(v2_prompt)) / len(v2_prompt)) * 100) if v2_prompt else 0,
            "v3_new_blocks": v3_metadata.get("blocks_used", []),
            "v3_architecture": "10-block",
            "v2_architecture": "6-section",
        }

        # Save comparison to v3_test_generations collection
        comparison_doc = {
            "comparison_id": comparison_id,
            "user_id": user_id,
            "seed_content": seed_content,
            "platform": platform,
            "v2_draft_id": v2_result.get("responseData", {}).get("draft_id"),
            "v3_draft_id": v3_result.get("responseData", {}).get("draft_id"),
            "v2_image_url": v2_result.get("responseData", {}).get("image_url"),
            "v3_image_url": v3_result.get("responseData", {}).get("image_url"),
            "v2_prompt": v2_prompt,
            "v3_prompt": v3_prompt,
            "prompt_diff": prompt_diff,
            "user_choice": None,  # Will be set when user picks
            "chosen_at": None,
            "created_at": datetime.utcnow(),
        }

        await db["v3_test_generations"].insert_one(comparison_doc)

        # Track in PostHog
        track_event(user_id, "v3_comparison_generated", {
            "comparison_id": comparison_id,
            "platform": platform,
            "v2_prompt_length": len(v2_prompt),
            "v3_prompt_length": len(v3_prompt),
            "has_reference_image": bool(reference_image),
        })

        print(f"\n{'='*70}")
        print(f"COMPARISON COMPLETE")
        print(f"{'='*70}")
        print(f"Comparison ID: {comparison_id}")
        print(f"V2 Image: {comparison_doc['v2_image_url'][:80]}...")
        print(f"V3 Image: {comparison_doc['v3_image_url'][:80]}...")
        print(f"{'='*70}\n")

        return UriResponse.get_single_data_response("v2_v3_comparison", {
            "comparison_id": comparison_id,
            "v2_result": v2_result.get("responseData"),
            "v3_result": v3_result.get("responseData"),
            "prompt_diff": prompt_diff,
            "message": "Both versions generated. Choose which one to use."
        })

    except Exception as e:
        print(f"[COMPARISON] Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Comparison failed: {str(e)}")


@router.post("/generate-v3-only")
async def generate_v3_only(
    seed_content: str,
    platform: str = "instagram",
    reference_image: Optional[str] = None,
    image_model: str = "gpt-image-2",
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """
    Generate image with V3 system only (no comparison).
    Useful for batch testing or when user has chosen V3.
    """
    try:
        user_id = current_user.get("userId") or current_user.get("user_id")

        # Load brand profile
        brand_profile = await db["brand_profiles"].find_one({"user_id": user_id})
        if not brand_profile:
            raise HTTPException(status_code=404, detail="Brand profile not found")

        brand_context = {
            "user_id": user_id,
            "brand_name": brand_profile.get("brand_name"),
            "brand_colors": brand_profile.get("brand_colors", []),
            "industry": brand_profile.get("industry"),
            "region": brand_profile.get("region"),
            "style_slug": brand_profile.get("style_slug"),
            "logo_url": brand_profile.get("logo_url"),
            "logo_position": brand_profile.get("logo_position", "bottom-right"),
            "logo_size": brand_profile.get("logo_size", "small"),
            "tagline": brand_profile.get("tagline"),
            "cta_styles": brand_profile.get("cta_styles", []),
            "default_link": brand_profile.get("default_link"),
        }

        # Generate with V3
        v3_result = await V3ImageContentService.generate_image_for_draft_v3(
            seed_content=seed_content,
            brand_context=brand_context,
            platform=platform,
            reference_image=reference_image,
            user_id=user_id,
            image_model=image_model,
        )

        # Save to test database
        draft_data = v3_result.get("responseData", {})
        test_doc = {
            "test_generation_id": str(ObjectId()),
            "user_id": user_id,
            "version": "v3",
            "draft_id": draft_data.get("draft_id"),
            "image_url": draft_data.get("image_url"),
            "v3_metadata": draft_data.get("v3_metadata"),
            "platform": platform,
            "seed_content": seed_content,
            "created_at": datetime.utcnow(),
        }

        await db["v3_test_generations"].insert_one(test_doc)

        # Track in PostHog
        track_event(user_id, "v3_generation_only", {
            "platform": platform,
            "prompt_length": draft_data.get("v3_metadata", {}).get("prompt_length"),
            "has_reference_image": bool(reference_image),
        })

        return v3_result

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"V3 generation failed: {str(e)}")


@router.post("/record-choice")
async def record_user_choice(
    comparison_id: str,
    chosen_version: str,  # "v2" or "v3"
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """
    Record which version (V2 or V3) the user chose to use.
    This is the PRIMARY success metric for V3 testing.

    Args:
        comparison_id: ID from compare-generation endpoint
        chosen_version: "v2" or "v3"
    """
    try:
        user_id = current_user.get("userId") or current_user.get("user_id")

        if chosen_version not in ["v2", "v3"]:
            raise HTTPException(status_code=400, detail="chosen_version must be 'v2' or 'v3'")

        # Update comparison record
        result = await db["v3_test_generations"].update_one(
            {"comparison_id": comparison_id, "user_id": user_id},
            {
                "$set": {
                    "user_choice": chosen_version,
                    "chosen_at": datetime.utcnow(),
                }
            }
        )

        if result.modified_count == 0:
            raise HTTPException(status_code=404, detail="Comparison not found")

        # Track in PostHog (PRIMARY METRIC)
        track_event(user_id, "v3_comparison_choice", {
            "comparison_id": comparison_id,
            "chosen_version": chosen_version,
            "v3_won": chosen_version == "v3",
        })

        print(f"[V3 TEST] User {user_id} chose {chosen_version} (comparison {comparison_id})")

        return UriResponse.get_single_data_response("choice_recorded", {
            "comparison_id": comparison_id,
            "chosen_version": chosen_version,
            "message": f"Your choice ({chosen_version}) has been recorded. Thank you for testing!"
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to record choice: {str(e)}")


@router.get("/stats")
async def get_v3_test_stats(
    current_user: dict = Depends(get_current_user),
    db = Depends(get_db),
):
    """
    Get V3 testing statistics.
    Shows approval rates, edit rates, and user preferences.

    Returns:
        {
            "total_comparisons": 150,
            "v3_win_rate": 0.67,  # 67% of users chose V3
            "v2_win_rate": 0.33,
            "avg_prompt_length_v2": 856,
            "avg_prompt_length_v3": 1847,
            "recent_comparisons": [...]
        }
    """
    try:
        user_id = current_user.get("userId") or current_user.get("user_id")

        # Get all comparisons with choices
        comparisons = await db["v3_test_generations"].find({
            "user_id": user_id,
            "user_choice": {"$ne": None}
        }).to_list(length=1000)

        if not comparisons:
            return UriResponse.get_single_data_response("v3_stats", {
                "message": "No test data yet. Try the /compare-generation endpoint first!",
                "total_comparisons": 0,
            })

        # Calculate stats
        total = len(comparisons)
        v3_wins = sum(1 for c in comparisons if c.get("user_choice") == "v3")
        v2_wins = sum(1 for c in comparisons if c.get("user_choice") == "v2")

        v3_win_rate = v3_wins / total if total > 0 else 0
        v2_win_rate = v2_wins / total if total > 0 else 0

        # Prompt length averages
        v2_prompts = [len(c.get("v2_prompt", "")) for c in comparisons if c.get("v2_prompt")]
        v3_prompts = [len(c.get("v3_prompt", "")) for c in comparisons if c.get("v3_prompt")]

        avg_v2_length = int(sum(v2_prompts) / len(v2_prompts)) if v2_prompts else 0
        avg_v3_length = int(sum(v3_prompts) / len(v3_prompts)) if v3_prompts else 0

        # Recent comparisons
        recent = sorted(comparisons, key=lambda x: x.get("created_at", datetime.min), reverse=True)[:10]

        return UriResponse.get_single_data_response("v3_stats", {
            "total_comparisons": total,
            "v3_wins": v3_wins,
            "v2_wins": v2_wins,
            "v3_win_rate": round(v3_win_rate, 2),
            "v2_win_rate": round(v2_win_rate, 2),
            "avg_prompt_length_v2": avg_v2_length,
            "avg_prompt_length_v3": avg_v3_length,
            "prompt_length_increase": avg_v3_length - avg_v2_length,
            "recent_comparisons": [
                {
                    "comparison_id": c.get("comparison_id"),
                    "platform": c.get("platform"),
                    "chosen_version": c.get("user_choice"),
                    "created_at": c.get("created_at").isoformat() if c.get("created_at") else None,
                }
                for c in recent
            ],
            "recommendation": "V3 is winning!" if v3_win_rate > 0.6 else "Keep testing..." if v3_win_rate > 0.4 else "V2 is performing better"
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {str(e)}")
