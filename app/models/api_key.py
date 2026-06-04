"""
API Key Model for SDK Authentication

Enterprise-grade API key management with scopes, rate limiting, and usage tracking.
"""

from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from bson import ObjectId
import secrets
import hashlib


class APIKeyScope:
    """API key permission scopes"""

    # Content operations
    CONTENT_READ = "content:read"
    CONTENT_WRITE = "content:write"
    CONTENT_DELETE = "content:delete"

    # Draft operations
    DRAFTS_READ = "drafts:read"
    DRAFTS_WRITE = "drafts:write"
    DRAFTS_DELETE = "drafts:delete"

    # Image operations
    IMAGES_GENERATE = "images:generate"
    IMAGES_EDIT = "images:edit"

    # Connection operations
    CONNECTIONS_READ = "connections:read"
    CONNECTIONS_WRITE = "connections:write"
    CONNECTIONS_DELETE = "connections:delete"

    # Publishing operations
    PUBLISHING_WRITE = "publishing:write"
    PUBLISHING_SCHEDULE = "publishing:schedule"

    # Billing operations
    BILLING_READ = "billing:read"
    BILLING_WRITE = "billing:write"

    # Admin operations
    ADMIN_ALL = "admin:all"

    @classmethod
    def get_all_scopes(cls) -> List[str]:
        """Get all available scopes"""
        return [
            cls.CONTENT_READ, cls.CONTENT_WRITE, cls.CONTENT_DELETE,
            cls.DRAFTS_READ, cls.DRAFTS_WRITE, cls.DRAFTS_DELETE,
            cls.IMAGES_GENERATE, cls.IMAGES_EDIT,
            cls.CONNECTIONS_READ, cls.CONNECTIONS_WRITE, cls.CONNECTIONS_DELETE,
            cls.PUBLISHING_WRITE, cls.PUBLISHING_SCHEDULE,
            cls.BILLING_READ, cls.BILLING_WRITE,
            cls.ADMIN_ALL
        ]

    @classmethod
    def get_default_scopes(cls) -> List[str]:
        """Get default scopes for new API keys"""
        return [
            cls.CONTENT_READ, cls.CONTENT_WRITE,
            cls.DRAFTS_READ, cls.DRAFTS_WRITE, cls.DRAFTS_DELETE,
            cls.IMAGES_GENERATE, cls.IMAGES_EDIT,
            cls.CONNECTIONS_READ, cls.CONNECTIONS_WRITE,
            cls.PUBLISHING_WRITE, cls.PUBLISHING_SCHEDULE,
            cls.BILLING_READ
        ]


class APIKeyRateLimit(BaseModel):
    """Rate limit configuration for API key"""
    requests_per_hour: int = Field(default=1000, ge=1)
    requests_per_day: int = Field(default=10000, ge=1)
    image_generations_per_hour: int = Field(default=50, ge=1)
    content_generations_per_hour: int = Field(default=100, ge=1)


class APIKeyUsageStats(BaseModel):
    """Usage statistics for API key"""
    total_requests: int = Field(default=0, ge=0)
    requests_today: int = Field(default=0, ge=0)
    requests_this_hour: int = Field(default=0, ge=0)
    last_request_at: Optional[datetime] = None
    last_request_ip: Optional[str] = None
    last_request_endpoint: Optional[str] = None


