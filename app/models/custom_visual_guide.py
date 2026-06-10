# app/models/custom_visual_guide.py

"""
Custom Visual Guide Model
Represents user-uploaded reference images analyzed for aesthetic and typography.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
from pydantic import BaseModel, Field


class AestheticProfile(BaseModel):
    """Aesthetic extraction result from GPT-4o-mini Vision"""
    visual_genre: str
    quality_benchmark: str
    camera_style: str
    color_palette: Dict[str, Any]
    lighting: Dict[str, Any]
    composition: Dict[str, Any]
    texture_and_atmosphere: Dict[str, Any]
    mood: Dict[str, Any]
    anti_aesthetic: List[str]
    subject_treatment: str


class TypographyCharacter(BaseModel):
    """Typography character extraction from reference"""
    style_class: str  # serif | sans-serif | display | script | mono | mixed
    subclass: Optional[str] = None
    weight_visual: str  # thin | light | regular | medium | bold | black
    contrast: str  # low | medium | high | extreme
    width: str  # condensed | normal | wide
    personality: str  # one word
    energy_level: int  # 1-10
    use_case_alignment: List[str]
    decorative_level: str  # functional | semi-decorative | highly-decorative


class FontMatch(BaseModel):
    """Individual font match result"""
    font_id: str
    font_name: str
    match_score: int
    match_confidence: str  # high | medium | low
    source: str  # library | user_upload


class IdentifiedFont(BaseModel):
    """Named font identification (e.g., "Bebas Neue")"""
    name: str
    confidence: str  # high | medium


class NextStepSuggestion(BaseModel):
    """User guidance for match outcome"""
    type: str  # use_match | use_match_with_caveat | upload_identified | upload_descriptive | use_brand_default_decorative
    message: str
    actionable_link: Optional[str] = None


class TypographyExtraction(BaseModel):
    """Complete typography analysis result"""
    has_typography: bool
    typography_character: Optional[TypographyCharacter] = None
    matching_priority_traits: Optional[List[str]] = None
    identified_font: Optional[IdentifiedFont] = None


class MetadataTags(BaseModel):
    """11-dimension metadata tags (V3 ontology)"""
    energy_level: int  # 1-10
    formality: str  # luxury | casual
    tone: str  # playful | serious
    density: str  # maximalist | minimalist | balanced
    treatment: str  # documentary | stylized
    subject_focus: str  # human-centered | product-centered
    visual_mode: str  # cinematic | graphic
    audience_register: str  # premium | meme-native
    composition_density: str  # dense | spacious | balanced
    emotion: str
    intent_tags: List[str]
    minimal: bool
    sensitive_content_safe: bool


class CustomVisualGuide(BaseModel):
    """Main custom visual guide document"""
    id: Optional[str] = None
    user_id: str
    brand_id: Optional[str] = None
    name: str

    # Source image
    original_image_url: str
    original_image_hash: str
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)

    # Aesthetic extraction
    aesthetic_profile: Dict[str, Any]  # AestheticProfile as dict
    prompt_fragment: str

    # Typography extraction and matching
    typography_extraction: Optional[Dict[str, Any]] = None  # TypographyExtraction as dict
    match_outcome: str  # STRONG_MATCH | DECENT_MATCH | WEAK_MATCH | NO_RECOMMENDED_MATCH | NO_MATCH | NO_TYPOGRAPHY | DECORATIVE_ACCEPTED
    matched_font_id: Optional[str] = None
    matched_font_source: Optional[str] = None  # library | user_upload
    match_confidence: Optional[str] = None  # high | medium | low
    alternative_font_matches: Optional[List[Dict[str, Any]]] = None  # List of FontMatch as dict
    identified_font_name: Optional[str] = None
    next_step_suggestion: Optional[Dict[str, Any]] = None  # NextStepSuggestion as dict

    # Metadata
    metadata_tags: Dict[str, Any]  # MetadataTags as dict

    # Tracking
    times_used: int = 0
    times_font_applied: int = 0
    times_user_uploaded_suggested_font: int = 0
    last_used_at: Optional[datetime] = None
    avg_user_rating: Optional[float] = None

    # Lifecycle
    status: str = "active"  # active | archived
    archived_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class GuideUsageEvent(BaseModel):
    """Track when and how guides are used"""
    id: Optional[str] = None
    guide_id: str
    campaign_id: Optional[str] = None
    applied_matched_font: bool = False
    font_used_id: Optional[str] = None
    used_at: datetime = Field(default_factory=datetime.utcnow)
    user_rating: Optional[int] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
