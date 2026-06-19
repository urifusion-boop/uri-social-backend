"""
Brand Account Model — Agency Accounts feature

A BrandAccount is the tenant/isolation unit (PRD "Brand"). It is the context
boundary for everything Jane does — every Jane document (brand_profiles,
writing_dna, blog_posts, content_drafts, etc.) is scoped by this brand_id.

IMPORTANT naming: do not confuse with the existing `brand_profiles` collection,
which is Jane's voice/playbook doc. That doc becomes a CHILD of a BrandAccount,
carrying this brand_id.

Solo SMEs get a personal BrandAccount (agency_id = None) auto-provisioned, so
the single-user product keeps working unchanged.
"""

from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
from bson import ObjectId
import secrets


class BrandAccount(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    brand_id: str = Field(..., description="Unique brand identifier (brnd_xxx)")

    # Ownership / org
    agency_id: Optional[str] = Field(default=None, description="null = solo SME brand")
    owner_user_id: str = Field(..., description="User who owns/created the brand")

    # Basic info
    name: str = Field(..., min_length=1, max_length=200)
    industry: Optional[str] = Field(default=None, max_length=120)
    logo_url: Optional[str] = None

    # Per-brand credit cap (PRD §4.3); null = no cap
    monthly_credit_cap: Optional[float] = Field(default=None, ge=0)

    status: str = Field(default="active", pattern="^(active|archived|deleted)$")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    archived_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str, datetime: lambda v: v.isoformat()}

    @staticmethod
    def generate_brand_id() -> str:
        return f"brnd_{secrets.token_urlsafe(12)[:16]}"

    @staticmethod
    def personal_brand_id(user_id: str) -> str:
        """Deterministic personal brand id for a solo user (idempotent migration)."""
        return f"brnd_personal_{user_id}"

    def to_dict(self) -> Dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True)
        if self.id:
            data["_id"] = ObjectId(self.id)
        return data

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "brand_id": self.brand_id,
            "agency_id": self.agency_id,
            "owner_user_id": self.owner_user_id,
            "name": self.name,
            "industry": self.industry,
            "logo_url": self.logo_url,
            "monthly_credit_cap": self.monthly_credit_cap,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


class CreateBrandRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    industry: Optional[str] = Field(default=None, max_length=120)
    logo_url: Optional[str] = None
    monthly_credit_cap: Optional[float] = Field(default=None, ge=0)


class DuplicateBrandRequest(BaseModel):
    template_brand_id: str = Field(..., description="Brand to clone playbook/DNA/styles from")
    name: str = Field(..., min_length=1, max_length=200)
    industry: Optional[str] = Field(default=None, max_length=120)


class UpdateBrandRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    industry: Optional[str] = Field(default=None, max_length=120)
    logo_url: Optional[str] = None
    monthly_credit_cap: Optional[float] = Field(default=None, ge=0)
