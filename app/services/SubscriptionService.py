"""
Subscription Management Service
Strictly aligned with PRICING PRD V1

Handles subscription plans:
- Plan definitions (PRD 5)
- Subscription activation (PRD 6.1)
- Subscription renewal (PRD 5.2)
- Tier management
"""
from typing import Optional, List, Dict
from datetime import datetime
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import get_db
from app.domain.models.billing_models import (
    SubscriptionTier,
    SubscriptionResponse
)
from app.services.CreditService import credit_service


class SubscriptionService:
    """
    Subscription tier and lifecycle management
    PRD Section 5: Plan Structure & Section 6: Billing System Requirements
    """

    def __init__(self):
        self.db: AsyncIOMotorDatabase = get_db()
        self.subscription_tiers_collection = self.db["subscription_tiers"]
        self.user_credits_collection = self.db["user_credits"]

    # ==================== PRD 5: Plan Structure ====================

    async def initialize_default_tiers(self) -> None:
        """
        Seed database with default subscription tiers
        PRD Section 5: Plan Structure (Current Only)

        Pricing:
        - Starter: ₦15,000 / 20 credits
        - Growth: ₦25,000 / 35 credits
        - Pro: ₦40,000 / 50 credits
        - Agency: ₦80,000 / 100 credits
        - Custom: ₦750 per credit
        """
        # Check if tiers already exist
        existing_count = await self.subscription_tiers_collection.count_documents({})
        if existing_count > 0:
            return  # Already initialized

        tiers = [
            SubscriptionTier(
                tier_id="starter",
                name="Starter Plan",
                price_ngn=15000,
                credits=20,
                price_per_credit=750,
                features=[
                    "20 Campaigns/Month",
                    "AI Image Generation",
                    "Multi-platform Formatting",
                    "Email Support"
                ],
                is_active=True
            ),
            SubscriptionTier(
                tier_id="growth",
                name="Growth Plan",
                price_ngn=25000,
                credits=35,
                price_per_credit=714,
                features=[
                    "35 Campaigns/Month",
                    "AI Image Generation",
                    "Multi-platform Formatting",
                    "Priority Support",
                    "Advanced Analytics"
                ],
                is_active=True
            ),
            SubscriptionTier(
                tier_id="pro",
                name="Pro Plan",
                price_ngn=40000,
                credits=50,
                price_per_credit=800,
                features=[
                    "50 Campaigns/Month",
                    "AI Image Generation",
                    "Multi-platform Formatting",
                    "Priority Support",
                    "Advanced Analytics",
                    "Team Collaboration"
                ],
                is_active=True
            ),
            SubscriptionTier(
                tier_id="agency",
                name="Agency Plan",
                price_ngn=80000,
                credits=100,
                price_per_credit=800,
                features=[
                    "100 Campaigns/Month",
                    "AI Image Generation",
                    "Multi-platform Formatting",
                    "Dedicated Support",
                    "Advanced Analytics",
                    "Team Collaboration",
                    "White Label Options"
                ],
                is_active=True
            ),
            SubscriptionTier(
                tier_id="custom",
                name="Custom Plan",
                price_ngn=750,  # Per credit
                credits=1,  # Unit pricing
                price_per_credit=750,
                features=[
                    "Pay Per Credit",
                    "₦750 per Campaign",
                    "All Features Included",
                    "No Monthly Commitment"
                ],
                is_active=True
            )
        ]

        # Insert all tiers
        await self.subscription_tiers_collection.insert_many(
            [tier.dict(exclude_none=True) for tier in tiers]
        )

    async def get_all_tiers(self, active_only: bool = True) -> List[SubscriptionTier]:
        """
        Get all available subscription tiers
        PRD Section 5: Plan Structure
        """
        query = {"is_active": True} if active_only else {}
        cursor = self.subscription_tiers_collection.find(query).sort("price_ngn", 1)

        tiers = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            tiers.append(SubscriptionTier(**doc))

        return tiers

    async def get_tier_by_id(self, tier_id: str) -> Optional[SubscriptionTier]:
        """
        Get specific subscription tier details
        """
        tier_doc = await self.subscription_tiers_collection.find_one({"tier_id": tier_id})

        if not tier_doc:
            return None

        tier_doc["_id"] = str(tier_doc["_id"])
        return SubscriptionTier(**tier_doc)

    # ==================== PRD 6.1: Subscription Management ====================

    async def create_subscription(
        self,
        user_id: str,
        tier_id: str
    ) -> SubscriptionResponse:
        """
        Create new subscription after successful payment
        PRD 6.3: On success: Assign credits, Activate subscription
        """
        # Get tier details
        tier = await self.get_tier_by_id(tier_id)
        if not tier:
            raise ValueError(f"Invalid tier_id: {tier_id}")

        # Allocate credits via CreditService
        wallet = await credit_service.allocate_credits(
            user_id=user_id,
            tier_id=tier_id,
            credits=tier.credits,
            reason="subscription"
        )

        return SubscriptionResponse(
            tier_id=tier.tier_id,
            name=tier.name,
            price_ngn=tier.price_ngn,
            credits=tier.credits,
            credits_remaining=wallet.credits_remaining,
            next_renewal=wallet.next_renewal
        )

    async def renew_subscription(self, user_id: str) -> SubscriptionResponse:
        """
        Renew existing subscription (monthly auto-renewal)
        PRD 5.2: Credits reset every billing cycle
        """
        # Get current subscription
        wallet = await credit_service.get_user_wallet(user_id)
        if not wallet or not wallet.subscription_tier:
            raise ValueError("No active subscription found")

        # Get tier details
        tier = await self.get_tier_by_id(wallet.subscription_tier)
        if not tier:
            raise ValueError(f"Invalid tier: {wallet.subscription_tier}")

        # Reset credits for new billing cycle
        renewed_wallet = await credit_service.allocate_credits(
            user_id=user_id,
            tier_id=tier.tier_id,
            credits=tier.credits,
            reason="subscription"
        )

        return SubscriptionResponse(
            tier_id=tier.tier_id,
            name=tier.name,
            price_ngn=tier.price_ngn,
            credits=tier.credits,
            credits_remaining=renewed_wallet.credits_remaining,
            next_renewal=renewed_wallet.next_renewal
        )

    async def get_current_subscription(self, user_id: str) -> Optional[SubscriptionResponse]:
        """
        Get user's current subscription details
        """
        wallet = await credit_service.get_user_wallet(user_id)
        if not wallet or not wallet.subscription_tier:
            return None

        tier = await self.get_tier_by_id(wallet.subscription_tier)
        if not tier:
            return None

        return SubscriptionResponse(
            tier_id=tier.tier_id,
            name=tier.name,
            price_ngn=tier.price_ngn,
            credits=tier.credits,
            credits_remaining=wallet.credits_remaining,
            next_renewal=wallet.next_renewal
        )

    async def cancel_subscription(self, user_id: str) -> bool:
        """
        Cancel user subscription (credits remain until end of cycle)
        PRD 13: MVP Scope - cancellation allowed
        """
        wallet = await credit_service.get_user_wallet(user_id)
        if not wallet:
            return False

        # Set subscription_tier to None but keep remaining credits
        await self.user_credits_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "subscription_tier": None,
                    "next_renewal": None,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return True

    # ==================== Tier Validation ====================

    async def validate_tier_purchase(
        self,
        user_id: str,
        tier_id: str
    ) -> Dict[str, any]:
        """
        Validate if user can purchase tier
        Returns: {valid: bool, message: str, tier: SubscriptionTier}
        """
        tier = await self.get_tier_by_id(tier_id)

        if not tier:
            return {
                "valid": False,
                "message": f"Subscription tier '{tier_id}' not found",
                "tier": None
            }

        if not tier.is_active:
            return {
                "valid": False,
                "message": f"Subscription tier '{tier_id}' is not available",
                "tier": None
            }

        return {
            "valid": True,
            "message": "Tier valid for purchase",
            "tier": tier
        }


# Singleton instance
subscription_service = SubscriptionService()