class APIKey(BaseModel):
    """API Key model for SDK authentication"""

    id: Optional[str] = Field(default=None, alias="_id")
    user_id: str = Field(..., description="User ID who owns this API key")

    # API Key details
    key_prefix: str = Field(..., description="First 8 chars of key for display (e.g., 'urisocial_abcd1234')")
    key_hash: str = Field(..., description="SHA256 hash of the full API key")

    name: str = Field(..., max_length=100, description="Human-readable name for the key")
    description: Optional[str] = Field(None, max_length=500, description="Optional description")

    # Permissions
    scopes: List[str] = Field(default_factory=APIKeyScope.get_default_scopes)

    # Rate limiting
    rate_limits: APIKeyRateLimit = Field(default_factory=APIKeyRateLimit)

    # Usage tracking
    usage_stats: APIKeyUsageStats = Field(default_factory=APIKeyUsageStats)

    # Status
    status: str = Field(default="active", pattern="^(active|revoked|expired)$")

    # Metadata
    environment: str = Field(default="production", pattern="^(production|development|staging)$")
    allowed_ips: Optional[List[str]] = Field(None, description="Whitelist of allowed IP addresses")
    allowed_origins: Optional[List[str]] = Field(None, description="Whitelist of allowed CORS origins")

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoked_reason: Optional[str] = None

    class Config:
        populate_by_name = True
        json_encoders = {
            ObjectId: str,
            datetime: lambda v: v.isoformat()
        }

    @staticmethod
    def generate_api_key() -> str:
        """
        Generate a secure API key
        Format: urisocial_<32 random chars>
        """
        random_part = secrets.token_urlsafe(24)  # 32 chars after base64 encoding
        return f"urisocial_{random_part}"

    @staticmethod
    def hash_api_key(api_key: str) -> str:
        """Hash an API key using SHA256"""
        return hashlib.sha256(api_key.encode()).hexdigest()

    @staticmethod
    def get_key_prefix(api_key: str) -> str:
        """Get the first 12 characters for display"""
        return api_key[:12] + "..." if len(api_key) > 12 else api_key

    def has_scope(self, required_scope: str) -> bool:
        """Check if API key has the required scope"""
        # Admin has all permissions
        if APIKeyScope.ADMIN_ALL in self.scopes:
            return True

        return required_scope in self.scopes

    def has_any_scope(self, required_scopes: List[str]) -> bool:
        """Check if API key has any of the required scopes"""
        if APIKeyScope.ADMIN_ALL in self.scopes:
            return True

        return any(scope in self.scopes for scope in required_scopes)

    def is_valid(self) -> bool:
        """Check if API key is currently valid"""
        if self.status != "active":
            return False

        if self.expires_at and datetime.utcnow() > self.expires_at:
            return False

        return True

    def increment_usage(self, endpoint: str, ip_address: str):
        """Increment usage statistics"""
        self.usage_stats.total_requests += 1
        self.usage_stats.requests_today += 1
        self.usage_stats.requests_this_hour += 1
        self.usage_stats.last_request_at = datetime.utcnow()
        self.usage_stats.last_request_ip = ip_address
        self.usage_stats.last_request_endpoint = endpoint
        self.last_used_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def check_rate_limit(self, operation_type: str = "general") -> bool:
        """
        Check if rate limit is exceeded
        Returns True if within limits, False if exceeded
        """
        if operation_type == "image_generation":
            return self.usage_stats.requests_this_hour < self.rate_limits.image_generations_per_hour
        elif operation_type == "content_generation":
            return self.usage_stats.requests_this_hour < self.rate_limits.content_generations_per_hour
        else:
            return self.usage_stats.requests_this_hour < self.rate_limits.requests_per_hour

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage"""
        data = self.model_dump(by_alias=True, exclude_none=True)
        if self.id:
            data["_id"] = ObjectId(self.id)
        return data

    def to_public_dict(self) -> Dict[str, Any]:
        """Convert to public dictionary (without sensitive data)"""
        return {
            "id": self.id,
            "key_prefix": self.key_prefix,
            "name": self.name,
            "description": self.description,
            "scopes": self.scopes,
            "status": self.status,
            "environment": self.environment,
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "usage_stats": {
                "total_requests": self.usage_stats.total_requests,
                "last_request_at": self.usage_stats.last_request_at.isoformat() if self.usage_stats.last_request_at else None
            }
        }


class CreateAPIKeyRequest(BaseModel):
    """Request model for creating a new API key"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    scopes: Optional[List[str]] = None
    environment: str = Field(default="production", pattern="^(production|development|staging)$")
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650, description="Expiration in days (max 10 years)")
    rate_limit_requests_per_hour: Optional[int] = Field(None, ge=1, le=10000)


class CreateAPIKeyResponse(BaseModel):
    """Response model for creating a new API key"""
    api_key: str = Field(..., description="The actual API key - ONLY SHOWN ONCE")
    api_key_id: str
    key_prefix: str
    name: str
    scopes: List[str]
    environment: str
    created_at: str
    expires_at: Optional[str] = None

    warning: str = Field(
        default="Store this API key securely. You won't be able to see it again!",
        description="Security warning"
    )


class UpdateAPIKeyRequest(BaseModel):
    """Request model for updating an API key"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    scopes: Optional[List[str]] = None
    status: Optional[str] = Field(None, pattern="^(active|revoked)$")
    rate_limit_requests_per_hour: Optional[int] = Field(None, ge=1, le=10000)
