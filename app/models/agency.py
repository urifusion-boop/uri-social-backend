"""
Agency Models — Agency Accounts feature

The agency layer that wraps Jane. An agency manages many brand accounts under
one organization, with one shared credit pool and brand-level data isolation.

Collections:
  - agencies              (the top-level org + credit wallet)
  - agency_members        (users in the agency, role: admin | agent)
  - member_brand_access   (which agents can operate which brands)
  - brand_credit_usage    (per-brand credit consumption log)

Note: this app uses MongoDB. The PRD's SQL tables map to these collections;
FKs are plain id fields, indexes are created in migrations / on startup.
"""

from datetime import datetime
from typing import Optional, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field
from bson import ObjectId
import secrets


class AgencyRole(str, Enum):
    """V1 two-role model (PRD §5.1)."""
    ADMIN = "admin"   # Everything: billing, credits, brands, members, all content ops
    AGENT = "agent"   # Content ops on assigned brands only


class Agency(BaseModel):
    """Top-level organization holding the shared credit wallet."""

    id: Optional[str] = Field(default=None, alias="_id")
    agency_id: str = Field(..., description="Unique agency identifier (agcy_xxx)")

    name: str = Field(..., min_length=1, max_length=200)
    owner_user_id: str = Field(..., description="User who created the agency")

    # Credit wallet (shared pool)
    wallet_credits: float = Field(default=0.0, ge=0)
    per_brand_caps_enabled: bool = Field(default=False)

    # Plan
    plan_tier: str = Field(default="agency_starter")
    max_brands: Optional[int] = Field(default=None, description="null = unlimited")

    status: str = Field(default="active", pattern="^(active|suspended|cancelled)$")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True
        use_enum_values = True
        json_encoders = {ObjectId: str, datetime: lambda v: v.isoformat()}

    @staticmethod
    def generate_agency_id() -> str:
        return f"agcy_{secrets.token_urlsafe(12)[:16]}"

    def to_dict(self) -> Dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True)
        if self.id:
            data["_id"] = ObjectId(self.id)
        return data

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "agency_id": self.agency_id,
            "name": self.name,
            "owner_user_id": self.owner_user_id,
            "wallet_credits": self.wallet_credits,
            "per_brand_caps_enabled": self.per_brand_caps_enabled,
            "plan_tier": self.plan_tier,
            "max_brands": self.max_brands,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


class AgencyMember(BaseModel):
    """A user's membership + role in an agency."""

    id: Optional[str] = Field(default=None, alias="_id")
    agency_member_id: str = Field(..., description="Unique member id (ambr_xxx)")
    agency_id: str = Field(...)
    user_id: Optional[str] = Field(default=None, description="Null until a pending email invite is claimed")
    email: Optional[str] = Field(default=None, description="Invited email (for pending invites)")

    role: AgencyRole = Field(default=AgencyRole.AGENT)
    status: str = Field(default="active", pattern="^(active|invited|suspended|removed)$")

    invited_by_user_id: Optional[str] = None
    invited_at: datetime = Field(default_factory=datetime.utcnow)
    joined_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True
        use_enum_values = True
        json_encoders = {ObjectId: str, datetime: lambda v: v.isoformat()}

    @staticmethod
    def generate_member_id() -> str:
        return f"ambr_{secrets.token_urlsafe(12)[:16]}"

    def to_dict(self) -> Dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True)
        if self.id:
            data["_id"] = ObjectId(self.id)
        return data

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "agency_member_id": self.agency_member_id,
            "agency_id": self.agency_id,
            "user_id": self.user_id,
            "email": self.email,
            "role": self.role,
            "status": self.status,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
            "created_at": self.created_at.isoformat(),
        }


class MemberBrandAccess(BaseModel):
    """Join row: which agency member can operate which brand."""

    id: Optional[str] = Field(default=None, alias="_id")
    agency_member_id: str = Field(...)
    brand_id: str = Field(...)
    assigned_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str, datetime: lambda v: v.isoformat()}

    def to_dict(self) -> Dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True)
        if self.id:
            data["_id"] = ObjectId(self.id)
        return data


class BrandCreditUsage(BaseModel):
    """Per-brand credit consumption log (PRD §4.4) — always written, even shared-pool."""

    id: Optional[str] = Field(default=None, alias="_id")
    brand_id: str = Field(...)
    agency_id: Optional[str] = None
    operation_type: str = Field(..., description="content_generation | image | blog | ...")
    credits_consumed: float = Field(..., ge=0)
    consumed_by_user_id: Optional[str] = None
    consumed_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str, datetime: lambda v: v.isoformat()}

    def to_dict(self) -> Dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True)
        if self.id:
            data["_id"] = ObjectId(self.id)
        return data


# ── Request models ──────────────────────────────────────────────────────────

class CreateAgencyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    plan_tier: str = Field(default="agency_starter")


class InviteAgencyMemberRequest(BaseModel):
    email: str = Field(...)
    role: AgencyRole = Field(default=AgencyRole.AGENT)


class TopUpWalletRequest(BaseModel):
    credits: float = Field(..., gt=0)
