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
import json
import os
import subprocess
import tempfile
from math import ceil
from typing import Optional, TypedDict

from app.core.config import settings
from app.database import get_db
from app.services.CreditService import credit_service
from app.services.TrialService import trial_service

PLATFORM_SETTINGS_COLLECTION = "platform_settings"
VIDEO_EDIT_RATE_KEY = "video_edit_credits_per_minute"


async def get_video_edit_rate() -> int:
    """
    PRD §12 NFR: "The pricing rate should be configurable by an administrator
    rather than permanently hard-coded." Reads the live rate from
    platform_settings; falls back to the settings.py default only if no
    admin has ever set one (fresh install / before first configuration).
    """
    db = get_db()
    doc = await db[PLATFORM_SETTINGS_COLLECTION].find_one({"key": VIDEO_EDIT_RATE_KEY})
    if doc and isinstance(doc.get("value"), int) and doc["value"] > 0:
        return doc["value"]
    return settings.VIDEO_EDIT_CREDITS_PER_MINUTE


async def set_video_edit_rate(rate: int, updated_by: str) -> int:
    """Admin-only write path — the caller (complete_social_manager.py's
    PATCH /video-editing/pricing) gates this behind the JANE_ADS_ADMIN_EMAILS
    allowlist before calling here. Upserts so the very first admin change
    creates the document."""
    if rate <= 0:
        raise ValueError("rate must be a positive integer")
    from datetime import datetime
    db = get_db()
    await db[PLATFORM_SETTINGS_COLLECTION].update_one(
        {"key": VIDEO_EDIT_RATE_KEY},
        {"$set": {"value": rate, "updated_by": updated_by, "updated_at": datetime.utcnow()}},
        upsert=True,
    )
    return rate


def probe_duration_strict(video_bytes: bytes) -> Optional[float]:
    """
    Billing-only duration probe. Unlike video_production_service._probe_duration
    (which returns a fabricated 120.0s on any failure — fine for its own
    pacing/analysis use, wrong for billing), this returns None on failure so
    callers can surface PRD §11's "Video Duration Cannot Be Detected" error
    and decline to charge, instead of silently billing a made-up duration
    for a file that may not even be a valid video.
    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(video_bytes)
            tmp = f.name
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", tmp],
                capture_output=True, text=True, timeout=30,
            )
        finally:
            os.unlink(tmp)
        info = json.loads(result.stdout)
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "video":
                duration = stream.get("duration")
                if duration is not None and float(duration) > 0:
                    return float(duration)
        return None
    except Exception:
        return None


def duration_undetectable_response() -> dict:
    """PRD §11: Video Duration Cannot Be Detected."""
    return {
        "status": False,
        "responseCode": 422,
        "responseMessage": "We could not determine the duration of this video. Please upload the video again.",
        "responseData": None,
    }


class VideoBillingResult(TypedDict):
    success: bool
    is_trial: bool
    duration_seconds: float
    billable_minutes: int
    credits_charged: int
    credits_remaining: Optional[int]
    error: Optional[str]


async def compute_video_credits(duration_seconds: float) -> tuple[int, int]:
    """
    PRD §5: Credits required = ceiling(duration in minutes) x rate per minute.
    Rate is the live, admin-configurable value (see get_video_edit_rate) —
    not a hard-coded constant. Returns (billable_minutes, credits_required).
    """
    rate = await get_video_edit_rate()
    billable_minutes = max(1, ceil(max(duration_seconds, 0) / 60))
    credits_required = billable_minutes * rate
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
    billable_minutes, credits_required = await compute_video_credits(duration_seconds)

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


async def insufficient_credits_response(billing: VideoBillingResult) -> dict:
    """PRD §11 error copy + UI requirements (§9): cost preview + balance."""
    rate = await get_video_edit_rate()
    needed = billing["billable_minutes"] * rate
    if billing.get("is_trial"):
        message = (
            "Your video editing free trial doesn't have enough credits left for "
            f"this video ({billing['credits_charged'] or needed} credits needed)."
        )
    else:
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
            "credits_required": needed,
            "credits_remaining": billing.get("credits_remaining"),
            "rate_per_minute": rate,
            "upgrade_url": "/pricing",
        },
    }
