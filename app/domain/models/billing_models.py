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
    User credit balance tracking with multi-duration billing support
    PRD: Subscription Plan Upgrade (Multi-Duration with 5% Bulk Discount)
    Sections 7.1 & 8.1 & 8.3: User Wallet + Billing Cycle + Subscription Lifecycle

    Credits are tracked separately:
    - subscription_credits: Subscription credits (consumed FIRST, reset on renewal)
    - bonus_credits: One-time bonus credits (consumed SECOND, never expire)

    Consumption order ensures subscription credits are used before expiry.
    """
    user_id: str = Field(..., description="Reference to users collection")

    # Bonus credits (consumed second, preserved on renewal, never expire)
    bonus_credits: int = Field(default=0, description="One-time bonus credits (consumed after subscription)")

    # Subscription credits (consumed first, reset on renewal)
    subscription_credits: int = Field(default=0, description="Subscription credits (consumed first)")

    # Legacy/computed fields (for backwards compatibility)
    total_credits: int = Field(default=0, description="Total credits: bonus + subscription")
    credits_used: int = Field(default=0, description="Credits consumed in current cycle")
    credits_remaining: int = Field(default=0, description="Calculated: bonus_credits + subscription_credits")

    # Subscription details
    subscription_tier: Optional[str] = Field(default=None, description="starter|growth|pro|agency|custom")
    billing_cycle: str = Field(default="monthly", description="monthly|3_months|6_months|12_months (PRD 8.1)")

    # Lifecycle tracking (PRD 8.3: Subscription Lifecycle)
    start_date: Optional[datetime] = Field(default=None, description="Subscription start date")
    end_date: Optional[datetime] = Field(default=None, description="Subscription end date (auto-expire after this)")
    next_renewal: Optional[datetime] = Field(default=None, description="Next billing cycle date")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {ObjectId: str}
        schema_extra = {
            "example": {
                "user_id": "507f1f77bcf86cd799439011",
                "bonus_credits": 1000,
                "subscription_credits": 60,
                "total_credits": 1060,
                "credits_used": 15,
                "credits_remaining": 1045,
                "subscription_tier": "starter",
                "billing_cycle": "3_months",
                "start_date": "2026-04-01T00:00:00Z",
                "end_date": "2026-07-01T00:00:00Z",
                "next_renewal": "2026-07-01T00:00:00Z"
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
    type: Literal["allocation", "deduction", "bonus", "refund", "trial"] = Field(..., description="Transaction type")
    amount: int = Field(..., description="Credit amount (negative for deduction)")
    balance_before: int = Field(..., description="Credit balance before transaction")
    balance_after: int = Field(..., description="Credit balance after transaction")
    reason: Literal["subscription", "retry", "campaign_generation", "refund", "bonus", "trial", "whatsapp_content_generation", "whatsapp_graphic_generation"] = Field(..., description="Why credits changed")
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
    Subscription plan definition with multi-duration support
    PRD: Subscription Plan Upgrade (Multi-Duration with 5% Bulk Discount)
    Sections 5, 6, 7: Multi-duration subscription model with 5% discount
    """
    tier_id: str = Field(..., description="starter|growth|pro|agency|custom")
    name: str = Field(..., description="Display name")

    # Monthly pricing (base price)
    price_ngn_monthly: int = Field(..., description="Monthly price in Nigerian Naira")
    credits_monthly: int = Field(..., description="Monthly credit allocation")

    # Multi-duration pricing (calculated with 5% discount)
    price_ngn_3months: int = Field(..., description="3-month price with 5% discount")
    price_ngn_6months: int = Field(..., description="6-month price with 5% discount")
    price_ngn_12months: int = Field(..., description="12-month price with 5% discount")

    # Legacy fields for backward compatibility
    price_ngn: int = Field(..., description="Alias for price_ngn_monthly (backward compatibility)")
    credits: int = Field(..., description="Alias for credits_monthly (backward compatibility)")

    price_per_credit: int = Field(..., description="Calculated unit price based on monthly plan")
    features: list[str] = Field(default_factory=list, description="Feature list for display")
    is_active: bool = Field(default=True, description="Whether tier is available for purchase")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        schema_extra = {
            "example": {
                "tier_id": "starter",
                "name": "Starter Plan",
                "price_ngn_monthly": 15000,
                "price_ngn_3months": 42750,
                "price_ngn_6months": 85500,
                "price_ngn_12months": 171000,
                "credits_monthly": 20,
                "price_ngn": 15000,
                "credits": 20,
                "price_per_credit": 750,
                "features": ["20 Campaigns/Month", "Basic Support"],
                "is_active": True
            }
        }


# ==================== PAYMENT TRANSACTIONS ====================
# PRD Section 6: Billing System Requirements

