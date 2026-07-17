"""
Visual Content Engine V2 - Pydantic Models
4-layer compositing architecture as per PRD
"""

from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field
from datetime import datetime
from uuid import uuid4


# ============================================================================
# LAYER WRAPPER
# ============================================================================

class LayerData(BaseModel):
    """
    Generic wrapper for one layer's output: the layer's own data plus
    metadata about how it was produced (cost, model used, timestamps, etc).
    Used for content/imagery/brand/typesetting layers alike.
    """
    layer_type: str
    data: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# REQUEST MODELS
# ============================================================================

class ContentPlanRequest(BaseModel):
    """Request to plan content (AI text layer). Brand is resolved server-side from
    the authenticated user's active brand context — never passed by the client,
    matching every other endpoint in this app."""
    seed_content: str = Field(..., description="Topic or brief for content")
    platforms: List[str] = Field(..., description="Target platforms")
    post_intent: str = Field(..., description="sale, product, announcement, testimonial, educational")
    carousel_slides: int = Field(1, description="Number of slides (1 for single post)")


class GenerateImageRequest(BaseModel):
    """Path A: Generate imagery-only via GPT Image 2"""
    content_plan: str = Field(..., description="What to generate")
    negative_space: str = Field("left_third", description="Where to leave space for text")
    format: str = Field("1:1", description="Aspect ratio: 1:1, 4:5, or 9:16")


class UploadImageRequest(BaseModel):
    """Path B: Upload and clean user image"""
    image_url: str = Field(..., description="User's uploaded image URL")
    cleanup_level: Literal["none", "background_removal", "reframe", "ai_recomposite"] = "background_removal"


class CarouselContentPlanRequest(BaseModel):
    """PRD Section 9.1: one AI call plans the whole carousel's narrative arc."""
    seed_content: str = Field(..., description="Topic or brief for the carousel")
    platforms: List[str] = Field(..., description="Target platforms")
    post_intent: str = Field("carousel", description="sale, product, announcement, testimonial, educational, carousel")
    carousel_count: int = Field(3, ge=2, le=10, description="Number of slides, 2-10 per PRD Section 9")


class CarouselGenerateImagesRequest(BaseModel):
    """Path A carousel: one independent GPT Image 2 generation per slide brief."""
    image_briefs: List[str] = Field(..., min_length=2, max_length=10, description="One image brief per slide, from the carousel content plan")
    format: str = Field("1:1", description="Aspect ratio: 1:1, 4:5, or 9:16")
    negative_space: str = Field("left_third", description="Where to leave space for text on each slide")


class BrandPrefsUpdateRequest(BaseModel):
    """
    V2-only per-brand preferences — stored in visual_engine_v2_brand_prefs,
    never on the shared brand_profiles document.
    """
    logo_control_mode: Optional[Literal["agent", "user"]] = Field(
        None, description="'agent' = Orshot places the logo natively in-template; "
                          "'user' = render without a logo, then composite at logo_manual_position"
    )
    logo_manual_position: Optional[
        Literal["top_left", "top_right", "top_center", "bottom_left", "bottom_right", "bottom_center", "center"]
    ] = Field(None, description="Required when logo_control_mode='user'")
    style_family: Optional[
        Literal[
            "bold_modern", "minimal_clean", "modern_professional", "educational",
            "testimonial_social_proof", "playful_colorful", "elegant_luxury"
        ]
    ] = Field(None, description="Manual override of the auto-derived template style family")


class RenderRequest(BaseModel):
    """4-layer compositor render request"""
    content_layer: Dict[str, Any] = Field(..., description="headline, subhead, promo, cta")
    imagery_layer: Dict[str, Any] = Field(..., description="path, image_url, source")

    # Options
    format: str = Field("1:1", description="Aspect ratio to render")
    formats: Optional[List[str]] = Field(
        None, description="One or more aspect ratios to render (PRD Section 14 multi-format); overrides `format` when set"
    )
    require_review: bool = Field(False, description="Force human review")


class CarouselRenderRequest(BaseModel):
    """
    Multi-slide carousel render. content_layer must carry a per-slide
    narrative (data.slides[], from POST /v2/carousel-content-plan) and
    imagery_layers must carry one independently-generated/uploaded image per
    slide (from POST /v2/carousel-generate-images or /v2/carousel-upload-images)
    — PRD Section 9.1, never one flat plan/image fragmented across slides.
    """
    content_layer: Dict[str, Any] = Field(..., description="Carousel content plan with data.slides[]")
    imagery_layers: List[Dict[str, Any]] = Field(..., min_length=1, description="Per-slide imagery layers, same order as content_layer.data.slides")
    format: str = Field("1:1", description="Aspect ratio to render")
    formats: Optional[List[str]] = Field(
        None, description="One or more aspect ratios to render (PRD Section 14 multi-format); overrides `format` when set"
    )
    carousel_count: int = Field(3, ge=2, le=10, description="Number of slides, 2-10 per PRD Section 9")


# ============================================================================
# RESPONSE MODELS
# ============================================================================

class ContentPlanResponse(BaseModel):
    """AI content planning result"""
    status: bool
    content: Dict[str, Any]  # {headline, subhead, promo, cta, image_brief}
    carousel_slides: Optional[List[Dict[str, Any]]] = None  # If carousel
    token_cost: float


