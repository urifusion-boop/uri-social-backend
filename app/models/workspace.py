"""
Workspace Model - Team/Project within a Client

Represents a team or project within a client organization.
Each workspace has its own:
- Team members with roles
- Brand profile
- Content and social connections
- Isolated data from other workspaces
"""

from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from bson import ObjectId
import secrets


class WorkspaceSettings(BaseModel):
    """Settings for a workspace"""
    auto_publish_enabled: bool = Field(default=False)
    content_approval_required: bool = Field(default=True)
    default_content_tone: str = Field(default="professional")
    allowed_platforms: List[str] = Field(default_factory=lambda: ["linkedin", "facebook", "instagram", "twitter"])
    timezone: str = Field(default="UTC")
    language: str = Field(default="en")


class WorkspaceUsageStats(BaseModel):
    """Usage statistics for a workspace"""
    total_content_generated: int = Field(default=0, ge=0)
    total_images_generated: int = Field(default=0, ge=0)
    total_posts_published: int = Field(default=0, ge=0)
    total_drafts_created: int = Field(default=0, ge=0)

    # Current month
    content_generated_this_month: int = Field(default=0, ge=0)
    images_generated_this_month: int = Field(default=0, ge=0)
    posts_published_this_month: int = Field(default=0, ge=0)

    last_activity_at: Optional[datetime] = None


class Workspace(BaseModel):
    """
    Workspace Model - Represents a team/project within a client

    A workspace provides data isolation and team collaboration.
    Each workspace has:
    - Team members with specific roles
    - Own brand profile
    - Own social media connections
    - Own content and drafts
    - Usage tracking
    """

    id: Optional[str] = Field(default=None, alias="_id")
    workspace_id: str = Field(..., description="Unique workspace identifier (wsp_xxxxx)")
    client_id: str = Field(..., description="Parent client ID")

    # Basic info
    name: str = Field(..., min_length=1, max_length=100, description="Workspace name")
    slug: str = Field(..., pattern="^[a-z0-9-]+$", description="URL-friendly identifier")
    description: Optional[str] = Field(None, max_length=500)
    avatar_url: Optional[str] = None

    # Ownership
    created_by_user_id: str = Field(..., description="User who created this workspace")

    # Brand profile (optional - can be set per workspace)
    default_brand_profile_id: Optional[str] = Field(None, description="Default brand profile for this workspace")

    # Settings
    settings: WorkspaceSettings = Field(default_factory=WorkspaceSettings)

    # Usage & Stats
    usage_stats: WorkspaceUsageStats = Field(default_factory=WorkspaceUsageStats)

    # Status
    status: str = Field(default="active", pattern="^(active|archived|deleted)$")

    # Metadata
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    archived_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {
            ObjectId: str,
            datetime: lambda v: v.isoformat()
        }

    @staticmethod
    def generate_workspace_id() -> str:
        """
        Generate a unique workspace ID
        Format: wsp_<random_16_chars>
        """
        random_part = secrets.token_urlsafe(12)[:16]
        return f"wsp_{random_part}"

    @staticmethod
    def generate_slug(name: str, existing_slugs: Optional[List[str]] = None) -> str:
        """
        Generate URL-friendly slug from name
        Example: "Marketing Team" -> "marketing-team"

        If slug exists, append number: "marketing-team-2"
        """
        slug = name.lower().strip()
        slug = slug.replace(" ", "-")
        slug = "".join(c for c in slug if c.isalnum() or c == "-")
        slug = "-".join(filter(None, slug.split("-")))
        slug = slug[:50]

        # Handle duplicates
        if existing_slugs and slug in existing_slugs:
            counter = 2
            while f"{slug}-{counter}" in existing_slugs:
                counter += 1
            slug = f"{slug}-{counter}"

        return slug

    def increment_usage(self, metric: str, amount: int = 1):
        """Increment usage statistics"""
        metric_map = {
            "content": ("total_content_generated", "content_generated_this_month"),
            "image": ("total_images_generated", "images_generated_this_month"),
            "post": ("total_posts_published", "posts_published_this_month"),
            "draft": ("total_drafts_created", None),
        }

        if metric in metric_map:
            total_field, monthly_field = metric_map[metric]
            current_total = getattr(self.usage_stats, total_field)
            setattr(self.usage_stats, total_field, current_total + amount)

            if monthly_field:
                current_monthly = getattr(self.usage_stats, monthly_field)
                setattr(self.usage_stats, monthly_field, current_monthly + amount)

        self.usage_stats.last_activity_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def reset_monthly_usage(self):
        """Reset monthly usage counters"""
        self.usage_stats.content_generated_this_month = 0
        self.usage_stats.images_generated_this_month = 0
        self.usage_stats.posts_published_this_month = 0
        self.updated_at = datetime.utcnow()

    def archive(self):
        """Archive the workspace"""
        self.status = "archived"
        self.archived_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def unarchive(self):
        """Unarchive the workspace"""
        self.status = "active"
        self.archived_at = None
        self.updated_at = datetime.utcnow()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage"""
        data = self.model_dump(by_alias=True, exclude_none=True)
        if self.id:
            data["_id"] = ObjectId(self.id)
        return data

    def to_public_dict(self) -> Dict[str, Any]:
        """Convert to public dictionary (safe for API responses)"""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "client_id": self.client_id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "avatar_url": self.avatar_url,
            "settings": self.settings.model_dump(),
            "usage_stats": {
                "total_content": self.usage_stats.total_content_generated,
                "total_images": self.usage_stats.total_images_generated,
                "total_posts": self.usage_stats.total_posts_published,
                "last_activity": self.usage_stats.last_activity_at.isoformat() if self.usage_stats.last_activity_at else None,
            },
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


class CreateWorkspaceRequest(BaseModel):
    """Request model for creating a new workspace"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    settings: Optional[Dict[str, Any]] = None


class UpdateWorkspaceRequest(BaseModel):
    """Request model for updating a workspace"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    avatar_url: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


class WorkspaceResponse(BaseModel):
    """Response model for workspace operations"""
    id: str
    workspace_id: str
    client_id: str
    name: str
    slug: str
    description: Optional[str]
    status: str
    member_count: Optional[int] = None
    created_at: str

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class WorkspaceDetailResponse(WorkspaceResponse):
    """Detailed response model for workspace with usage stats"""
    usage_stats: Dict[str, Any]
    settings: Dict[str, Any]
    avatar_url: Optional[str] = None