class PaymentTransaction(BaseModel):
    """
    Payment transaction record with billing cycle support
    PRD: Subscription Plan Upgrade (Multi-Duration with 5% Bulk Discount)
    Sections 6.3 & 8.1 & 8.2: Payment Flow + Billing Cycle + Payment Logic
    """
    user_id: str = Field(..., description="User making payment")
    transaction_ref: str = Field(..., description="SQUAD transaction reference")
    amount: int = Field(..., description="Payment amount in NGN")
    currency: str = Field(default="NGN", description="Currency code")
    status: Literal["pending", "completed", "failed"] = Field(default="pending", description="Payment status")
    payment_method: Optional[str] = Field(default=None, description="card|bank_transfer|ussd")
    gateway: str = Field(default="squad", description="Payment gateway used")

    # Subscription details (PRD 8.1 & 8.2)
    subscription_tier: str = Field(..., description="Tier being purchased")
    billing_cycle: str = Field(default="monthly", description="monthly|3_months|6_months|12_months")
    credits_allocated: int = Field(..., description="Total credits to be allocated for this payment")

    squad_response: Optional[dict] = Field(default=None, description="Full SQUAD webhook payload")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = Field(default=None, description="When payment was verified")

    class Config:
        json_encoders = {ObjectId: str}
        schema_extra = {
            "example": {
                "user_id": "507f1f77bcf86cd799439011",
                "transaction_ref": "SQUAD_123456789",
                "amount": 71250,
                "currency": "NGN",
                "status": "completed",
                "payment_method": "card",
                "gateway": "squad",
                "subscription_tier": "growth",
                "billing_cycle": "3_months",
                "credits_allocated": 105
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


# ==================== FREE TRIAL ====================
# PRD: Free Trial System V1

class UserTrial(BaseModel):
    """
    Free trial tracking per user
    PRD Section 4.1: User Trial Fields
    """
    user_id: str = Field(..., description="Reference to users collection")
    is_trial: bool = Field(default=True, description="Whether user is on trial")
    trial_start_date: datetime = Field(default_factory=datetime.utcnow, description="When trial started")
    trial_end_date: datetime = Field(..., description="When trial expires (start + 3 days)")
    trial_credits: int = Field(default=10, description="Total trial credits allocated")
    credits_remaining: int = Field(default=10, description="Trial credits remaining")
    trial_used: bool = Field(default=False, description="Whether trial has been claimed (abuse prevention)")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {ObjectId: str}
        schema_extra = {
            "example": {
                "user_id": "507f1f77bcf86cd799439011",
                "is_trial": True,
                "trial_start_date": "2026-04-13T00:00:00Z",
                "trial_end_date": "2026-04-16T00:00:00Z",
                "trial_credits": 10,
                "credits_remaining": 10,
                "trial_used": False
            }
        }


class TrialStatusResponse(BaseModel):
    """Trial status response for frontend"""
    is_trial: bool = Field(default=False)
    trial_active: bool = Field(default=False)
    trial_start_date: Optional[datetime] = None
    trial_end_date: Optional[datetime] = None
    trial_credits: int = Field(default=0)
    credits_remaining: int = Field(default=0)
    days_remaining: int = Field(default=0)
    hours_remaining: int = Field(default=0)
    trial_expired: bool = Field(default=False)
    trial_already_used: bool = Field(default=False)
    low_credit_warning: bool = Field(default=False, description="PRD 6.2: True when credits ≤ 2")


# ==================== API REQUEST/RESPONSE MODELS ====================

class InitializePaymentRequest(BaseModel):
    """
    Request to start payment flow with billing cycle support
    PRD: Subscription Plan Upgrade (Multi-Duration with 5% Bulk Discount)
    Section 8.1: Billing cycle selection
    """
    tier_id: str = Field(..., description="Subscription tier to purchase")
    billing_cycle: str = Field(default="monthly", description="monthly|3_months|6_months|12_months")
    test_amount: Optional[int] = Field(None, description="Custom test amount in NGN (only for tier_id='test')")
    test_credits: Optional[int] = Field(None, description="Custom test credits (only for tier_id='test')")

    class Config:
        schema_extra = {
            "example": {
                "tier_id": "growth",
                "billing_cycle": "3_months"
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
    """
    User credit balance information with billing cycle
    PRD: Subscription Plan Upgrade (Multi-Duration with 5% Bulk Discount)
    Section 8.3: Subscription Lifecycle tracking
    """
    total_credits: int
    credits_used: int
    credits_remaining: int
    subscription_tier: Optional[str] = None
    billing_cycle: Optional[str] = Field(default="monthly", description="monthly|3_months|6_months|12_months")
    start_date: Optional[datetime] = Field(default=None, description="Subscription start date")
    end_date: Optional[datetime] = Field(default=None, description="Subscription end date")
    next_renewal: Optional[datetime] = None
    low_credit_warning: bool = Field(default=False, description="True if credits <= 3 (PRD 7.3)")

    class Config:
        schema_extra = {
            "example": {
                "total_credits": 105,
                "credits_used": 15,
                "credits_remaining": 90,
                "subscription_tier": "growth",
                "billing_cycle": "3_months",
                "start_date": "2026-04-01T00:00:00Z",
                "end_date": "2026-07-01T00:00:00Z",
                "next_renewal": "2026-07-01T00:00:00Z",
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
