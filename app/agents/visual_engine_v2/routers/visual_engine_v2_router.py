"""
Visual Engine V2 Router
FastAPI endpoints for the 4-layer compositing system

PRD Section 5: API Endpoints
- POST /v2/content-plan - Generate Layer 1 (content)
- POST /v2/generate-image - Generate Layer 2 (Path A) — starts a background job
- POST /v2/upload-image - Upload Layer 2 (Path B) — starts a background job
- POST /v2/render - Full 4-layer render — starts a background job
- POST /v2/render-carousel - Multi-slide carousel — starts a background job
- GET /v2/jobs/{job_id} - Poll a background job's status/result
- GET /v2/review-queue - Get pending reviews
- POST /v2/review/{review_id}/approve - Approve review
- POST /v2/review/{review_id}/reject - Reject review
- GET /v2/connections - Which platforms are actually connected
- POST /v2/render/{render_id}/publish - Bridge a render into real posting

Generate-image/upload-image/render/render-carousel all return a job_id
immediately and do the actual (potentially 30-90s+) work in a FastAPI
BackgroundTasks callback, mirroring the production /generate-content
endpoint's own pattern ("text is always returned immediately... images
are generated in the background"). Holding one HTTP connection open for
the full duration of a GPT Image 2 or Orshot call is fragile against any
intermediate proxy/gateway timeout, independent of the client's own
timeout setting — confirmed live: raising the frontend timeout to 300s
did not fix a hung request, because nothing in the chain was actually
waiting on the client's timeout value at all.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form
from typing import Optional, List, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai import AsyncOpenAI
from datetime import datetime
import asyncio
import os
import uuid

from app.agents.visual_engine_v2.models.visual_engine_models import (
    ContentPlanRequest,
    GenerateImageRequest,
    RenderRequest,
    CarouselRenderRequest,
    CarouselContentPlanRequest,
    CarouselGenerateImagesRequest,
    BrandPrefsUpdateRequest,
    VisualEngineRenderV2,
    LayerData
)
from app.agents.visual_engine_v2.services.content_layer_service import ContentLayerService
from app.agents.visual_engine_v2.services.image_path_service import ImagePathService, ImageGenerationError
from app.agents.visual_engine_v2.services.brand_compositor_service import BrandCompositorService
from app.agents.visual_engine_v2.services.quality_gate_service import QualityGateService
from app.agents.visual_engine_v2.services.publish_bridge_service import PublishBridgeService, SUPPORTED_PLATFORMS
from app.agents.visual_engine_v2.services.brand_prefs_service import BrandPrefsServiceV2
from app.agents.visual_engine_v2.services.image_cache_service import ImageCacheServiceV2, hash_image_bytes
from app.agents.visual_engine_v2.services.metrics_service import VisualEngineMetricsServiceV2
from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
from app.agents.social_media_manager.services.style_library import pick_next_style
from app.dependencies import get_db_dependency, get_active_brand_context, get_current_user


router = APIRouter(prefix="/v2", tags=["Visual Engine V2"])

# Initialize OpenAI client
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def _resolve_cleanup_level(cleanup_level: str) -> bool:
    """
    PRD Section 5.2 cleanup levels, mapped to what's actually implemented:
    - "none": no background removal — plain center-crop only.
    - "background_removal": strip the background onto the template's clean
      brand background — the "highest-leverage single step" per the PRD.
    - "reframe": content-aware smart crop instead of a blind center-crop
      (see ImagePathService._smart_crop_to_format) — handled by the caller
      passing cleanup_level through to process_uploaded_image_path_b, this
      function's boolean return is just the background-removal flag.
    - "ai_recomposite": handled entirely separately by the caller (single-post
      upload only, via process_uploaded_image_path_b_recomposite) before this
      function is ever reached. Carousel uploads don't support it yet — that's
      the one case that still reaches this function with "ai_recomposite" and
      gets rejected below, rather than silently no-op'ing.
    """
    if cleanup_level == "ai_recomposite":
        raise HTTPException(
            status_code=400,
            detail="AI re-compositing isn't available for carousel uploads yet — use it on a single-post upload instead, "
                   "or use 'background_removal'/'reframe'/'none' for this carousel."
        )
    return cleanup_level == "background_removal"


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


def _resolve_style_hint(brand_profile: Dict[str, Any]) -> Optional[str]:
    """
    Real style-prompt-fragment lookup for Path A image generation. The raw
    brand profile document has no "style_prompt_fragment" field (that never
    existed — a previous version of this code read a nonexistent key and
    silently always got None). The real fields are style_selections,
    style_rotation_index, and style_prompt_fragments (plural, list, stored
    per-selection) — reusing V1's own pick_next_style() (read-only import)
    is the same rotation logic V1's own image generation already uses.

    Deliberately read-only: unlike V1's callers, this never persists the
    advanced rotation index back onto the brand profile — V2 always reads
    the brand's current rotation position rather than owning/advancing it,
    since committing that write is V1's concern, not V2's.
    """
    style_selections = brand_profile.get("style_selections") or []
    if not style_selections:
        return None
    slug, fragment, _next_index = pick_next_style(
        style_selections=style_selections,
        rotation_index=int(brand_profile.get("style_rotation_index") or 0),
        industry=brand_profile.get("industry", ""),
        style_prompt_fragments=brand_profile.get("style_prompt_fragments") or [],
    )
    return fragment or None


def _resolve_brand_primary_color(brand_profile: Dict[str, Any]) -> Optional[str]:
    """
    Colors live in the ordered brand_colors list, not a "primary_color"
    scalar field (that field never existed on the raw document — a previous
    version of this code read it directly and always got None, so the
    brand-colored placeholder fallback always used the generic gray default
    instead of the brand's real primary color).
    """
    brand_colors = brand_profile.get("brand_colors") or []
    return brand_colors[0] if brand_colors else None


# ============================================================================
# BACKGROUND JOB HELPERS
# ============================================================================

async def _create_job(db: AsyncIOMotorDatabase, user_id: str, job_type: str) -> str:
    job_id = str(uuid.uuid4())
    await db["visual_engine_jobs"].insert_one({
        "job_id": job_id,
        "user_id": user_id,
        "type": job_type,
        "status": "pending",
        "result": None,
        "error": None,
        "created_at": datetime.utcnow(),
        "completed_at": None,
    })
    return job_id


async def _complete_job(db: AsyncIOMotorDatabase, job_id: str, result: Dict[str, Any]) -> None:
    await db["visual_engine_jobs"].update_one(
        {"job_id": job_id},
        {"$set": {"status": "completed", "result": result, "completed_at": datetime.utcnow()}}
    )


async def _fail_job(db: AsyncIOMotorDatabase, job_id: str, error: str) -> None:
    await db["visual_engine_jobs"].update_one(
        {"job_id": job_id},
        {"$set": {"status": "failed", "error": error, "completed_at": datetime.utcnow()}}
    )


# ============================================================================
# BACKGROUND JOB WORKERS
# ============================================================================

async def _job_generate_image_path_a(
    db: AsyncIOMotorDatabase, job_id: str, content_plan: str,
    style_hint: Optional[str], format: str, brand_primary: Optional[str],
    negative_space: str = "left_third"
) -> None:
    try:
        image_service = ImagePathService(openai_client)
        needs_attention = False
        error_message = None
        try:
            imagery_result = await image_service.generate_imagery_path_a(
                content_plan=content_plan, style_hint=style_hint, format=format,
                negative_space=negative_space
            )
        except ImageGenerationError as e:
            print(f"⚠️ [Path A] Falling back to brand-colored placeholder: {e}")
            needs_attention = True
            error_message = str(e)
            imagery_result = await ImagePathService.generate_placeholder_image(brand_primary, format=format)

        imagery_layer = LayerData(
            layer_type="imagery",
            data={"imagery_url": imagery_result["imagery_url"]},
            metadata={
                "path": imagery_result["path"],
                "cost": imagery_result["cost"],
                "format": format,
                "needs_attention": needs_attention,
                "error_message": error_message
            }
        )
        await _complete_job(db, job_id, {
            "success": True,
            "imagery_layer": imagery_layer.model_dump(),
            "cost": imagery_result["cost"],
            "needs_attention": needs_attention,
        })
    except Exception as e:
        print(f"❌ [Job {job_id}] generate_image_path_a failed unexpectedly: {e}")
        await _fail_job(db, job_id, str(e))


async def _job_upload_image_path_b(
    db: AsyncIOMotorDatabase, job_id: str, image_data: bytes,
    remove_background: bool, format: str, user_id: str, brand_id: str, cleanup_level: str
) -> None:
    try:
        image_hash = hash_image_bytes(image_data)
        imagery_result = await ImageCacheServiceV2.get_cached(db, user_id, brand_id, image_hash, cleanup_level, format)

        if not imagery_result:
            image_service = ImagePathService(openai_client)
            imagery_result = await image_service.process_uploaded_image_path_b(
                image_data=image_data, remove_background=remove_background, format=format, cleanup_level=cleanup_level
            )
            await ImageCacheServiceV2.store(
                db, user_id, brand_id, image_hash, cleanup_level, format, imagery_result["imagery_url"]
            )

        imagery_layer = LayerData(
            layer_type="imagery",
            data={"imagery_url": imagery_result["imagery_url"]},
            metadata={
                "path": imagery_result["path"],
                "cost": imagery_result["cost"],
                "format": format,
                "background_removed": remove_background,
                "cleanup_level": cleanup_level,
            }
        )
        await _complete_job(db, job_id, {
            "success": True,
            "imagery_layer": imagery_layer.model_dump(),
            "cost": imagery_result["cost"],
        })
    except Exception as e:
        print(f"❌ [Job {job_id}] upload_image_path_b failed unexpectedly: {e}")
        await _fail_job(db, job_id, str(e))


async def _job_upload_image_path_b_recomposite(
    db: AsyncIOMotorDatabase, job_id: str, image_data: bytes,
    content_plan: str, style_hint: Optional[str], format: str
) -> None:
    try:
        image_service = ImagePathService(openai_client)
        imagery_result = await image_service.process_uploaded_image_path_b_recomposite(
            image_data=image_data, content_plan=content_plan, style_hint=style_hint, format=format
        )
        imagery_layer = LayerData(
            layer_type="imagery",
            data={"imagery_url": imagery_result["imagery_url"]},
            metadata={
                "path": imagery_result["path"],
                "cost": imagery_result["cost"],
                "format": format,
                "cleanup_level": "ai_recomposite",
            }
        )
        await _complete_job(db, job_id, {
            "success": True,
            "imagery_layer": imagery_layer.model_dump(),
            "cost": imagery_result["cost"],
        })
    except Exception as e:
        print(f"❌ [Job {job_id}] upload_image_path_b_recomposite failed unexpectedly: {e}")
        await _fail_job(db, job_id, str(e))


async def _job_carousel_generate_images(
    db: AsyncIOMotorDatabase, job_id: str, image_briefs: List[str],
    style_hint: Optional[str], format: str, brand_primary: Optional[str], negative_space: str
) -> None:
    try:
        image_service = ImagePathService(openai_client)
        results = await image_service.generate_carousel_imagery_path_a(
            image_briefs=image_briefs, brand_primary=brand_primary, style_hint=style_hint,
            format=format, negative_space=negative_space
        )
        imagery_layers = [
            LayerData(
                layer_type="imagery",
                data={"imagery_url": r["imagery_url"]},
                metadata={
                    "path": r["path"], "cost": r["cost"], "format": format,
                    "needs_attention": r.get("needs_attention", False),
                }
            )
            for r in results
        ]
        await _complete_job(db, job_id, {
            "success": True,
            "imagery_layers": [l.model_dump() for l in imagery_layers],
            "cost": sum(r["cost"] for r in results),
        })
    except Exception as e:
        print(f"❌ [Job {job_id}] carousel_generate_images failed unexpectedly: {e}")
        await _fail_job(db, job_id, str(e))


async def _job_carousel_upload_images(
    db: AsyncIOMotorDatabase, job_id: str, images_data: List[bytes],
    carousel_count: int, remove_background: bool, format: str,
    user_id: str, brand_id: str, cleanup_level: str
) -> None:
    try:
        image_service = ImagePathService(openai_client)

        async def _clean_one(img_bytes: bytes) -> Dict[str, Any]:
            image_hash = hash_image_bytes(img_bytes)
            cached = await ImageCacheServiceV2.get_cached(db, user_id, brand_id, image_hash, cleanup_level, format)
            if cached:
                return cached
            result = await image_service.process_uploaded_image_path_b(
                image_data=img_bytes, remove_background=remove_background, format=format, cleanup_level=cleanup_level
            )
            await ImageCacheServiceV2.store(db, user_id, brand_id, image_hash, cleanup_level, format, result["imagery_url"])
            return result

        results = list(await asyncio.gather(*[_clean_one(img) for img in images_data]))
        while len(results) < carousel_count and results:
            results.append(dict(results[-1]))
        results = results[:carousel_count]

        imagery_layers = [
            LayerData(
                layer_type="imagery",
                data={"imagery_url": r["imagery_url"]},
                metadata={"path": r["path"], "cost": r["cost"], "format": format, "background_removed": remove_background}
            )
            for r in results
        ]
        await _complete_job(db, job_id, {
            "success": True,
            "imagery_layers": [l.model_dump() for l in imagery_layers],
            "cost": sum(r["cost"] for r in results),
        })
    except Exception as e:
        print(f"❌ [Job {job_id}] carousel_upload_images failed unexpectedly: {e}")
        await _fail_job(db, job_id, str(e))


async def _job_render(
    db: AsyncIOMotorDatabase, job_id: str, user_id: str, brand_id: str,
    content_layer: LayerData,
    format: str, formats: Optional[List[str]], carousel_count: int,
    imagery_layer: Optional[LayerData] = None,
    imagery_layers: Optional[List[LayerData]] = None,
) -> None:
    try:
        compositor_service = BrandCompositorService(db)
        render: VisualEngineRenderV2 = await compositor_service.compose_final_render(
            user_id=user_id,
            brand_id=brand_id,
            content_layer=content_layer,
            imagery_layer=imagery_layer,
            imagery_layers=imagery_layers,
            format=format,
            formats=formats,
            carousel_count=carousel_count
        )

        quality_gate_service = QualityGateService(db)
        quality_result = await quality_gate_service.evaluate_render(render, user_id)

        render_dict = render.model_dump()
        render_dict["_id"] = render.id
        render_dict["quality_gate_result"] = quality_result
        await db["visual_engine_renders_v2"].insert_one(render_dict)

        result: Dict[str, Any] = {
            "success": True,
            "render_id": render.id,
            "format_outputs": render.format_outputs,
            "template_id": render.typesetting_layer.data.get("template_id"),
            "style_family": render.typesetting_layer.data.get("style_family"),
            "total_cost": render.total_cost,
            "quality_gate": quality_result,
            "status": "pending_review" if quality_result["requires_review"] else "completed",
        }
        if carousel_count > 1:
            result["carousel_slides"] = render.final_outputs
            result["slide_count"] = len(render.final_outputs)
        else:
            result["final_outputs"] = render.final_outputs

        await _complete_job(db, job_id, result)
    except Exception as e:
        print(f"❌ [Job {job_id}] render failed unexpectedly: {e}")
        await _fail_job(db, job_id, str(e))


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/content-plan")
async def generate_content_plan(
    request: ContentPlanRequest,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate Layer 1: Content (headline, subtext, CTA).

    PRD Section 7: Content Layer
    Returns structured text for template filling. A single GPT-4o text call
    is fast enough to stay synchronous — no job/polling needed here.
    """
    user_id = brand_ctx["user_id"]
    brand_profile = await _require_brand_profile(user_id, brand_ctx["brand_id"], db)

    content_service = ContentLayerService(openai_client)
    content_layer = await content_service.generate_content_plan(
        seed_content=request.seed_content,
        brand_context=BrandProfileService.to_brand_context(brand_profile),
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
    background_tasks: BackgroundTasks,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate Layer 2: Imagery (Path A - GPT Image 2).

    PRD Section 8: Imagery Layer - Path A. Returns a job_id immediately;
    poll GET /v2/jobs/{job_id} for the result.
    """
    brand_profile = await _require_brand_profile(brand_ctx["user_id"], brand_ctx["brand_id"], db)
    style_hint = _resolve_style_hint(brand_profile)

    job_id = await _create_job(db, brand_ctx["user_id"], "generate_image")
    background_tasks.add_task(
        _job_generate_image_path_a,
        db, job_id, request.content_plan, style_hint, request.format, _resolve_brand_primary_color(brand_profile),
        request.negative_space
    )

    return {"success": True, "job_id": job_id, "status": "pending"}


@router.post("/upload-image")
async def upload_image_path_b(
    background_tasks: BackgroundTasks,
    format: str = Form("1:1"),
    cleanup_level: str = Form("background_removal"),
    content_plan: Optional[str] = Form(None),
    image_file: UploadFile = File(...),
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Upload Layer 2: Imagery (Path B - User upload).

    PRD Section 8: Imagery Layer - Path B. cleanup_level: "none",
    "background_removal" (default), "reframe" (content-aware crop), or
    "ai_recomposite" (premium — preserves the real product pixel-exact,
    generates a new scene around it via background-removal + Path A
    generation + compositing; requires content_plan describing the new
    scene, and always routes to mandatory review per PRD Section 13).
    Returns a job_id immediately; poll GET /v2/jobs/{job_id} for the result.
    """
    brand_profile = await _require_brand_profile(brand_ctx["user_id"], brand_ctx["brand_id"], db)
    image_data = await image_file.read()

    if cleanup_level == "ai_recomposite":
        if not content_plan or not content_plan.strip():
            raise HTTPException(
                status_code=400,
                detail="content_plan is required when cleanup_level='ai_recomposite' — describe the new scene to generate around the product."
            )
        style_hint = _resolve_style_hint(brand_profile)
        job_id = await _create_job(db, brand_ctx["user_id"], "upload_image")
        background_tasks.add_task(
            _job_upload_image_path_b_recomposite, db, job_id, image_data, content_plan.strip(), style_hint, format
        )
        return {"success": True, "job_id": job_id, "status": "pending"}

    remove_background = _resolve_cleanup_level(cleanup_level)
    job_id = await _create_job(db, brand_ctx["user_id"], "upload_image")
    background_tasks.add_task(
        _job_upload_image_path_b, db, job_id, image_data, remove_background, format,
        brand_ctx["user_id"], brand_ctx["brand_id"], cleanup_level
    )

    return {"success": True, "job_id": job_id, "status": "pending"}


@router.post("/carousel-content-plan")
async def generate_carousel_content_plan(
    request: CarouselContentPlanRequest,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate carousel Layer 1: the full per-slide narrative arc in one call.

    PRD Section 9.1. Returns content_layer.data.slides[] — feed the
    image_brief from each slide into POST /v2/carousel-generate-images
    (Path A) or upload one photo per slide to POST /v2/carousel-upload-images
    (Path B), then pass both into POST /v2/render-carousel.
    """
    user_id = brand_ctx["user_id"]
    brand_profile = await _require_brand_profile(user_id, brand_ctx["brand_id"], db)

    content_service = ContentLayerService(openai_client)
    content_layer = await content_service.generate_carousel_content_plan(
        seed_content=request.seed_content,
        brand_context=BrandProfileService.to_brand_context(brand_profile),
        carousel_count=request.carousel_count,
        post_intent=request.post_intent,
        platform=(request.platforms[0] if request.platforms else "instagram")
    )

    return {
        "success": True,
        "content_layer": content_layer.model_dump(),
        "cost": content_layer.metadata.get("cost", 0.0)
    }


@router.post("/carousel-generate-images")
async def carousel_generate_images_path_a(
    request: CarouselGenerateImagesRequest,
    background_tasks: BackgroundTasks,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Generate carousel Layer 2 (Path A): one independent GPT Image 2
    generation per slide brief, run concurrently. PRD Section 9.1 — never
    one image reused across every slide. Returns a job_id immediately;
    poll GET /v2/jobs/{job_id} for the result (imagery_layers: List[LayerData]).
    """
    brand_profile = await _require_brand_profile(brand_ctx["user_id"], brand_ctx["brand_id"], db)
    style_hint = _resolve_style_hint(brand_profile)

    job_id = await _create_job(db, brand_ctx["user_id"], "carousel_generate_images")
    background_tasks.add_task(
        _job_carousel_generate_images,
        db, job_id, request.image_briefs, style_hint, request.format,
        _resolve_brand_primary_color(brand_profile), request.negative_space
    )

    return {"success": True, "job_id": job_id, "status": "pending"}


@router.post("/carousel-upload-images")
async def carousel_upload_images_path_b(
    background_tasks: BackgroundTasks,
    carousel_count: int = Form(...),
    format: str = Form("1:1"),
    cleanup_level: str = Form("background_removal"),
    image_files: List[UploadFile] = File(...),
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Upload carousel Layer 2 (Path B): one photo per slide. PRD Section 9.1 —
    if fewer photos are uploaded than there are slides, the last uploaded
    photo repeats for the remaining slides ("repeats a hero image where
    appropriate"). Returns a job_id immediately; poll GET /v2/jobs/{job_id}.
    """
    await _require_brand_profile(brand_ctx["user_id"], brand_ctx["brand_id"], db)
    remove_background = _resolve_cleanup_level(cleanup_level)

    images_data = [await f.read() for f in image_files]

    job_id = await _create_job(db, brand_ctx["user_id"], "carousel_upload_images")
    background_tasks.add_task(
        _job_carousel_upload_images, db, job_id, images_data, carousel_count, remove_background, format,
        brand_ctx["user_id"], brand_ctx["brand_id"], cleanup_level
    )

    return {"success": True, "job_id": job_id, "status": "pending"}


@router.post("/render")
async def render_full_composition(
    request: RenderRequest,
    background_tasks: BackgroundTasks,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Full 4-layer render: Content → Imagery → Brand → Typesetting.

    PRD Section 11: Four-Layer Architecture. Returns a job_id immediately;
    poll GET /v2/jobs/{job_id} for the result.
    """
    user_id = brand_ctx["user_id"]
    await _require_brand_profile(user_id, brand_ctx["brand_id"], db)

    content_layer = LayerData(**request.content_layer)
    imagery_layer = LayerData(**request.imagery_layer)

    job_id = await _create_job(db, user_id, "render")
    background_tasks.add_task(
        _job_render, db, job_id, user_id, brand_ctx["brand_id"],
        content_layer, request.format, request.formats, 1,
        imagery_layer,
    )

    return {"success": True, "job_id": job_id, "status": "pending"}


@router.post("/render-carousel")
async def render_carousel(
    request: CarouselRenderRequest,
    background_tasks: BackgroundTasks,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Multi-slide carousel render.

    PRD Section 9: Carousel Posts. content_layer must carry a per-slide
    narrative (data.slides[], from POST /v2/carousel-content-plan) and
    imagery_layers one independently-produced image per slide (from
    POST /v2/carousel-generate-images or /v2/carousel-upload-images).
    Returns a job_id immediately; poll GET /v2/jobs/{job_id} for the result.
    """
    user_id = brand_ctx["user_id"]
    await _require_brand_profile(user_id, brand_ctx["brand_id"], db)

    content_layer = LayerData(**request.content_layer)
    imagery_layers = [LayerData(**layer) for layer in request.imagery_layers]

    job_id = await _create_job(db, user_id, "render_carousel")
    background_tasks.add_task(
        _job_render, db, job_id, user_id, brand_ctx["brand_id"],
        content_layer, request.format, request.formats, request.carousel_count,
        None, imagery_layers,
    )

    return {"success": True, "job_id": job_id, "status": "pending"}


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Poll a background job started by generate-image/upload-image/render/
    render-carousel. status is "pending" | "completed" | "failed"; `result`
    is populated (matching that endpoint's old synchronous response shape)
    once status is "completed".
    """
    job = await db["visual_engine_jobs"].find_one({"job_id": job_id, "user_id": brand_ctx["user_id"]})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "success": True,
        "job_id": job_id,
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
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


@router.get("/brand-prefs")
async def get_brand_prefs(
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    This brand's V2-only rendering preferences: the auto-derived (or
    overridden) style_family, the logo control mode, and a recommended
    imagery path (PRD Section 10.1). Stored in V2's own
    visual_engine_v2_brand_prefs collection — never the shared brand profile.
    """
    user_id = brand_ctx["user_id"]
    brand_id = brand_ctx["brand_id"]
    brand_profile = await _require_brand_profile(user_id, brand_id, db)
    context = BrandProfileService.to_brand_context(brand_profile)

    prefs = await BrandPrefsServiceV2.get_or_create(
        db, user_id=user_id, brand_id=brand_id,
        style_selections=context.get("style_selections"),
        industry=context.get("industry"),
    )
    has_product_images = await ImageCacheServiceV2.has_any_for_brand(db, user_id, brand_id)

    return {
        "success": True,
        "style_family": prefs.get("style_family"),
        "style_family_override": prefs.get("style_family_override", False),
        "logo_control_mode": prefs.get("logo_control_mode", "agent"),
        "logo_manual_position": prefs.get("logo_manual_position"),
        "has_product_images": has_product_images,
        "recommended_image_path": "B" if has_product_images else "A",
    }


@router.put("/brand-prefs")
async def update_brand_prefs(
    request: BrandPrefsUpdateRequest,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Update this brand's V2-only preferences.

    logo_control_mode='user' requires logo_manual_position to be set (either
    in this same call or a prior one) — the frontend should show the
    tradeoff warning ("reduced template-fit guarantees") before letting the
    user pick this mode.
    """
    user_id = brand_ctx["user_id"]
    brand_id = brand_ctx["brand_id"]

    if request.logo_control_mode == "user" and not request.logo_manual_position:
        existing = await db["visual_engine_v2_brand_prefs"].find_one({"user_id": user_id, "brand_id": brand_id})
        if not existing or not existing.get("logo_manual_position"):
            raise HTTPException(
                status_code=400,
                detail="logo_manual_position is required when switching logo_control_mode to 'user'"
            )

    try:
        prefs = await BrandPrefsServiceV2.update_prefs(
            db, user_id=user_id, brand_id=brand_id,
            logo_control_mode=request.logo_control_mode,
            logo_manual_position=request.logo_manual_position,
            style_family=request.style_family,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "success": True,
        "style_family": prefs.get("style_family"),
        "style_family_override": prefs.get("style_family_override", False),
        "logo_control_mode": prefs.get("logo_control_mode", "agent"),
        "logo_manual_position": prefs.get("logo_manual_position"),
    }


@router.get("/metrics")
async def get_metrics(
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    PRD Section 16: success metrics, computed from this user's stored V2
    renders (visual_engine_renders_v2) — no separate instrumentation
    pipeline. Scoped to the authenticated user, not global/cross-tenant.
    """
    metrics = await VisualEngineMetricsServiceV2.compute(db, user_id=brand_ctx["user_id"])
    return {"success": True, **metrics}
