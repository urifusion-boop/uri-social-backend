"""
Jane + Ads — mid-flight monitoring (campaign roadmap, Tier 4).

PRD §7: Jane raises problems before the user notices, and never ends a campaign
silently. Runs periodically (wired into the existing notification scheduler —
see app/services/notification_scheduler.py) over every launched campaign,
comparing real delivery against a naive expected pace, and detecting campaigns
whose real end_time (Tier 0's fix) has just passed.

Deliberately simple: no ML, no trend modelling — one heuristic each for
"stuck" (spending with zero results after a couple of days) and "just ended".
Honest and explainable beats false precision here, matching the PRD's own
"roughly N people (estimate)" framing elsewhere.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def _as_aware_utc(dt) -> Optional[datetime]:
    """Mongo/ISO datetimes arrive naive sometimes, aware other times — normalize
    to aware UTC so subtraction/comparison never raises."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_underperforming(record: dict, summary: dict) -> bool:
    """Running >= 2 days with real spend but zero conversations yet."""
    created_at = _as_aware_utc(record.get("created_at"))
    if not created_at:
        return False
    days_running = (datetime.now(timezone.utc) - created_at).days
    return days_running >= 2 and summary["spend_ngn"] > 0 and summary["conversations"] == 0


async def _notify(notification_service, *, user_id: str, notification_type: str, subject: str, message: str, campaign_id: str) -> None:
    # _log_notification is NotificationService's shared DB-logging write (no
    # email/whatsapp actually dispatched here — this is a pure in-app entry,
    # which is all GET /notifications and /notifications/unread-count read).
    await notification_service._log_notification(
        user_id=user_id,
        notification_type=notification_type,
        channel="email",
        subject=subject,
        status="sent",
        metadata={"campaign_id": campaign_id, "message": message},
    )


async def check_active_campaigns(db) -> dict:
    """One pass over every campaign we've launched: flag underperformers, detect
    newly-ended campaigns, notify the owning user for both, and self-heal any
    campaign Meta now reports deleted (same rule GET /meta/campaigns already
    applies). Best-effort per campaign — one bad campaign's metrics call never
    blocks the rest."""
    from app.core.config import settings
    from app.services.NotificationService import notification_service
    from .adapters.meta import MetaAdPlatformAdapter

    if not (settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN):
        return {"checked": 0, "flagged": 0, "ended": 0}

    adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
    records = await db["jane_ads_meta_campaigns"].find({}, {"_id": 0}).to_list(length=500)

    checked = flagged = ended = 0
    for r in records:
        campaign_id = r.get("campaign_id")
        user_id = r.get("user_id")
        if not campaign_id or not user_id:
            continue
        try:
            summary = await adapter.fetch_campaign_summary(campaign_id)
        except Exception as e:
            print(f"[monitoring] summary failed for {campaign_id}: {e}", flush=True)
            continue
        checked += 1

        if summary["delivery"] == "Deleted":
            await db["jane_ads_meta_campaigns"].delete_one({"campaign_id": campaign_id})
            continue

        # Campaign-end detection (Tier 4b) — a real end_time has passed and we
        # haven't already told the user. `ended_notified` is a one-way flag so a
        # finished campaign is only ever announced once.
        ends_at = _as_aware_utc(summary.get("ends_at"))
        if ends_at and datetime.now(timezone.utc) > ends_at and not r.get("ended_notified"):
            name = r.get("display_name") or "Your campaign"
            cost_per = summary.get("cost_per_conversation_ngn")
            cost_line = f", at about ₦{cost_per:,.0f} each" if cost_per else ""
            await _notify(
                notification_service, user_id=user_id, notification_type="campaign_ended",
                subject=f"{name} has finished",
                message=(
                    f"{name} finished: {summary['conversations']} people messaged you{cost_line}, "
                    f"₦{summary['spend_ngn']:,.0f} spent total. Want to run it again?"
                ),
                campaign_id=campaign_id,
            )
            await db["jane_ads_meta_campaigns"].update_one({"campaign_id": campaign_id}, {"$set": {"ended_notified": True}})
            ended += 1
            continue

        # Underperformance — only meaningful once actually delivering, and only
        # announced once per campaign (`underperform_notified_at`) so it doesn't
        # re-fire every scheduler tick.
        if summary["delivery"] != "Active" or r.get("underperform_notified_at"):
            continue
        if _is_underperforming(r, summary):
            name = r.get("display_name") or "Your campaign"
            await _notify(
                notification_service, user_id=user_id, notification_type="campaign_update",
                subject=f"{name} could use a tweak",
                message=(
                    f"Quick update — {name} has spent ₦{summary['spend_ngn']:,.0f} so far with no "
                    "conversations yet. Might be worth trying a different photo or widening the area."
                ),
                campaign_id=campaign_id,
            )
            await db["jane_ads_meta_campaigns"].update_one(
                {"campaign_id": campaign_id},
                {"$set": {"underperform_notified_at": datetime.now(timezone.utc)}},
            )
            flagged += 1

    return {"checked": checked, "flagged": flagged, "ended": ended}
