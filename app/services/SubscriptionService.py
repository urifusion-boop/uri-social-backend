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
    PRD: Subscription Plan Upgrade (Multi-Duration with 5% Bulk Discount)
    Sections 5, 6, 8: Plan Structure, Billing System, Pricing Logic
    """

    # PRD Section 6: Pricing Logic - Billing Cycle Multipliers
    BILLING_CYCLE_MULTIPLIERS = {
        "monthly": 1,
        "3_months": 3,
        "6_months": 6,
        "12_months": 12
    }

    # PRD Section 6: 5% discount on all non-monthly plans
    DISCOUNT_RATE = 0.95

    def __init__(self):
        self._db: Optional[AsyncIOMotorDatabase] = None

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            self._db = get_db()
        return self._db

    @property
    def subscription_tiers_collection(self):
        return self.db["subscription_tiers"]

    @property
    def user_credits_collection(self):
        return self.db["user_credits"]

    # ==================== PRD 6: Pricing Calculations ====================

    def calculate_price(self, monthly_price: int, billing_cycle: str) -> int:
        """
        Calculate price for billing cycle with 5% discount on multi-month plans
        PRD Section 6: Pricing Logic

        Formula:
        - Monthly: monthly_price (no discount)
        - Multi-month: (monthly_price × months) × 0.95

        Args:
            monthly_price: Base monthly price in NGN
            billing_cycle: "monthly"|"3_months"|"6_months"|"12_months"

        Returns:
            Total price for the billing cycle in NGN
        """
        months = self.BILLING_CYCLE_MULTIPLIERS.get(billing_cycle, 1)

        if billing_cycle == "monthly":
            return monthly_price
        else:
            # Apply 5% discount for multi-month plans
            return int(monthly_price * months * self.DISCOUNT_RATE)

    def calculate_credits(self, monthly_credits: int, billing_cycle: str) -> int:
        """
        Calculate total credits for billing cycle
        PRD Section 6: Credits are NOT discounted - user gets full credits for all months

        Args:
            monthly_credits: Base monthly credit allocation
            billing_cycle: "monthly"|"3_months"|"6_months"|"12_months"

        Returns:
            Total credits for the billing cycle
        """
        months = self.BILLING_CYCLE_MULTIPLIERS.get(billing_cycle, 1)
        return monthly_credits * months

    def calculate_end_date(self, start_date: datetime, billing_cycle: str) -> datetime:
        """
        Calculate subscription end date based on billing cycle
        PRD Section 8.3: Subscription Lifecycle - Auto-expire after end_date

        Args:
            start_date: Subscription start date
            billing_cycle: "monthly"|"3_months"|"6_months"|"12_months"

        Returns:
            Subscription end date (start_date + months)
        """
        from dateutil.relativedelta import relativedelta

        months = self.BILLING_CYCLE_MULTIPLIERS.get(billing_cycle, 1)
        return start_date + relativedelta(months=months)

    # ==================== PRD 5: Plan Structure ====================

    async def initialize_default_tiers(self) -> None:
        """
        Seed database with default subscription tiers with multi-duration pricing
        PRD: Subscription Plan Upgrade (Multi-Duration with 5% Bulk Discount)
        Sections 5, 6, 7: Multi-duration subscription model with 5% discount

        Monthly Pricing:
        - Starter: ₦15,000 / 20 credits
        - Growth: ₦25,000 / 35 credits
        - Pro: ₦40,000 / 50 credits
        - Agency: ₦80,000 / 100 credits
        - Custom: ₦750 per credit

        Multi-month pricing applies 5% discount on total (not on credits)
        """
        # Check if tiers already exist
        existing_count = await self.subscription_tiers_collection.count_documents({})
        if existing_count > 0:
            return  # Already initialized

        tiers = [
            SubscriptionTier(
                tier_id="starter",
                name="Starter Plan",
                # Multi-duration pricing (PRD Section 6 & 7)
                price_ngn_monthly=15000,
                price_ngn_3months=int(15000 * 3 * self.DISCOUNT_RATE),  # ₦42,750 (save 5%)
                price_ngn_6months=int(15000 * 6 * self.DISCOUNT_RATE),  # ₦85,500 (save 5%)
                price_ngn_12months=int(15000 * 12 * self.DISCOUNT_RATE),  # ₦171,000 (save 5%)
                credits_monthly=20,
                # Legacy fields for backward compatibility
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
                # Multi-duration pricing (PRD Section 6 & 7)
                price_ngn_monthly=25000,
                price_ngn_3months=int(25000 * 3 * self.DISCOUNT_RATE),  # ₦71,250 (save 5%)
                price_ngn_6months=int(25000 * 6 * self.DISCOUNT_RATE),  # ₦142,500 (save 5%)
                price_ngn_12months=int(25000 * 12 * self.DISCOUNT_RATE),  # ₦285,000 (save 5%)
                credits_monthly=35,
                # Legacy fields for backward compatibility
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
                # Multi-duration pricing (PRD Section 6 & 7)
                price_ngn_monthly=40000,
                price_ngn_3months=int(40000 * 3 * self.DISCOUNT_RATE),  # ₦114,000 (save 5%)
                price_ngn_6months=int(40000 * 6 * self.DISCOUNT_RATE),  # ₦228,000 (save 5%)
                price_ngn_12months=int(40000 * 12 * self.DISCOUNT_RATE),  # ₦456,000 (save 5%)
                credits_monthly=50,
                # Legacy fields for backward compatibility
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
                # Multi-duration pricing (PRD Section 6 & 7)
                price_ngn_monthly=80000,
                price_ngn_3months=int(80000 * 3 * self.DISCOUNT_RATE),  # ₦228,000 (save 5%)
                price_ngn_6months=int(80000 * 6 * self.DISCOUNT_RATE),  # ₦456,000 (save 5%)
                price_ngn_12months=int(80000 * 12 * self.DISCOUNT_RATE),  # ₦912,000 (save 5%)
                credits_monthly=100,
                # Legacy fields for backward compatibility
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
                # Custom plan is pay-per-credit, no multi-duration pricing
                price_ngn_monthly=750,
                price_ngn_3months=750,
                price_ngn_6months=750,
                price_ngn_12months=750,
                credits_monthly=1,
                # Legacy fields for backward compatibility
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

        print(f"✅ Initialized {len(tiers)} subscription tiers with multi-duration pricing")

    async def get_all_tiers(self, active_only: bool = True) -> List[SubscriptionTier]:
        """
        Get all available subscription tiers
        PRD Section 5: Plan Structure
        Returns unique tiers by tier_id (removes duplicates)
        """
        query = {"is_active": True} if active_only else {}
        cursor = self.subscription_tiers_collection.find(query).sort("price_ngn", 1)

        tiers = []
        seen_tier_ids = set()

        async for doc in cursor:
            tier_id = doc.get("tier_id")
            # Skip duplicates - only keep first occurrence of each tier_id
            if tier_id in seen_tier_ids:
                continue

            seen_tier_ids.add(tier_id)
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
            next_renewal=wallet.next_renewal,
            billing_cycle=wallet.billing_cycle  # PRD 8.1: Include billing cycle
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

    # ==================== PRD 8.3: Subscription Lifecycle - Auto-Expiry ====================

    async def expire_subscriptions(self) -> dict:
        """
        Auto-expire subscriptions past their end_date
        PRD Section 8.3: Subscription Lifecycle - Auto-expire after end_date

        This method:
        1. Finds all subscriptions where end_date has passed
        2. Sets subscription_tier to None
        3. Sets subscription_credits to 0
        4. Keeps bonus_credits intact
        5. Updates credits_remaining to only bonus credits

        Returns count of expired subscriptions
        """
        from datetime import datetime

        now = datetime.utcnow()

        # Find all wallets with active subscriptions that have passed end_date
        expired_wallets = []
        cursor = credit_service.user_credits_collection.find({
            "subscription_tier": {"$ne": None},
            "end_date": {"$lte": now}
        })

        async for wallet in cursor:
            expired_wallets.append(wallet)

        if not expired_wallets:
            print(f"⏰ Subscription expiry check: No expired subscriptions found")
            return {"expired_count": 0, "message": "No expired subscriptions"}

        print(f"⏰ Found {len(expired_wallets)} expired subscriptions")

        # Expire each subscription
        expired_count = 0
        for wallet in expired_wallets:
            user_id = wallet["user_id"]
            tier_id = wallet.get("subscription_tier")
            end_date = wallet.get("end_date")
            bonus_credits = wallet.get("bonus_credits", 0)

            # Update wallet: remove subscription, keep bonus credits
            result = await credit_service.user_credits_collection.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "subscription_tier": None,
                        "subscription_credits": 0,
                        "total_credits": bonus_credits,
                        "credits_remaining": bonus_credits,
                        "billing_cycle": "monthly",
                        "start_date": None,
                        "end_date": None,
                        "next_renewal": None,
                        "updated_at": datetime.utcnow()
                    }
                }
            )

            if result.modified_count > 0:
                expired_count += 1
                print(f"✅ Expired subscription for user {user_id}: {tier_id} (ended {end_date.date()})")

        print(f"✅ Expired {expired_count}/{len(expired_wallets)} subscriptions")

        return {
            "expired_count": expired_count,
            "message": f"Successfully expired {expired_count} subscription(s)"
        }


# Singleton instance
subscription_service = SubscriptionService()
