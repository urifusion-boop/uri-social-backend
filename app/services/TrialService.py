"""
Free Trial Service
Aligned with Free Trial PRD V1

Handles:
- Trial activation on signup (PRD 5.1)
- Trial status checks (PRD 5.3)
- Trial credit deduction (PRD 5.2)
- Trial expiry logic (PRD 2.3)
- Abuse prevention (PRD 8)
- Wallet creation on trial expiry
"""
from typing import Optional
from datetime import datetime, timedelta
from math import ceil
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import get_db
from app.domain.models.billing_models import (
    UserTrial,
    TrialStatusResponse,
    CreditTransaction,
    UserCreditWallet,
)

# Trial configuration constants (PRD Section 2)
TRIAL_DURATION_DAYS = 7  # Extended from 3 to 7 days
TRIAL_CREDITS = 10
LOW_CREDIT_THRESHOLD = 2  # PRD 6.2: Warning when credits ≤ 2


class TrialService:
    """
    Free trial lifecycle management
    PRD Sections 2-8: Trial activation, usage, expiry, and abuse prevention
    """

    def __init__(self):
        self._db: Optional[AsyncIOMotorDatabase] = None

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            self._db = get_db()
        return self._db

    @property
    def trials_collection(self):
        return self.db["user_trials"]

    @property
    def credit_transactions_collection(self):
        return self.db["credit_transactions"]

    @property
    def user_credits_collection(self):
        return self.db["user_credits"]

    # ==================== Trial Expiry Handling ====================

    async def _ensure_wallet_on_trial_expiry(self, user_id: str) -> None:
        """
        Create a credit wallet with 0 credits when trial expires.
        This ensures users without subscriptions are properly blocked.
        """
        # Check if user already has a wallet
        existing_wallet = await self.user_credits_collection.find_one({"user_id": user_id})
        if existing_wallet:
            return  # User already has a wallet, no need to create

        # Create wallet with 0 credits
        now = datetime.utcnow()
        wallet = UserCreditWallet(
            user_id=user_id,
            bonus_credits=0,
            subscription_credits=0,
            total_credits=0,
            credits_used=0,
            credits_remaining=0,
            subscription_tier=None,
            next_renewal=None,
            created_at=now,
            updated_at=now
        )

        await self.user_credits_collection.insert_one(wallet.dict(exclude_none=True))
        print(f"[TrialService] Created 0-credit wallet for user {user_id} after trial expiry")

    # ==================== PRD 5.1: Trial Activation ====================

    async def activate_trial(self, user_id: str) -> TrialStatusResponse:
        """
        Activate free trial for a new user.
        PRD 5.1: On successful signup, assign trial.

        Abuse prevention (PRD 8): One trial per user_id.
        """
        # Check if user already had a trial
        existing = await self.trials_collection.find_one({"user_id": user_id})
        if existing:
            # PRD 8: One trial per user — return current status
            return await self.get_trial_status(user_id)

        now = datetime.utcnow()
        trial = UserTrial(
            user_id=user_id,
            is_trial=True,
            trial_start_date=now,
            trial_end_date=now + timedelta(days=TRIAL_DURATION_DAYS),
            trial_credits=TRIAL_CREDITS,
            credits_remaining=TRIAL_CREDITS,
            trial_used=True,
            created_at=now,
        )

        await self.trials_collection.insert_one(trial.dict(exclude_none=True))

        # Log the trial credit allocation
        await self.credit_transactions_collection.insert_one(
            CreditTransaction(
                user_id=user_id,
                type="trial",
                amount=TRIAL_CREDITS,
                balance_before=0,
                balance_after=TRIAL_CREDITS,
                reason="trial",
                created_at=now,
            ).dict(exclude_none=True)
        )

        return await self._build_status(trial.dict())

    # ==================== PRD 5.3: Expiry Check ====================

    async def get_trial_status(self, user_id: str) -> TrialStatusResponse:
        """
        Get current trial status.
        PRD 5.3: Run check on login, content generation, cron.
        PRD 4.2: trial_active = current_time < trial_end_date AND credits_remaining > 0
        """
        trial_doc = await self.trials_collection.find_one({"user_id": user_id})

        if not trial_doc:
            return TrialStatusResponse(
                is_trial=False,
                trial_active=False,
                trial_expired=False,
                trial_already_used=False,
            )

        return await self._build_status(trial_doc)

    async def _build_status(self, trial_doc: dict) -> TrialStatusResponse:
        """Build TrialStatusResponse from a trial document."""
        now = datetime.utcnow()
        end_date = trial_doc["trial_end_date"]
        credits_remaining = trial_doc["credits_remaining"]

        # PRD 4.2: trial_active = current_time < trial_end_date AND credits_remaining > 0
        time_remaining = end_date - now
        is_active = now < end_date and credits_remaining > 0

        total_seconds_remaining = max(0, time_remaining.total_seconds())
        days_remaining = ceil(total_seconds_remaining / 86400) if total_seconds_remaining > 0 else 0
        hours_remaining = max(0, int(time_remaining.total_seconds() // 3600))
        trial_expired = not is_active and trial_doc.get("trial_used", False)

        return TrialStatusResponse(
            is_trial=trial_doc.get("is_trial", False),
            trial_active=is_active,
            trial_start_date=trial_doc.get("trial_start_date"),
            trial_end_date=end_date,
            trial_credits=trial_doc.get("trial_credits", TRIAL_CREDITS),
            credits_remaining=credits_remaining,
            days_remaining=days_remaining,
            hours_remaining=hours_remaining,
            trial_expired=trial_expired,
            trial_already_used=trial_doc.get("trial_used", False),
            low_credit_warning=credits_remaining <= LOW_CREDIT_THRESHOLD and credits_remaining > 0,
        )

    # ==================== PRD 5.2: Credit Deduction ====================

    async def deduct_trial_credit(
        self,
        user_id: str,
        campaign_id: str,
        reason: str = "campaign_generation",
    ) -> bool:
        """
        Deduct 1 trial credit.
        PRD 5.2: Works same as paid users — deduct 1 credit per campaign.
        Returns False if trial expired or no credits left.
        """
        trial_doc = await self.trials_collection.find_one({"user_id": user_id})
        if not trial_doc:
            return False

        now = datetime.utcnow()
        end_date = trial_doc["trial_end_date"]
        credits_remaining = trial_doc["credits_remaining"]

        # PRD 2.3: Trial ends when 3 days elapsed OR credits = 0
        if now >= end_date or credits_remaining <= 0:
            # Trial expired - ensure user has a wallet with 0 credits
            await self._ensure_wallet_on_trial_expiry(user_id)
            return False

        balance_before = credits_remaining
        new_remaining = credits_remaining - 1

        # If this deduction brings credits to 0, create wallet for when trial fully expires
        if new_remaining == 0:
            await self._ensure_wallet_on_trial_expiry(user_id)

        await self.trials_collection.update_one(
            {"user_id": user_id},
            {"$set": {"credits_remaining": new_remaining}},
        )

        # Log deduction
        await self.credit_transactions_collection.insert_one(
            CreditTransaction(
                user_id=user_id,
                type="deduction",
                amount=-1,
                balance_before=balance_before,
                balance_after=new_remaining,
                reason=reason,
                campaign_id=campaign_id,
                created_at=now,
            ).dict(exclude_none=True)
        )

        return True

    # ==================== PRD 5.3 & 5.4: Access Control ====================

    async def can_generate(self, user_id: str) -> bool:
        """
        Check if trial user can generate content.
        PRD 5.4: If trial inactive → block content generation.
        Creates wallet with 0 credits if trial expired.
        """
        trial_doc = await self.trials_collection.find_one({"user_id": user_id})
        if not trial_doc:
            return False

        now = datetime.utcnow()
        is_active = now < trial_doc["trial_end_date"] and trial_doc["credits_remaining"] > 0

        # If trial expired, ensure wallet exists with 0 credits
        if not is_active:
            await self._ensure_wallet_on_trial_expiry(user_id)

        return is_active

    async def has_active_trial(self, user_id: str) -> bool:
        """
        Check if user has an active trial (not expired, has credits).
        Creates wallet with 0 credits if trial expired.
        """
        return await self.can_generate(user_id)

    async def has_used_trial(self, user_id: str) -> bool:
        """PRD 8: Check if user has already used their trial (abuse prevention)."""
        trial_doc = await self.trials_collection.find_one({"user_id": user_id})
        return trial_doc is not None


# Module-level singleton
trial_service = TrialService()
