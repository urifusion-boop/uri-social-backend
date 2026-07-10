"""
Visual Engine V2 Router
FastAPI endpoints for the 4-layer compositing system

PRD Section 5: API Endpoints
- POST /v2/content-plan - Generate Layer 1 (content)
- POST /v2/generate-image - Generate Layer 2 (Path A)
- POST /v2/upload-image - Upload Layer 2 (Path B)
- POST /v2/render - Full 4-layer render
- POST /v2/render-carousel - Multi-slide carousel
- GET /v2/review-queue - Get pending reviews
- POST /v2/review/{review_id}/approve - Approve review
- POST /v2/review/{review_id}/reject - Reject review
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from typing import Optional, List
from bson import ObjectId
from openai import AsyncOpenAI
import os

from app.agents.visual_engine_v2.models.visual_engine_models import (
    ContentPlanRequest,
    GenerateImageRequest,
    RenderRequest,
    CarouselRenderRequest,
    VisualEngineRenderV2,
    LayerData
)
from app.agents.visual_engine_v2.services.content_layer_service import ContentLayerService
from app.agents.visual_engine_v2.services.image_path_service import ImagePathService
from app.agents.visual_engine_v2.services.brand_compositor_service import BrandCompositorService
from app.agents.visual_engine_v2.services.quality_gate_service import QualityGateService
from app.database import get_database
from app.authentication import get_current_user


router = APIRouter(prefix="/v2", tags=["Visual Engine V2"])

# Initialize OpenAI client
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


@router.post("/content-plan")
async def generate_content_plan(
    request: ContentPlanRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database)
):
    """
    Generate Layer 1: Content (headline, subtext, CTA).

    PRD Section 7: Content Layer
    Returns structured text for template filling.
    """
    user_id = str(current_user["_id"])

    # Fetch brand profile
    brand_profile = await db["brand_profiles"].find_one(
        {"_id": ObjectId(request.brand_profile_id), "user_id": user_id}
    )

    if not brand_profile:
        raise HTTPException(status_code=404, detail="Brand profile not found")

    # Generate content layer
    content_service = ContentLayerService(openai_client)
    content_layer = await content_service.generate_content_plan(
        seed_content=request.seed_content,
        brand_context=brand_profile,
        post_intent=request.post_intent,
        platform=request.platform
    )

    return {
        "success": True,
        "content_layer": content_layer.model_dump(),
        "cost": content_layer.metadata.get("cost", 0.0)
    }


@router.post("/generate-image")
async def generate_image_path_a(
    request: GenerateImageRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database)
):
    """
    Generate Layer 2: Imagery (Path A - GPT Image 2).

    PRD Section 8: Imagery Layer - Path A
    Generates imagery-only (no text, no brand elements).
    """
    user_id = str(current_user["_id"])

    # Fetch brand profile for style hints
    brand_profile = await db["brand_profiles"].find_one(
        {"_id": ObjectId(request.brand_profile_id), "user_id": user_id}
    )

    if not brand_profile:
        raise HTTPException(status_code=404, detail="Brand profile not found")

    style_hint = brand_profile.get("style_prompt_fragment")

    # Generate imagery
    image_service = ImagePathService(openai_client)
    imagery_result = await image_service.generate_imagery_path_a(
        content_plan=request.content_plan,
        style_hint=style_hint,
        format=request.format
    )

    # Build LayerData
    imagery_layer = LayerData(
        layer_type="imagery",
        data={"imagery_url": imagery_result["imagery_url"]},
        metadata={
            "path": imagery_result["path"],
            "cost": imagery_result["cost"],
            "format": request.format
        }
    )

    return {
        "success": True,
        "imagery_layer": imagery_layer.model_dump(),
        "cost": imagery_result["cost"]
    }


@router.post("/upload-image")
async def upload_image_path_b(
    brand_profile_id: str = Form(...),
    format: str = Form("1:1"),
    remove_background: bool = Form(False),
    image_file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database)
):
    """
    Upload Layer 2: Imagery (Path B - User upload).

    PRD Section 8: Imagery Layer - Path B
    Processes user-uploaded image with optional background removal.
    """
    user_id = str(current_user["_id"])

    # Verify brand profile ownership
    brand_profile = await db["brand_profiles"].find_one(
        {"_id": ObjectId(brand_profile_id), "user_id": user_id}
    )

    if not brand_profile:
        raise HTTPException(status_code=404, detail="Brand profile not found")

    # Read image data
    image_data = await image_file.read()

    # Process image
    image_service = ImagePathService(openai_client)
    imagery_result = await image_service.process_uploaded_image_path_b(
        image_data=image_data,
        remove_background=remove_background,
        format=format
    )

    # Build LayerData
    imagery_layer = LayerData(
        layer_type="imagery",
        data={"imagery_url": imagery_result["imagery_url"]},
        metadata={
            "path": imagery_result["path"],
            "cost": imagery_result["cost"],
            "format": format,
            "background_removed": remove_background
        }
    )

    return {
        "success": True,
        "imagery_layer": imagery_layer.model_dump(),
        "cost": imagery_result["cost"]
    }


@router.post("/render")
async def render_full_composition(
    request: RenderRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database)
):
    """
    Full 4-layer render: Content → Imagery → Brand → Typesetting.

    PRD Section 11: Four-Layer Architecture
    Orchestrates all layers and produces final render.
    """
    user_id = str(current_user["_id"])

    # Verify brand profile ownership
    brand_profile = await db["brand_profiles"].find_one(
        {"_id": ObjectId(request.brand_profile_id), "user_id": user_id}
    )

    if not brand_profile:
        raise HTTPException(status_code=404, detail="Brand profile not found")

    # Parse layer data from request
    content_layer = LayerData(**request.content_layer)
    imagery_layer = LayerData(**request.imagery_layer)

    # Compose final render
    compositor_service = BrandCompositorService(db)
    render = await compositor_service.compose_final_render(
        user_id=user_id,
        brand_profile_id=request.brand_profile_id,
        content_layer=content_layer,
        imagery_layer=imagery_layer,
        format=request.format
    )

    # Quality gate evaluation
    quality_gate_service = QualityGateService(db)
    quality_result = await quality_gate_service.evaluate_render(render, user_id)

    # Save render to database
    render_dict = render.model_dump()
    render_dict["quality_gate_result"] = quality_result
    result = await db["visual_engine_renders_v2"].insert_one(render_dict)
    render_id = str(result.inserted_id)

    return {
        "success": True,
        "render_id": render_id,
        "final_outputs": render.final_outputs,
        "total_cost": render.total_cost,
        "quality_gate": quality_result,
        "status": "pending_review" if quality_result["requires_review"] else "completed"
    }


@router.post("/render-carousel")
async def render_carousel(
    request: CarouselRenderRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database)
):
    """
    Multi-slide carousel render.

    PRD Section 13: Carousel Posts
    Generates 2-10 slides for carousel posts.
    """
    user_id = str(current_user["_id"])

    # Verify brand profile ownership
    brand_profile = await db["brand_profiles"].find_one(
        {"_id": ObjectId(request.brand_profile_id), "user_id": user_id}
    )

    if not brand_profile:
        raise HTTPException(status_code=404, detail="Brand profile not found")

    # Parse layer data
    content_layer = LayerData(**request.content_layer)
    imagery_layer = LayerData(**request.imagery_layer)

    # Compose carousel render
    compositor_service = BrandCompositorService(db)
    render = await compositor_service.compose_final_render(
        user_id=user_id,
        brand_profile_id=request.brand_profile_id,
        content_layer=content_layer,
        imagery_layer=imagery_layer,
        format=request.format,
        carousel_count=request.carousel_count
    )

    # Quality gate evaluation
    quality_gate_service = QualityGateService(db)
    quality_result = await quality_gate_service.evaluate_render(render, user_id)

    # Save render to database
    render_dict = render.model_dump()
    render_dict["quality_gate_result"] = quality_result
    result = await db["visual_engine_renders_v2"].insert_one(render_dict)
    render_id = str(result.inserted_id)

    return {
        "success": True,
        "render_id": render_id,
        "carousel_slides": render.final_outputs,
        "slide_count": len(render.final_outputs),
        "total_cost": render.total_cost,
        "quality_gate": quality_result,
        "status": "pending_review" if quality_result["requires_review"] else "completed"
    }


@router.get("/review-queue")
async def get_review_queue(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database)
):
    """
    Get pending review queue items for current user.

    PRD Section 14: Quality Gate & Human Review
    """
    user_id = str(current_user["_id"])

    quality_gate_service = QualityGateService(db)
    pending_reviews = await quality_gate_service.get_pending_reviews(user_id=user_id)

    return {
        "success": True,
        "pending_reviews": [review.model_dump() for review in pending_reviews],
        "count": len(pending_reviews)
    }


@router.post("/review/{review_id}/approve")
async def approve_review(
    review_id: str,
    reviewer_notes: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database)
):
    """
    Manually approve a render from review queue.

    PRD Section 14: Quality Gate & Human Review
    """
    quality_gate_service = QualityGateService(db)
    success = await quality_gate_service.approve_render(review_id, reviewer_notes)

    if not success:
        raise HTTPException(status_code=404, detail="Review not found")

    return {"success": True, "message": "Render approved"}


@router.post("/review/{review_id}/reject")
async def reject_review(
    review_id: str,
    reviewer_notes: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database)
):
    """
    Manually reject a render from review queue.

    PRD Section 14: Quality Gate & Human Review
    """
    quality_gate_service = QualityGateService(db)
    success = await quality_gate_service.reject_render(review_id, reviewer_notes)

    if not success:
        raise HTTPException(status_code=404, detail="Review not found")

    return {"success": True, "message": "Render rejected"}
