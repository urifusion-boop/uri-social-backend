# app/models/ai_prompt_template.py

from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field
from beanie import Document
from bson import ObjectId


class PromptSection(BaseModel):
    """Individual section of a prompt template"""
    name: str  # "FORMAT", "STYLE", "COMPOSITION", etc.
    content: List[str]  # List of instructions/guidelines


class PromptTemplate(Document):
    """
    AI Image Generation Prompt Template

    Stores structured prompt templates for marketing image generation.
    Compatible with existing image_content_service.py DALL-E integration.
    """

    template_id: str = Field(unique=True, index=True)
    name: str  # "Modern Doodle Collage", "Minimalist Editorial", etc.
    description: str  # Short description of the template style
    category: str  # "fashion", "product", "food", "beverage", "editorial"

    # Template sections - structured format
    sections: List[PromptSection] = []

    # Format specifications
    default_aspect_ratio: str = "1:1"  # "1:1", "4:5", "16:9", "9:16"
    default_size: str = "1024x1024"  # Compatible with DALL-E size format

    # Template variables that can be replaced
    # e.g., [PRODUCT_NAME], [BRAND], [COLOR_SCHEME]
    variables: List[str] = []

    # Example outputs (for frontend preview)
    example_images: List[str] = []  # URLs to example generated images

    # Metadata
    is_active: bool = True
    is_premium: bool = False  # Premium templates for paid users
    usage_count: int = 0

    workspace_id: Optional[str] = None  # For workspace-specific templates
    created_by: Optional[str] = None  # User ID who created custom template

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "ai_prompt_templates"
        indexes = [
            "template_id",
            "category",
            "is_active",
            "workspace_id",
        ]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for API responses"""
        return {
            "id": str(self.id),
            "template_id": self.template_id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "default_aspect_ratio": self.default_aspect_ratio,
            "default_size": self.default_size,
            "variables": self.variables,
            "example_images": self.example_images,
            "is_premium": self.is_premium,
            "usage_count": self.usage_count,
            "created_at": self.created_at.isoformat(),
        }


class AIImageGeneration(Document):
    """
    Track AI marketing image generations

    Stores history of generated images for analytics and billing.
    """

    user_id: str  # Reference to User
    workspace_id: Optional[str] = None

    template_id: str  # Which template was used
    template_name: str  # Snapshot of template name

    # Generation inputs
    prompt: str  # Complete generated prompt sent to AI
    variables: Dict[str, str] = {}  # Variables filled in by user
    size: str = "1024x1024"
    aspect_ratio: str = "1:1"

    # Generation outputs
    image_url: Optional[str] = None  # Cloudinary URL after upload
    dalle_url: Optional[str] = None  # Original DALL-E URL
    status: str = "pending"  # pending, completed, failed
    error_message: Optional[str] = None

    # Provider info
    provider: str = "dall-e-3"  # dall-e-3, dall-e-2, midjourney, etc.
    model: str = "dall-e-3"

    # Cost tracking
    cost_credits: float = 0.0  # Credits deducted from user
    cost_usd: float = 0.0  # Actual API cost

    # Metadata
    generation_time_ms: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "ai_image_generations"
        indexes = [
            "user_id",
            "workspace_id",
            "template_id",
            "status",
            "created_at",
        ]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for API responses"""
        return {
            "id": str(self.id),
            "template_id": self.template_id,
            "template_name": self.template_name,
            "image_url": self.image_url,
            "status": self.status,
            "error_message": self.error_message,
            "prompt": self.prompt,
            "variables": self.variables,
            "size": self.size,
            "aspect_ratio": self.aspect_ratio,
            "provider": self.provider,
            "cost_credits": self.cost_credits,
            "created_at": self.created_at.isoformat(),
        }
