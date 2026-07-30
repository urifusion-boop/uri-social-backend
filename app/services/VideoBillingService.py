"""
Video Editing Billing Service
PRD: Video Billing PRD.pdf

Charges 4 credits per billable minute of the video being edited (partial
minutes rounded up), the same way for every video-editing entry point
(produce-video, zapcap-produce, submagic-produce). Reuses the existing
personal credit wallet (CreditService/TrialService) — the same wallet
`generate_content` already bills to — rather than the brand/agency wallet,
since this PRD explicitly treats it as "the existing Uri credit wallet"
with no mention of brand-level billing.

Trial and paid users are mutually exclusive billing paths (matching the
existing convention in generate_content): an active trial user is charged
from their trial balance; if their trial doesn't have enough credits left
for this specific job, the request is blocked rather than silently falling
back to the paid wallet.
"""
from math import ceil
from typing import Optional, TypedDict

from app.core.config import settings
from app.services.CreditService import credit_service
from app.services.TrialService import trial_service


class VideoBillingResult(TypedDict):
    success: bool
    is_trial: bool
    duration_seconds: float
    billable_minutes: int
    credits_charged: int
    credits_remaining: Optional[int]
    error: Optional[str]


def compute_video_credits(duration_seconds: float) -> tuple[int, int]:
    """
    PRD §5: Credits required = ceiling(duration in minutes) x rate per minute.
    Returns (billable_minutes, credits_required).
    """
    billable_minutes = max(1, ceil(max(duration_seconds, 0) / 60))
    credits_required = billable_minutes * settings.VIDEO_EDIT_CREDITS_PER_MINUTE
    return billable_minutes, credits_required


async def charge_for_video_job(
    user_id: str,
    duration_seconds: float,
    job_id: str,
    reason: str,
) -> VideoBillingResult:
    """
    PRD Steps 5-7: check free-trial eligibility, then credit balance, then
    deduct. Returns a result dict — callers must check `success` before
    proceeding with the actual (paid) render submission, and must call
    `refund_video_job` with this same result if the submission then fails
    for a system reason.
    """
    billable_minutes, credits_required = compute_video_credits(duration_seconds)

    is_trial = await trial_service.has_active_trial(user_id)

    if is_trial:
        deducted = await trial_service.deduct_trial_credit(
            user_id=user_id, campaign_id=job_id, reason=reason, amount=credits_required
        )
        if not deducted:
            return VideoBillingResult(
                success=False,
                is_trial=True,
                duration_seconds=duration_seconds,
                billable_minutes=billable_minutes,
                credits_charged=0,
                credits_remaining=None,
                error="insufficient_trial_credits",
            )
        return VideoBillingResult(
            success=True,
            is_trial=True,
            duration_seconds=duration_seconds,
            billable_minutes=billable_minutes,
            credits_charged=credits_required,
            credits_remaining=None,
            error=None,
        )

    has_credits = await credit_service.check_sufficient_credits(user_id, required=credits_required)
    if not has_credits:
        wallet = await credit_service.get_user_wallet(user_id)
        return VideoBillingResult(
            success=False,
            is_trial=False,
            duration_seconds=duration_seconds,
            billable_minutes=billable_minutes,
            credits_charged=0,
            credits_remaining=wallet.credits_remaining if wallet else 0,
            error="insufficient_credits",
        )

    deducted = await credit_service.deduct_credit(
        user_id=user_id, campaign_id=job_id, reason=reason, amount=credits_required
    )
    if not deducted:
        # Lost a race against another concurrent deduction — surface the
        # same insufficient-credits error rather than proceeding unbilled.
        return VideoBillingResult(
            success=False,
            is_trial=False,
            duration_seconds=duration_seconds,
            billable_minutes=billable_minutes,
            credits_charged=0,
            credits_remaining=None,
            error="insufficient_credits",
        )

    return VideoBillingResult(
        success=True,
        is_trial=False,
        duration_seconds=duration_seconds,
        billable_minutes=billable_minutes,
        credits_charged=credits_required,
        credits_remaining=None,
        error=None,
    )


async def refund_video_job(user_id: str, job_id: str, billing: VideoBillingResult, reason: str) -> None:
    """PRD Step 7 / FR-10: auto-refund when a billed job fails for a Uri system
    reason (not a bad user upload — that's rejected before billing happens)."""
    if not billing.get("success") or not billing.get("credits_charged"):
        return
    amount = billing["credits_charged"]
    if billing.get("is_trial"):
        await trial_service.refund_trial_credit(user_id=user_id, campaign_id=job_id, reason=reason, amount=amount)
    else:
        await credit_service.refund_credit(user_id=user_id, campaign_id=job_id, reason=reason, amount=amount)


def insufficient_credits_response(billing: VideoBillingResult) -> dict:
    """PRD §11 error copy + UI requirements (§9): cost preview + balance."""
    if billing.get("is_trial"):
        message = (
            "Your video editing free trial doesn't have enough credits left for "
            f"this video ({billing['credits_charged'] or billing['billable_minutes'] * settings.VIDEO_EDIT_CREDITS_PER_MINUTE} credits needed)."
        )
    else:
        needed = billing["billable_minutes"] * settings.VIDEO_EDIT_CREDITS_PER_MINUTE
        message = (
            f"This video requires {needed} credits, but you currently have "
            f"{billing.get('credits_remaining') or 0} credits. Purchase more credits to continue."
        )
    return {
        "status": False,
        "responseCode": 402,
        "responseMessage": message,
        "responseData": {
            "duration_seconds": billing["duration_seconds"],
            "billable_minutes": billing["billable_minutes"],
            "credits_required": billing["billable_minutes"] * settings.VIDEO_EDIT_CREDITS_PER_MINUTE,
            "credits_remaining": billing.get("credits_remaining"),
            "rate_per_minute": settings.VIDEO_EDIT_CREDITS_PER_MINUTE,
            "upgrade_url": "/pricing",
        },
    }
