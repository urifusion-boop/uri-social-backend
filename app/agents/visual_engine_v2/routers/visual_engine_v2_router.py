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
from typing import Optional, List, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
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
from app.agents.visual_engine_v2.services.image_path_service import ImagePathService, ImageGenerationError
from app.agents.visual_engine_v2.services.brand_compositor_service import BrandCompositorService
from app.agents.visual_engine_v2.services.quality_gate_service import QualityGateService
from app.agents.visual_engine_v2.services.publish_bridge_service import PublishBridgeService, SUPPORTED_PLATFORMS
from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
from app.dependencies import get_db_dependency, get_active_brand_context, get_current_user
from datetime import datetime


router = APIRouter(prefix="/v2", tags=["Visual Engine V2"])

# Initialize OpenAI client
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


async def _require_brand_profile(user_id: str, brand_id: str, db: AsyncIOMotorDatabase) -> Dict[str, Any]:
    """
    Resolve the active brand's profile the same way every other endpoint in this
    app does (BrandProfileService.get, scoped by JWT user_id + active brand_id) —
    never by a client-supplied brand_profile_id, which nothing else in the app
    exposes (BrandProfileService.get() deliberately strips _id from every response).
    """
    result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
    profile = result.get("responseData") if isinstance(result, dict) else None
    if not profile:
        raise HTTPException(status_code=404, detail="Brand profile not found — complete Brand Playbook first")
    return profile


