"""
Credit Management Service
Strictly aligned with PRICING PRD V1

Handles all credit operations:
- Balance checking (PRD 7.1)
- Credit deduction (PRD 7.2)
- Credit allocation (PRD 6.3)
- Low credit warnings (PRD 7.3)
- Transaction logging (PRD 11)
"""
from typing import Optional, List, Dict
from datetime import datetime, timedelta
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import get_db
from app.domain.models.billing_models import (
    UserCreditWallet,
    CreditTransaction,
    CreditBalanceResponse
)


class CreditService:
    """
    Credit balance and transaction management
    PRD Sections 7 & 11: Credit Balance System & System Limits
    """

    def __init__(self):
        self._db: Optional[AsyncIOMotorDatabase] = None

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            self._db = get_db()
        return self._db

    @property
    def user_credits_collection(self):
        return self.db["user_credits"]

    @property
    def credit_transactions_collection(self):
        return self.db["credit_transactions"]

    # ==================== PRD 7.1: User Wallet ====================

    async def get_user_wallet(self, user_id: str) -> Optional[UserCreditWallet]:
        """
        Get user credit wallet
        Returns None if user has no wallet (never subscribed)
        """
        wallet_doc = await self.user_credits_collection.find_one({"user_id": user_id})
        if not wallet_doc:
            return None

        wallet_doc["_id"] = str(wallet_doc["_id"])

        # Recalculate total_credits and credits_remaining from source fields
        # This ensures correct values even if database has stale computed fields
        # NOTE: subscription_credits and bonus_credits already have deductions applied,
        # so credits_remaining = bonus_credits + subscription_credits (no need to subtract credits_used)
        bonus_credits = wallet_doc.get("bonus_credits", 0)
        subscription_credits = wallet_doc.get("subscription_credits", 0)

        wallet_doc["total_credits"] = bonus_credits + subscription_credits
        wallet_doc["credits_remaining"] = bonus_credits + subscription_credits

        return UserCreditWallet(**wallet_doc)

    async def get_credit_balance(self, user_id: str) -> CreditBalanceResponse:
        """
        Get user credit balance with low credit warning
        PRD 7.3: Low Credit Warning when credits ≤ 3
        """
        wallet = await self.get_user_wallet(user_id)

        if not wallet:
            # User never subscribed - return zero balance
            return CreditBalanceResponse(
                total_credits=0,
                credits_used=0,
                credits_remaining=0,
                subscription_tier=None,
                next_renewal=None,
                low_credit_warning=True  # Show warning to subscribe
            )

        # PRD 7.3: Trigger warning when credits ≤ 3
        low_credit_warning = wallet.credits_remaining <= 3

        return CreditBalanceResponse(
            total_credits=wallet.total_credits,
            credits_used=wallet.credits_used,
            credits_remaining=wallet.credits_remaining,
            subscription_tier=wallet.subscription_tier,
            next_renewal=wallet.next_renewal,
            low_credit_warning=low_credit_warning
        )

    async def create_wallet(
        self,
        user_id: str,
        total_credits: int,
        subscription_tier: str
    ) -> UserCreditWallet:
        """
        Create initial credit wallet for new subscriber
        """
        wallet = UserCreditWallet(
            user_id=user_id,
            bonus_credits=0,
            subscription_credits=total_credits,
            total_credits=total_credits,
            credits_used=0,
            credits_remaining=total_credits,
            subscription_tier=subscription_tier,
            next_renewal=datetime.utcnow() + timedelta(days=30),  # Monthly billing
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )

        await self.user_credits_collection.insert_one(wallet.dict(exclude_none=True))
        return wallet

    async def add_bonus_credits(
        self,
        user_id: str,
        bonus_amount: int,
        reason: str = "bonus"
    ) -> UserCreditWallet:
        """
        Add bonus credits to user wallet
        Bonus credits are consumed first and never expire
        """
        wallet = await self.get_user_wallet(user_id)

        if not wallet:
            # Create wallet with bonus credits only
            new_wallet = UserCreditWallet(
                user_id=user_id,
                bonus_credits=bonus_amount,
                subscription_credits=0,
                total_credits=bonus_amount,
                credits_used=0,
                credits_remaining=bonus_amount,
                subscription_tier=None,
                next_renewal=None,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            await self.user_credits_collection.insert_one(new_wallet.dict(exclude_none=True))

            # Log bonus transaction
            await self.credit_transactions_collection.insert_one(
                CreditTransaction(
                    user_id=user_id,
                    type="bonus",
                    amount=bonus_amount,
                    balance_before=0,
                    balance_after=bonus_amount,
                    reason=reason,
                    created_at=datetime.utcnow()
                ).dict(exclude_none=True)
            )

            return new_wallet

        # Add to existing wallet
        current_bonus = getattr(wallet, 'bonus_credits', 0)
        new_bonus = current_bonus + bonus_amount
        new_total = new_bonus + getattr(wallet, 'subscription_credits', 0)
        new_remaining = new_total - wallet.credits_used

        await self.user_credits_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "bonus_credits": new_bonus,
                    "total_credits": new_total,
                    "credits_remaining": new_remaining,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        # Log bonus transaction
        await self.credit_transactions_collection.insert_one(
            CreditTransaction(
                user_id=user_id,
                type="bonus",
                amount=bonus_amount,
                balance_before=wallet.credits_remaining,
                balance_after=new_remaining,
                reason=reason,
                created_at=datetime.utcnow()
            ).dict(exclude_none=True)
        )

        wallet.bonus_credits = new_bonus
        wallet.total_credits = new_total
        wallet.credits_remaining = new_remaining
        return wallet

    # ==================== PRD 7.2: Deduction Logic ====================

    async def check_sufficient_credits(self, user_id: str, required: int = 1) -> bool:
        """
        Check if user has sufficient credits
        PRD 7.2: if credits_remaining >= 1: allow, else: block action

        Legacy users (without wallet) get unlimited access for backward compatibility
        """
        wallet = await self.get_user_wallet(user_id)

        if not wallet:
            # Legacy user from before billing system - allow unlimited access
            return True

        return wallet.credits_remaining >= required

    async def deduct_credit(
        self,
        user_id: str,
        campaign_id: str,
        reason: str = "campaign_generation",
        retry_count: int = 0
    ) -> bool:
        """
        Deduct 1 credit from user balance
        PRD 7.2: Deduction Logic
        PRD 11: Must log all credit usage events

        Credit deduction priority:
        1. Subscription credits (consumed first - they expire monthly)
        2. Bonus credits (consumed second - they never expire)

        This ensures subscription credits are used before expiry.

        Legacy users (without wallet) are not deducted - backward compatibility

        Returns:
            bool: True if deduction successful, False if insufficient credits
        """
        wallet = await self.get_user_wallet(user_id)

        if not wallet:
            # Legacy user from before billing system - skip deduction
            return True

        if wallet.credits_remaining < 1:
            return False

        # Get current credit breakdown
        current_bonus = getattr(wallet, 'bonus_credits', 0)
        current_subscription = getattr(wallet, 'subscription_credits', 0)
        balance_before = wallet.credits_remaining

        # Deduct from subscription first (use before expiry), then bonus (never expires)
        if current_subscription > 0:
            # Deduct from subscription credits first (use before monthly reset)
            new_subscription = current_subscription - 1
            new_bonus = current_bonus
        else:
            # Deduct from bonus credits (only when subscription is depleted)
            new_subscription = 0
            new_bonus = current_bonus - 1

        # Calculate new totals
        new_total = new_bonus + new_subscription
        new_credits_used = wallet.credits_used + 1
        new_credits_remaining = new_total

        # Update wallet
        await self.user_credits_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "bonus_credits": new_bonus,
                    "subscription_credits": new_subscription,
                    "total_credits": new_total,
                    "credits_used": new_credits_used,
                    "credits_remaining": new_credits_remaining,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        # PRD 11: Log all credit usage events
        transaction = CreditTransaction(
            user_id=user_id,
            type="deduction",
            amount=-1,
            balance_before=balance_before,
            balance_after=new_credits_remaining,
            reason=reason,
            campaign_id=campaign_id,
            retry_count=retry_count,
            created_at=datetime.utcnow()
        )

        await self.credit_transactions_collection.insert_one(
            transaction.dict(exclude_none=True)
        )

        return True

    # ==================== PRD 6.3: Payment Flow - Credit Allocation ====================

    async def allocate_credits(
        self,
        user_id: str,
        tier_id: str,
        credits: int,
        reason: str = "subscription"
    ) -> UserCreditWallet:
        """
        Allocate credits to user after successful payment
        PRD 6.3: On success: Assign credits, Activate subscription
        PRD 5.2: Subscription credits reset every billing cycle (no rollover)
        Bonus credits are preserved and consumed first
        """
        existing_wallet = await self.get_user_wallet(user_id)

        if existing_wallet:
            # Existing subscriber - renewal or upgrade
            balance_before = existing_wallet.credits_remaining

            # Get current bonus and subscription credits
            current_bonus = getattr(existing_wallet, 'bonus_credits', 0)
            current_subscription = getattr(existing_wallet, 'subscription_credits', 0)
            current_used = existing_wallet.credits_used

            # Check if this is a renewal (same tier) or new purchase
            is_renewal = existing_wallet.subscription_tier == tier_id

            if is_renewal:
                # Monthly renewal: Reset subscription credits, preserve bonus credits
                new_bonus = current_bonus
                new_subscription = credits
                new_used = 0  # Reset usage counter
            else:
                # New purchase/upgrade: Keep bonus, add new subscription credits
                new_bonus = current_bonus
                new_subscription = credits
                new_used = current_used  # Keep existing usage

            # Calculate totals
            new_total = new_bonus + new_subscription
            new_remaining = new_total - new_used

            await self.user_credits_collection.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "bonus_credits": new_bonus,
                        "subscription_credits": new_subscription,
                        "total_credits": new_total,
                        "credits_used": new_used,
                        "credits_remaining": new_remaining,
                        "subscription_tier": tier_id,
                        "next_renewal": datetime.utcnow() + timedelta(days=30),
                        "updated_at": datetime.utcnow()
                    }
                }
            )

            # Log allocation transaction
            transaction = CreditTransaction(
                user_id=user_id,
                type="allocation",
                amount=credits,
                balance_before=balance_before,
                balance_after=new_remaining,
                reason=reason,
                created_at=datetime.utcnow()
            )

        else:
            # New subscriber - create wallet
            wallet = await self.create_wallet(user_id, credits, tier_id)

            # Log initial allocation
            transaction = CreditTransaction(
                user_id=user_id,
                type="allocation",
                amount=credits,
                balance_before=0,
                balance_after=credits,
                reason=reason,
                created_at=datetime.utcnow()
            )

        await self.credit_transactions_collection.insert_one(
            transaction.dict(exclude_none=True)
        )

        return await self.get_user_wallet(user_id)

    # ==================== Transaction History ====================

    async def get_transaction_history(
        self,
        user_id: str,
        limit: int = 50
    ) -> List[Dict]:
        """
        Get user's credit transaction history
        PRD 11: Must log all credit usage events & retry actions
        """
        cursor = self.credit_transactions_collection.find(
            {"user_id": user_id}
        ).sort("created_at", -1).limit(limit)

        transactions = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            transactions.append(doc)

        return transactions

    # ==================== PRD 8: Credit Exhaustion Behavior ====================

    async def is_blocked(self, user_id: str) -> bool:
        """
        Check if user should be blocked from generating content
        PRD 8: When credits = 0, block new campaign generation
        """
        wallet = await self.get_user_wallet(user_id)

        if not wallet:
            return True  # No wallet = never subscribed = blocked

        return wallet.credits_remaining == 0

    # ==================== Admin/System Methods ====================

    async def refund_credit(
        self,
        user_id: str,
        campaign_id: str,
        reason: str = "refund"
    ) -> bool:
        """
        Refund 1 credit to user (for failed campaigns, etc.)
        PRD Section 2 allows for refunds in transaction types
        """
        wallet = await self.get_user_wallet(user_id)

        if not wallet:
            return False

        balance_before = wallet.credits_remaining
        new_credits_used = max(0, wallet.credits_used - 1)
        new_credits_remaining = wallet.total_credits - new_credits_used

        await self.user_credits_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "credits_used": new_credits_used,
                    "credits_remaining": new_credits_remaining,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        # Log refund transaction
        transaction = CreditTransaction(
            user_id=user_id,
            type="refund",
            amount=1,
            balance_before=balance_before,
            balance_after=new_credits_remaining,
            reason=reason,
            campaign_id=campaign_id,
            created_at=datetime.utcnow()
        )

        await self.credit_transactions_collection.insert_one(
            transaction.dict(exclude_none=True)
        )

        return True


# Singleton instance
credit_service = CreditService()
