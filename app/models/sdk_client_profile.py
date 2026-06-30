"""
SDK Client Profile Model - Multi-Tenant SDK Integration

Represents an SDK client (external application) that integrates URISocial
via API keys. Each SDK client can have many end-users.

This is separate from the Client model (app/models/client.py) which is for
workspace/agency management.
"""

from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from bson import ObjectId
import secrets


class SDKClientSettings(BaseModel):
    """Settings for SDK client"""
    enable_custom_branding: bool = Field(default=False, description="Allow client to customize branding")
    require_email_verification: bool = Field(default=True, description="Require email verification for end-users")
    auto_create_end_users: bool = Field(default=True, description="Auto-create end-users on first API call")
    webhook_url: Optional[str] = Field(default=None, description="Webhook URL for events")
    custom_domain: Optional[str] = Field(default=None, description="Custom domain for white-labeling")
    allowed_features: List[str] = Field(
        default_factory=lambda: ["content_generation", "image_generation", "brand_profile"],
        description="Features enabled for this client"
    )
    default_brand_preferences: Dict[str, Any] = Field(
        default_factory=dict,
        description="Default brand preferences template for new end-users"
    )


class SDKClientLimits(BaseModel):
    """Resource limits for SDK client"""
    max_end_users: int = Field(default=10000, ge=1, description="Maximum number of end-users")
    max_brands_per_user: int = Field(default=3, ge=1, description="Maximum brands per end-user")
    max_monthly_generations: int = Field(default=50000, ge=1, description="Max content generations per month")
    max_monthly_images: int = Field(default=10000, ge=1, description="Max image generations per month")


class SDKClientStats(BaseModel):
    """Usage statistics for SDK client"""
    total_end_users: int = Field(default=0, ge=0, description="Total end-users created")
    active_end_users_30d: int = Field(default=0, ge=0, description="Active users in last 30 days")
    total_generations_month: int = Field(default=0, ge=0, description="Content generations this month")
    total_images_month: int = Field(default=0, ge=0, description="Image generations this month")
    total_api_calls: int = Field(default=0, ge=0, description="Total API calls")
    last_activity_at: Optional[datetime] = None


class SDKClientProfile(BaseModel):
    """
    SDK Client Profile - External application using URISocial SDK

    Represents an external application/platform that integrates URISocial
    via SDK. Each SDK client can have thousands of end-users (their users).

    Example: "Acme SaaS Platform" uses URISocial SDK to provide social media
    features to their 5,000 customers. Each customer is an end-user.
    """

    id: Optional[str] = Field(default=None, alias="_id")
    sdk_client_id: str = Field(..., description="Unique SDK client identifier (sdkcli_xxxxx)")

    # API Key linkage (from SDK Gateway)
    api_key_hash: str = Field(..., description="Hash of the API key (for lookup)")
    api_key_prefix: str = Field(..., description="API key prefix for display (urisocial_xxxxx)")
    developer_id: str = Field(..., description="Developer/owner user ID from SDK Gateway")

    # Client metadata
    company_name: str = Field(..., min_length=1, max_length=200, description="Company/Application name")
    company_logo_url: Optional[str] = Field(default=None, description="Logo URL")
    company_website: Optional[str] = Field(default=None, description="Company website")

    # Configuration
    settings: SDKClientSettings = Field(default_factory=SDKClientSettings)
    limits: SDKClientLimits = Field(default_factory=SDKClientLimits)
    stats: SDKClientStats = Field(default_factory=SDKClientStats)

    # Billing integration (optional - can inherit from API key owner's billing)
    shared_credits_with_developer: bool = Field(
        default=True,
        description="If True, uses developer's credit pool; if False, has own pool"
    )
    dedicated_credits: int = Field(default=0, ge=0, description="Dedicated credit pool (if not shared)")

    # Status
    status: str = Field(
        default="active",
        pattern="^(active|suspended|deleted)$",
        description="SDK client status"
    )

    # Metadata
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Custom metadata")

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_api_call_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {
            ObjectId: str,
            datetime: lambda v: v.isoformat()
        }

    @staticmethod
    def generate_sdk_client_id() -> str:
        """
        Generate a unique SDK client ID
        Format: sdkcli_<random_16_chars>
        """
        random_part = secrets.token_urlsafe(12)[:16]
        return f"sdkcli_{random_part}"

    def can_create_end_user(self) -> bool:
        """Check if client can create more end-users"""
        return self.stats.total_end_users < self.limits.max_end_users

    def increment_end_user_count(self):
        """Increment end-user count"""
        self.stats.total_end_users += 1
        self.updated_at = datetime.utcnow()

    def update_activity(self):
        """Update last activity timestamp"""
        self.last_api_call_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def increment_usage(self, metric: str, amount: int = 1):
        """Increment usage statistics"""
        if metric == "generation":
            self.stats.total_generations_month += amount
        elif metric == "image":
            self.stats.total_images_month += amount
        elif metric == "api_call":
            self.stats.total_api_calls += amount

        self.updated_at = datetime.utcnow()