@router.post("/content-plan")
async def generate_content_plan(
    request: ContentPlanRequest,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate Layer 1: Content (headline, subtext, CTA).

    PRD Section 7: Content Layer
    Returns structured text for template filling.
    """
    user_id = brand_ctx["user_id"]
    brand_profile = await _require_brand_profile(user_id, brand_ctx["brand_id"], db)

    # Generate content layer
    content_service = ContentLayerService(openai_client)
    content_layer = await content_service.generate_content_plan(
        seed_content=request.seed_content,
        brand_context=brand_profile,
        post_intent=request.post_intent,
        platform=(request.platforms[0] if request.platforms else "instagram")
    )

    return {
        "success": True,
        "content_layer": content_layer.model_dump(),
        "cost": content_layer.metadata.get("cost", 0.0)
    }


@router.post("/generate-image")
async def generate_image_path_a(
    request: GenerateImageRequest,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate Layer 2: Imagery (Path A - GPT Image 2).

    PRD Section 8: Imagery Layer - Path A
    Generates imagery-only (no text, no brand elements).
    """
    brand_profile = await _require_brand_profile(brand_ctx["user_id"], brand_ctx["brand_id"], db)
    style_hint = brand_profile.get("style_prompt_fragment")

    # Generate imagery. Path A already retries once internally (PRD Section 12);
    # if it still fails, fall back to a brand-colored placeholder rather than a 500.
    image_service = ImagePathService(openai_client)
    needs_attention = False
    error_message = None
    try:
        imagery_result = await image_service.generate_imagery_path_a(
            content_plan=request.content_plan,
            style_hint=style_hint,
            format=request.format
        )
    except ImageGenerationError as e:
        print(f"⚠️ [Path A] Falling back to brand-colored placeholder: {e}")
        needs_attention = True
        error_message = str(e)
        imagery_result = ImagePathService.generate_placeholder_image(
            brand_profile.get("primary_color"),
            format=request.format
        )

    # Build LayerData
    imagery_layer = LayerData(
        layer_type="imagery",
        data={"imagery_url": imagery_result["imagery_url"]},
        metadata={
            "path": imagery_result["path"],
            "cost": imagery_result["cost"],
            "format": request.format,
            "needs_attention": needs_attention,
            "error_message": error_message
        }
    )

    return {
        "success": True,
        "imagery_layer": imagery_layer.model_dump(),
        "cost": imagery_result["cost"],
        "needs_attention": needs_attention
    }


@router.post("/upload-image")
async def upload_image_path_b(
    format: str = Form("1:1"),
    remove_background: bool = Form(False),
    image_file: UploadFile = File(...),
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Upload Layer 2: Imagery (Path B - User upload).

    PRD Section 8: Imagery Layer - Path B
    Processes user-uploaded image with optional background removal.
    """
    await _require_brand_profile(brand_ctx["user_id"], brand_ctx["brand_id"], db)

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
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Full 4-layer render: Content → Imagery → Brand → Typesetting.

    PRD Section 11: Four-Layer Architecture
    Orchestrates all layers and produces final render.
    """
    user_id = brand_ctx["user_id"]
    await _require_brand_profile(user_id, brand_ctx["brand_id"], db)

    # Parse layer data from request
    content_layer = LayerData(**request.content_layer)
    imagery_layer = LayerData(**request.imagery_layer)

    # Compose final render — across every requested format if `formats` was given
    compositor_service = BrandCompositorService(db)
    render = await compositor_service.compose_final_render(
        user_id=user_id,
        brand_id=brand_ctx["brand_id"],
        content_layer=content_layer,
        imagery_layer=imagery_layer,
        format=request.format,
        formats=request.formats
    )

    # Quality gate evaluation (may reference render.id, e.g. in a review-queue entry)
    quality_gate_service = QualityGateService(db)
    quality_result = await quality_gate_service.evaluate_render(render, user_id)

    # Save render to database, keyed by the same id the quality gate already used
    # (so a review-queue entry's render_id resolves to this document)
    render_dict = render.model_dump()
    render_dict["_id"] = render.id
    render_dict["quality_gate_result"] = quality_result
    await db["visual_engine_renders_v2"].insert_one(render_dict)

    return {
        "success": True,
        "render_id": render.id,
        "final_outputs": render.final_outputs,
        "format_outputs": render.format_outputs,
        "template_id": render.typesetting_layer.data.get("template_id"),
        "style_family": render.typesetting_layer.data.get("style_family"),
        "total_cost": render.total_cost,
        "quality_gate": quality_result,
        "status": "pending_review" if quality_result["requires_review"] else "completed"
    }


@router.post("/render-carousel")
async def render_carousel(
    request: CarouselRenderRequest,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Multi-slide carousel render.

    PRD Section 13: Carousel Posts
    Generates 2-10 slides for carousel posts.
    """
    user_id = brand_ctx["user_id"]
    await _require_brand_profile(user_id, brand_ctx["brand_id"], db)

    # Parse layer data
    content_layer = LayerData(**request.content_layer)
    imagery_layer = LayerData(**request.imagery_layer)

    # Compose carousel render — across every requested format if `formats` was given
    compositor_service = BrandCompositorService(db)
    render = await compositor_service.compose_final_render(
        user_id=user_id,
        brand_id=brand_ctx["brand_id"],
        content_layer=content_layer,
        imagery_layer=imagery_layer,
        format=request.format,
        formats=request.formats,
        carousel_count=request.carousel_count
    )

    # Quality gate evaluation (may reference render.id, e.g. in a review-queue entry)
    quality_gate_service = QualityGateService(db)
    quality_result = await quality_gate_service.evaluate_render(render, user_id)

    # Save render to database, keyed by the same id the quality gate already used
    # (so a review-queue entry's render_id resolves to this document)
    render_dict = render.model_dump()
    render_dict["_id"] = render.id
    render_dict["quality_gate_result"] = quality_result
    await db["visual_engine_renders_v2"].insert_one(render_dict)

    return {
        "success": True,
        "render_id": render.id,
        "carousel_slides": render.final_outputs,
        "slide_count": len(render.final_outputs),
        "format_outputs": render.format_outputs,
        "template_id": render.typesetting_layer.data.get("template_id"),
        "style_family": render.typesetting_layer.data.get("style_family"),
        "total_cost": render.total_cost,
        "quality_gate": quality_result,
        "status": "pending_review" if quality_result["requires_review"] else "completed"
    }


@router.get("/review-queue")
async def get_review_queue(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get pending review queue items for current user.

    PRD Section 14: Quality Gate & Human Review
    """
    user_id = str(current_user["userId"])

    quality_gate_service = QualityGateService(db)
    pending_reviews = await quality_gate_service.get_pending_reviews(user_id=user_id)

    return {
        "success": True,
        "pending_reviews": [review.model_dump() for review in pending_reviews],
        "count": len(pending_reviews)
    }


@router.post("/review-queue/sweep-expired")
async def sweep_expired_reviews(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Auto-approve soft-review renders whose review window has expired without an
    explicit rejection (PRD Section 13). Mandatory-tier renders are never touched.

    Not yet wired to a scheduler — this module is still isolated for testing.
    Point an external cron at this endpoint (mirroring the existing
    /social-media/publish-scheduled pattern) once ready to move past that.
    """
    quality_gate_service = QualityGateService(db)
    result = await quality_gate_service.sweep_expired_soft_reviews()
    return {"success": True, **result}


@router.post("/review/{review_id}/approve")
async def approve_review(
    review_id: str,
    reviewer_notes: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
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
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
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


@router.get("/connections")
async def get_connected_platforms(
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Which platforms does the active brand's account actually have connected
    right now — reuses the exact social_connections query the real posting
    pipeline runs, so this reflects reality, not a guess.
    """
    bridge = PublishBridgeService(db)
    connected = await bridge.get_connected_platforms(brand_ctx["user_id"])
    return {
        "success": True,
        "connected_platforms": connected,
        "supported_platforms": SUPPORTED_PLATFORMS,
    }


@router.post("/render/{render_id}/publish")
async def publish_render(
    render_id: str,
    platform: str,
    scheduled_datetime: Optional[str] = None,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Bridge a completed V2 render into the real posting pipeline: builds a
    content_drafts document in the shape approval_workflow_service.py expects
    and hands it to the actual publish/schedule functions — no platform API
    calls happen in this module, they're reused from the existing pipeline.

    scheduled_datetime: ISO 8601 string. Omit to publish immediately.
    """
    user_id = brand_ctx["user_id"]

    render = await db["visual_engine_renders_v2"].find_one({"_id": render_id, "user_id": user_id})
    if not render:
        raise HTTPException(status_code=404, detail="Render not found")

    parsed_schedule = None
    if scheduled_datetime:
        try:
            parsed_schedule = datetime.fromisoformat(scheduled_datetime.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="scheduled_datetime must be ISO 8601")

    bridge = PublishBridgeService(db)
    result = await bridge.publish_render(
        user_id=user_id,
        brand_id=brand_ctx.get("brand_id"),
        render=render,
        platform=platform,
        scheduled_datetime=parsed_schedule,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    return result
