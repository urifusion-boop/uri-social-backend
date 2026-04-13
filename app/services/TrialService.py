"""
Free Trial Service
Aligned with Free Trial PRD V1

Handles:
- Trial activation on signup (PRD 5.1)
- Trial status checks (PRD 5.3)
- Trial credit deduction (PRD 5.2)
- Trial expiry logic (PRD 2.3)
- Abuse prevention (PRD 8)
"""
from typing import Optional
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import get_db
from app.domain.models.billing_models import (
    UserTrial,
    TrialStatusResponse,
    CreditTransaction,
)

# Trial configuration constants (PRD Section 2)
TRIAL_DURATION_DAYS = 3
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

        days_remaining = max(0, time_remaining.days)
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
            return False

        balance_before = credits_remaining
        new_remaining = credits_remaining - 1

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
        """
        trial_doc = await self.trials_collection.find_one({"user_id": user_id})
        if not trial_doc:
            return False

        now = datetime.utcnow()
        return now < trial_doc["trial_end_date"] and trial_doc["credits_remaining"] > 0

    async def has_active_trial(self, user_id: str) -> bool:
        """Check if user has an active trial (not expired, has credits)."""
        return await self.can_generate(user_id)

    async def has_used_trial(self, user_id: str) -> bool:
        """PRD 8: Check if user has already used their trial (abuse prevention)."""
        trial_doc = await self.trials_collection.find_one({"user_id": user_id})
        return trial_doc is not None


# Module-level singleton
trial_service = TrialService()
