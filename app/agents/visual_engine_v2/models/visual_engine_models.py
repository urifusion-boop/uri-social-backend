"""
Visual Content Engine V2 - Pydantic Models
4-layer compositing architecture as per PRD
"""

from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field
from datetime import datetime


# ============================================================================
# REQUEST MODELS
# ============================================================================

class ContentPlanRequest(BaseModel):
    """Request to plan content (AI text layer)"""
    seed_content: str = Field(..., description="Topic or brief for content")
    platforms: List[str] = Field(..., description="Target platforms")
    post_intent: str = Field(..., description="sale, product, announcement, testimonial, educational")
    brand_id: Optional[str] = Field(None, description="Brand ID for context")
    carousel_slides: int = Field(1, description="Number of slides (1 for single post)")


class GenerateImageRequest(BaseModel):
    """Path A: Generate imagery-only via GPT Image 2"""
    content_brief: str = Field(..., description="What to generate")
    negative_space: str = Field("left_third", description="Where to leave space for text")
    size: str = Field("1024x1024", description="Image dimensions")
    brand_id: Optional[str] = Field(None)


class UploadImageRequest(BaseModel):
    """Path B: Upload and clean user image"""
    image_url: str = Field(..., description="User's uploaded image URL")
    cleanup_level: Literal["none", "background_removal", "reframe", "ai_recomposite"] = "background_removal"
    brand_id: Optional[str] = Field(None)


class RenderRequest(BaseModel):
    """4-layer compositor render request"""
    content_layer: Dict[str, Any] = Field(..., description="headline, subhead, promo, cta")
    imagery_layer: Dict[str, Any] = Field(..., description="path, image_url, source")
    brand_layer: Dict[str, Any] = Field(..., description="logo_url, primary_color, font")
    template_layer: Dict[str, Any] = Field(..., description="template_id, format, style_family")

    # Options
    formats: List[str] = Field(["1:1"], description="Aspect ratios to render")
    require_review: bool = Field(False, description="Force human review")


class CarouselRenderRequest(BaseModel):
    """Multi-slide carousel render"""
    slides: List[RenderRequest] = Field(..., description="Render request per slide")
    brand_id: str = Field(..., description="Brand ID")


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
    id: str = Field(..., description="Unique render ID")
    user_id: str
    brand_id: str

    # 4-layer data
    content_layer: Dict[str, Any]  # {headline, subhead, promo, cta}
    imagery_layer: Dict[str, Any]  # {path: "A"|"B", image_url, source}
    brand_layer: Dict[str, Any]    # {logo_url, primary_color, font}
    template_layer: Dict[str, Any] # {template_id, format, version, style_family}

    # Output
    final_render_urls: Dict[str, str] = Field(default_factory=dict)  # {format: url}
    status: Literal["planning", "rendering", "review", "approved", "rejected", "published"] = "planning"

    # Quality gate
    confidence_score: float = 0.0
    review_required: bool = False
    review_reason: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None

    # Cost tracking
    cost_breakdown: Dict[str, float] = Field(default_factory=dict)
    total_cost_usd: float = 0.0

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
    queue_id: str
    render_id: str
    user_id: str
    brand_id: str

    # Why it needs review
    review_reason: str
    confidence_score: float
    auto_flags: List[str] = Field(default_factory=list)  # ["incomplete_profile", "low_image_quality"]

    # Preview
    preview_url: str
    content_preview: Dict[str, Any]

    # Status
    status: Literal["pending", "approved", "rejected"] = "pending"
    assigned_to: Optional[str] = None
    reviewed_at: Optional[datetime] = None

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
