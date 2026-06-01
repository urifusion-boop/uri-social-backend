"""
Client Model - Multi-Tenant Organization/Company

Represents a company/organization that uses URISocial SDK.
One client can have multiple workspaces and team members.
Billing and credits are managed at the client level.
"""

from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, EmailStr
from bson import ObjectId
import secrets


class ClientBillingInfo(BaseModel):
    """Billing information for a client"""
    billing_email: EmailStr
    company_name: Optional[str] = None
    tax_id: Optional[str] = None
    address: Optional[str] = None
    country: Optional[str] = None
    billing_cycle: str = Field(default="monthly", pattern="^(monthly|annual|custom)$")


class ClientSubscription(BaseModel):
    """Subscription details for a client"""
    tier: str = Field(default="starter", pattern="^(starter|professional|enterprise|custom)$")
    status: str = Field(default="active", pattern="^(active|trial|suspended|cancelled)$")

    # Credits
    total_credits: int = Field(default=1000, ge=0)
    used_credits: int = Field(default=0, ge=0)

    # Limits
    max_workspaces: int = Field(default=3, ge=1)
    max_users_per_workspace: int = Field(default=10, ge=1)
    max_api_keys: int = Field(default=5, ge=1)

    # Subscription dates
    trial_ends_at: Optional[datetime] = None
    current_period_start: Optional[datetime] = None
    current_period_end: Optional[datetime] = None


class ClientUsageStats(BaseModel):
    """Usage statistics for a client"""
    total_content_generated: int = Field(default=0, ge=0)
    total_images_generated: int = Field(default=0, ge=0)
    total_posts_published: int = Field(default=0, ge=0)
    total_api_requests: int = Field(default=0, ge=0)

    # Current period usage
    content_generated_this_month: int = Field(default=0, ge=0)
    images_generated_this_month: int = Field(default=0, ge=0)
    posts_published_this_month: int = Field(default=0, ge=0)
    api_requests_this_month: int = Field(default=0, ge=0)

    last_activity_at: Optional[datetime] = None


class Client(BaseModel):
    """
    Client Model - Represents a company/organization

    A client is the top-level entity in the multi-tenant hierarchy.
    Each client has:
    - Multiple workspaces (teams/projects)
    - Billing information and subscription
    - Credits pool shared across workspaces
    - Usage tracking and limits
    """

    id: Optional[str] = Field(default=None, alias="_id")
    client_id: str = Field(..., description="Unique client identifier (cli_xxxxx)")

    # Basic info
    name: str = Field(..., min_length=1, max_length=200, description="Client/Company name")
    slug: str = Field(..., pattern="^[a-z0-9-]+$", description="URL-friendly identifier")
    description: Optional[str] = Field(None, max_length=1000)

    # Owner (user who created the client)
    owner_user_id: str = Field(..., description="User ID of the client owner")

    # Billing & Subscription
    billing_info: ClientBillingInfo
    subscription: ClientSubscription = Field(default_factory=ClientSubscription)

    # Usage & Stats
    usage_stats: ClientUsageStats = Field(default_factory=ClientUsageStats)

    # Settings
    settings: Dict[str, Any] = Field(default_factory=dict, description="Client-specific settings")

    # Status
    status: str = Field(default="active", pattern="^(active|suspended|cancelled|deleted)$")

    # Metadata
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Custom metadata")

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    deleted_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {
            ObjectId: str,
            datetime: lambda v: v.isoformat()
        }

    @staticmethod
    def generate_client_id() -> str:
        """
        Generate a unique client ID
        Format: cli_<random_16_chars>
        """
        random_part = secrets.token_urlsafe(12)[:16]
        return f"cli_{random_part}"

    @staticmethod
    def generate_slug(name: str) -> str:
        """
        Generate URL-friendly slug from name
        Example: "Acme Corp" -> "acme-corp"
        """
        slug = name.lower().strip()
        slug = slug.replace(" ", "-")
        slug = "".join(c for c in slug if c.isalnum() or c == "-")
        slug = "-".join(filter(None, slug.split("-")))  # Remove duplicate dashes
        return slug[:50]  # Limit length

    def has_credits(self, amount: int = 1) -> bool:
        """Check if client has enough credits"""
        available = self.subscription.total_credits - self.subscription.used_credits
        return available >= amount

    def deduct_credits(self, amount: int) -> bool:
        """
        Deduct credits from client's pool
        Returns True if successful, False if insufficient credits
        """
        if not self.has_credits(amount):
            return False

        self.subscription.used_credits += amount
        self.updated_at = datetime.utcnow()
        return True

    def add_credits(self, amount: int):
        """Add credits to client's pool"""
        self.subscription.total_credits += amount
        self.updated_at = datetime.utcnow()

    def can_create_workspace(self, current_workspace_count: int) -> bool:
        """Check if client can create more workspaces"""
        return current_workspace_count < self.subscription.max_workspaces

    def can_add_user_to_workspace(self, current_user_count: int) -> bool:
        """Check if workspace can add more users"""
        return current_user_count < self.subscription.max_users_per_workspace

    def increment_usage(self, metric: str, amount: int = 1):
        """Increment usage statistics"""
        metric_map = {
            "content": ("total_content_generated", "content_generated_this_month"),
            "image": ("total_images_generated", "images_generated_this_month"),
            "post": ("total_posts_published", "posts_published_this_month"),
            "api": ("total_api_requests", "api_requests_this_month"),
        }

        if metric in metric_map:
            total_field, monthly_field = metric_map[metric]
            current_total = getattr(self.usage_stats, total_field)
            current_monthly = getattr(self.usage_stats, monthly_field)

            setattr(self.usage_stats, total_field, current_total + amount)
            setattr(self.usage_stats, monthly_field, current_monthly + amount)

        self.usage_stats.last_activity_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def reset_monthly_usage(self):
        """Reset monthly usage counters (called at start of billing cycle)"""
        self.usage_stats.content_generated_this_month = 0
        self.usage_stats.images_generated_this_month = 0
        self.usage_stats.posts_published_this_month = 0
        self.usage_stats.api_requests_this_month = 0
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
            "client_id": self.client_id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "subscription": {
                "tier": self.subscription.tier,
                "status": self.subscription.status,
                "max_workspaces": self.subscription.max_workspaces,
                "max_users_per_workspace": self.subscription.max_users_per_workspace,
            },
            "credits": {
                "total": self.subscription.total_credits,
                "used": self.subscription.used_credits,
                "remaining": self.subscription.total_credits - self.subscription.used_credits,
            },
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


class CreateClientRequest(BaseModel):
    """Request model for creating a new client"""
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    billing_email: EmailStr
    company_name: Optional[str] = None
    subscription_tier: str = Field(default="starter", pattern="^(starter|professional|enterprise)$")


class UpdateClientRequest(BaseModel):
    """Request model for updating a client"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    billing_email: Optional[EmailStr] = None
    company_name: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


class ClientResponse(BaseModel):
    """Response model for client operations"""
    id: str
    client_id: str
    name: str
    slug: str
    description: Optional[str]
    subscription_tier: str
    subscription_status: str
    credits_remaining: int
    max_workspaces: int
    status: str
    created_at: str

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
