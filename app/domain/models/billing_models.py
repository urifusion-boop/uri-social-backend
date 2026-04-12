"""
Billing and Credit System Models
Strictly aligned with PRICING PRD V1
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime
from bson import ObjectId


class PyObjectId(ObjectId):
    """Custom ObjectId type for Pydantic"""
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid ObjectId")
        return ObjectId(v)

    @classmethod
    def __modify_schema__(cls, field_schema):
        field_schema.update(type="string")


# ==================== USER CREDIT WALLET ====================
# PRD Section 7.1: Each user must have total_credits, credits_used, credits_remaining

class UserCreditWallet(BaseModel):
    """
    User credit balance tracking
    PRD Section 7.1: User Wallet
    """
    user_id: str = Field(..., description="Reference to users collection")
    total_credits: int = Field(default=0, description="Total credits allocated this billing cycle")
    credits_used: int = Field(default=0, description="Credits consumed in current cycle")
    credits_remaining: int = Field(default=0, description="Calculated: total_credits - credits_used")
    subscription_tier: Optional[str] = Field(default=None, description="starter|growth|pro|agency|custom")
    next_renewal: Optional[datetime] = Field(default=None, description="Next billing cycle date")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {ObjectId: str}
        schema_extra = {
            "example": {
                "user_id": "507f1f77bcf86cd799439011",
                "total_credits": 35,
                "credits_used": 15,
                "credits_remaining": 20,
                "subscription_tier": "growth",
                "next_renewal": "2026-05-06T00:00:00Z"
            }
        }


# ==================== CREDIT TRANSACTIONS ====================
# PRD Section 9: Campaign Tracking System

class CreditTransaction(BaseModel):
    """
    Credit transaction log for auditing
    Tracks all credit movements
    """
    user_id: str = Field(..., description="User who performed action")
    type: Literal["allocation", "deduction", "bonus", "refund"] = Field(..., description="Transaction type")
    amount: int = Field(..., description="Credit amount (negative for deduction)")
    balance_before: int = Field(..., description="Credit balance before transaction")
    balance_after: int = Field(..., description="Credit balance after transaction")
    reason: Literal["subscription", "retry", "campaign_generation", "refund", "bonus"] = Field(..., description="Why credits changed")
    campaign_id: Optional[str] = Field(default=None, description="Reference to content_requests if applicable")
    retry_count: Optional[int] = Field(default=0, description="Retry number if applicable")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {ObjectId: str}
        schema_extra = {
            "example": {
                "user_id": "507f1f77bcf86cd799439011",
                "type": "deduction",
                "amount": -1,
                "balance_before": 21,
                "balance_after": 20,
                "reason": "campaign_generation",
                "campaign_id": "507f191e810c19729de860ea"
            }
        }


# ==================== SUBSCRIPTION TIERS ====================
# PRD Section 5: Plan Structure

class SubscriptionTier(BaseModel):
    """
    Subscription plan definition
    PRD Section 5: Plan Structure (Current Only)
    """
    tier_id: str = Field(..., description="starter|growth|pro|agency|custom")
    name: str = Field(..., description="Display name")
    price_ngn: int = Field(..., description="Price in Nigerian Naira")
    credits: int = Field(..., description="Monthly credit allocation")
    price_per_credit: int = Field(..., description="Calculated unit price")
    features: list[str] = Field(default_factory=list, description="Feature list for display")
    is_active: bool = Field(default=True, description="Whether tier is available for purchase")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        schema_extra = {
            "example": {
                "tier_id": "growth",
                "name": "Growth Plan",
                "price_ngn": 25000,
                "credits": 35,
                "price_per_credit": 714,
                "features": ["35 Campaigns/Month", "Priority Support"],
                "is_active": True
            }
        }


# ==================== PAYMENT TRANSACTIONS ====================
# PRD Section 6: Billing System Requirements

class PaymentTransaction(BaseModel):
    """
    Payment transaction record
    PRD Section 6.3: Payment Flow
    """
    user_id: str = Field(..., description="User making payment")
    transaction_ref: str = Field(..., description="SQUAD transaction reference")
    amount: int = Field(..., description="Payment amount in NGN")
    currency: str = Field(default="NGN", description="Currency code")
    status: Literal["pending", "completed", "failed"] = Field(default="pending", description="Payment status")
    payment_method: Optional[str] = Field(default=None, description="card|bank_transfer|ussd")
    gateway: str = Field(default="squad", description="Payment gateway used")
    subscription_tier: str = Field(..., description="Tier being purchased")
    squad_response: Optional[dict] = Field(default=None, description="Full SQUAD webhook payload")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = Field(default=None, description="When payment was verified")

    class Config:
        json_encoders = {ObjectId: str}
        schema_extra = {
            "example": {
                "user_id": "507f1f77bcf86cd799439011",
                "transaction_ref": "SQUAD_123456789",
                "amount": 25000,
                "currency": "NGN",
                "status": "completed",
                "payment_method": "card",
                "gateway": "squad",
                "subscription_tier": "growth"
            }
        }


# ==================== CAMPAIGN TRACKING ====================
# PRD Section 9: Campaign Schema

class CampaignTracking(BaseModel):
    """
    Campaign tracking for credit usage
    PRD Section 9: Campaign Schema
    CRITICAL: Must track both retry_count AND image_retry_count separately
    """
    campaign_id: str = Field(..., description="Unique campaign identifier")
    user_id: str = Field(..., description="User who created campaign")
    credits_used: int = Field(default=1, description="Credits consumed by this campaign")
    retry_count: int = Field(default=0, description="Number of full campaign retries (PRD 3.2)")
    image_retry_count: int = Field(default=0, description="Number of image-only retries (PRD 4.2)")
    text_edit_count: int = Field(default=0, description="Text rewrites (unlimited, no cost)")
    status: Literal["pending", "completed", "failed"] = Field(default="completed", description="Campaign status")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {ObjectId: str}
        schema_extra = {
            "example": {
                "campaign_id": "507f191e810c19729de860ea",
                "user_id": "507f1f77bcf86cd799439011",
                "credits_used": 1,
                "retry_count": 0,
                "image_retry_count": 0,
                "text_edit_count": 5,
                "status": "completed"
            }
        }


# ==================== API REQUEST/RESPONSE MODELS ====================

class InitializePaymentRequest(BaseModel):
    """Request to start payment flow"""
    tier_id: str = Field(..., description="Subscription tier to purchase")

    class Config:
        schema_extra = {
            "example": {
                "tier_id": "growth"
            }
        }


class InitializePaymentResponse(BaseModel):
    """Response with SQUAD payment data for inline modal"""
    payment_url: str = Field(..., description="SQUAD hosted checkout page (fallback)")
    transaction_ref: str = Field(..., description="Reference for tracking")
    amount: int = Field(..., description="Payment amount in NGN")
    email: str = Field(..., description="Customer email")
    currency: str = Field(default="NGN", description="Payment currency")
    public_key: str = Field(..., description="SQUAD public key for frontend")

    class Config:
        schema_extra = {
            "example": {
                "payment_url": "https://checkout.squad.co/123456",
                "transaction_ref": "SQUAD_123456789",
                "amount": 25000,
                "email": "user@example.com",
                "currency": "NGN",
                "public_key": "pk_test_xxxxx"
            }
        }


class VerifyPaymentRequest(BaseModel):
    """Request to verify payment status"""
    transaction_ref: str = Field(..., description="SQUAD transaction reference")


class CreditBalanceResponse(BaseModel):
    """User credit balance information"""
    total_credits: int
    credits_used: int
    credits_remaining: int
    subscription_tier: Optional[str] = None
    next_renewal: Optional[datetime] = None
    low_credit_warning: bool = Field(default=False, description="True if credits <= 3 (PRD 7.3)")

    class Config:
        schema_extra = {
            "example": {
                "total_credits": 35,
                "credits_used": 15,
                "credits_remaining": 20,
                "subscription_tier": "growth",
                "next_renewal": "2026-05-06T00:00:00Z",
                "low_credit_warning": False
            }
        }


class SubscriptionResponse(BaseModel):
    """Current subscription details"""
    tier_id: str
    name: str
    price_ngn: int
    credits: int
    credits_remaining: int
    next_renewal: Optional[datetime] = None

    class Config:
        schema_extra = {
            "example": {
                "tier_id": "growth",
                "name": "Growth Plan",
                "price_ngn": 25000,
                "credits": 35,
                "credits_remaining": 20,
                "next_renewal": "2026-05-06T00:00:00Z"
            }
        }