class ImageGenerationResponse(BaseModel):
    """Path A generation result"""
    status: bool
    image_url: str
    path: Literal["path_a"] = "path_a"
    cost_usd: float


class ImageUploadResponse(BaseModel):
    """Path B upload/cleanup result"""
    status: bool
    original_url: str
    cleaned_url: str
    path: Literal["path_b"] = "path_b"
    cleanup_applied: str
    cost_usd: float


class RenderResponse(BaseModel):
    """Template render result"""
    status: bool
    render_id: str
    render_urls: Dict[str, str]  # {format: url}
    cost_usd: float
    confidence_score: float
    review_required: bool
    review_reason: Optional[str] = None


# ============================================================================
# DATABASE MODELS
# ============================================================================

class VisualEngineRenderV2(BaseModel):
    """V2 render job stored in DB"""
    id: str = Field(default_factory=lambda: str(uuid4()), description="Unique render ID")
    user_id: str
    brand_profile_id: str

    # 4-layer data
    content_layer: LayerData      # {headline, subhead, promo, cta}
    imagery_layer: LayerData      # {path: "A"|"B", image_url, source}
    brand_layer: LayerData        # {logo_url, primary_color, font}
    typesetting_layer: LayerData  # {template_id, rendered_urls, format, carousel_count}

    # Output
    final_outputs: List[str] = Field(default_factory=list)  # rendered URL(s) in the primary format; 1 for single post, N for carousel slides
    format_outputs: Dict[str, List[str]] = Field(default_factory=dict)  # PRD Section 14: every requested aspect-ratio format, keyed by "1:1"/"4:5"/"9:16"
    status: Literal[
        "planning", "rendering", "review", "approved", "rejected", "completed",
        "scheduled", "published", "publish_failed"
    ] = "planning"
    content_draft_id: Optional[str] = None  # links to the content_drafts document once bridged to the real posting pipeline

    # Quality gate
    confidence_score: float = 0.0
    review_required: bool = False
    review_reason: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None

    # PRD Section 13: tiered review model
    review_tier: Literal["auto", "soft", "mandatory"] = "auto"
    review_expires_at: Optional[datetime] = None  # soft tier: auto-approves if not rejected by this time

    # PRD Section 12: failure handling — never post a broken asset, never fail silently
    needs_attention: bool = False
    error_message: Optional[str] = None
    used_fallback_background: bool = False  # true if a brand-colored placeholder replaced a failed render

    # Cost tracking
    cost_breakdown: Dict[str, float] = Field(default_factory=dict)
    total_cost: float = 0.0

    # Metadata
    post_intent: str = Field("general", description="sale, product, announcement, etc")
    platforms: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    rendered_at: Optional[datetime] = None
    published_at: Optional[datetime] = None

    # Carousel
    is_carousel: bool = False
    carousel_slide_index: Optional[int] = None
    carousel_parent_id: Optional[str] = None


class VisualEngineTemplateV2(BaseModel):
    """Template library entry"""
    template_id: str = Field(..., description="Unique template ID")
    name: str = Field(..., description="Human-readable template name")
    style_family: str = Field(..., description="Maps to Visual Style Guide")
    format: Literal["1:1", "4:5", "9:16"] = Field(..., description="Aspect ratio")
    post_intent: str = Field(..., description="sale, product, announcement, testimonial, educational")
    image_path: Literal["path_a", "path_b", "both"] = Field(..., description="Which image path it supports")

    # Vendor integration
    orshot_template_id: Optional[str] = Field(None, description="Orshot template ID")
    placid_template_id: Optional[str] = Field(None, description="Placid template ID (fallback)")

    # Template slots (what can be injected)
    slots: Dict[str, str] = Field(default_factory=dict)  # {slot_name: slot_type}

    # Preview
    preview_url: Optional[str] = Field(None)
    thumbnail_url: Optional[str] = Field(None)

    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = True
    version: int = 1


class VisualEngineReviewQueueV2(BaseModel):
    """Review queue entry"""
    queue_id: str = Field(default_factory=lambda: str(uuid4()))
    render_id: str
    user_id: str
    brand_profile_id: str
    review_tier: Literal["soft", "mandatory"] = "soft"

    # Why it needs review
    review_reason: str = ""
    quality_score: float
    detected_issues: List[str] = Field(default_factory=list)  # ["incomplete_profile", "low_image_quality"]

    # Preview
    preview_url: str = ""
    content_preview: Dict[str, Any] = Field(default_factory=dict)

    # Status
    status: Literal["pending", "approved", "rejected"] = "pending"
    assigned_to: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    reviewer_notes: Optional[str] = None

    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    priority: int = 0  # Higher = more urgent


class VisualEngineConfigV2(BaseModel):
    """V2 configuration and feature flags"""
    enabled: bool = True
    beta_users: List[str] = Field(default_factory=list)

    # Vendor settings
    orshot_api_key: Optional[str] = None
    orshot_enabled: bool = True
    placid_api_key: Optional[str] = None
    placid_enabled: bool = False  # Fallback

    # Quality gate thresholds
    min_confidence_auto_publish: float = 0.85
    require_review_first_n_posts: int = 3

    # Cost caps
    max_cost_per_render_usd: float = 0.50

    # Feature toggles
    carousel_enabled: bool = True
    multi_format_enabled: bool = True
    ai_recomposite_enabled: bool = False  # Premium, opt-in
