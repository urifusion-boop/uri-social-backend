"""
SDK End-User Model - Multi-Tenant SDK Integration

Represents an end-user (client's user) who uses URISocial features through
an SDK client's platform.

Example: John Doe is a user of "Acme SaaS Platform". When John uses social
media features on Acme's platform, he's an SDK end-user.
"""

from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, EmailStr
from bson import ObjectId
import secrets


class SDKEndUserMetadata(BaseModel):
    """Custom metadata from SDK client about their user"""
    # Flexible structure - client can store any data they need
    # Examples: tier, subscription_status, custom_fields, etc.
    pass


class SDKEndUser(BaseModel):
    """
    SDK End-User - A user of an SDK client's platform

    Represents an individual user from an SDK client's platform.
    Each end-user has their own brand profile and preferences.

    Flow:
    1. SDK Client (e.g., "Acme Platform") makes API call
    2. Includes X-End-User-ID: "user_123" (Acme's user ID)
    3. URISocial creates/loads SDK End-User record
    4. Links to brand_profile for user's preferences
    """

    id: Optional[str] = Field(default=None, alias="_id")
    end_user_id: str = Field(..., description="Unique end-user identifier (enduser_xxxxx)")

    # Parent SDK client
    sdk_client_id: str = Field(..., description="Parent SDK client ID (FK to sdk_client_profiles)")

    # External identity (from SDK client's system)
    external_user_id: str = Field(..., description="Client's user ID (their system)")
    external_email: Optional[EmailStr] = Field(default=None, description="User's email (from client)")
    external_name: Optional[str] = Field(default=None, max_length=200, description="User's name (from client)")

    # URISocial data
    brand_profile_id: Optional[str] = Field(default=None, description="FK to brand_profiles collection")

    # User status
    onboarding_completed: bool = Field(default=False, description="Has user completed onboarding")
    status: str = Field(
        default="active",
        pattern="^(active|suspended|deleted)$",
        description="End-user status"
    )

    # Custom metadata from SDK client
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Custom data from SDK client")

    # Activity tracking
    last_active_at: Optional[datetime] = None
    total_api_calls: int = Field(default=0, ge=0, description="Total API calls by this user")
    total_generations: int = Field(default=0, ge=0, description="Total content generations")
    total_images: int = Field(default=0, ge=0, description="Total image generations")

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
    def generate_end_user_id() -> str:
        """
        Generate a unique end-user ID
        Format: enduser_<random_16_chars>
        """
        random_part = secrets.token_urlsafe(12)[:16]
        return f"enduser_{random_part}"

    def update_activity(self):
        """Update last activity timestamp"""
        self.last_active_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def increment_usage(self, metric: str, amount: int = 1):
        """Increment usage statistics"""
        if metric == "generation":
            self.total_generations += amount
        elif metric == "image":
            self.total_images += amount
        elif metric == "api_call":
            self.total_api_calls += amount

        self.update_activity()

    def mark_onboarding_complete(self):
        """Mark onboarding as completed"""
        self.onboarding_completed = True
        self.updated_at = datetime.utcnow()

    def is_active(self) -> bool:
        """Check if end-user is active"""
        return self.status == "active" and self.deleted_at is None
