"""
Uri Market Intelligence — outbox delivery worker (PRD §14 engineering note:
"A delivery worker applies recipient preferences, quiet hours, caps and
expiry at send time.").

Reuses Uri's existing EmailService.send_raw_email for the actual send (PRD
§2: "Reuse Uri's existing application shell... and relevant workflows") —
this module owns only the MI-specific eligibility rules the PRD calls out
(topic cooldown, quiet-hours/urgent-override interaction, daily cap,
material-update bypass, expiry recheck), not email transport itself.

"Email failure must not remove the in-app item" (PRD §14) holds structurally
here, not by convention: a FAILED outbox entry never touches mi_insights —
in-app visibility comes from the insight simply being active, entirely
independent of this module's outcome.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.EmailService import email_service
from app.services.PostHogService import track_event
from .models import MINotificationPreferences, NotificationCategory, OutboxDeliveryMode, OutboxStatus

DAILY_IMMEDIATE_EMAIL_CAP = 3   # PRD §14: "three per recipient per day"
TOPIC_COOLDOWN_HOURS = 6        # PRD §14: "a six-hour duplicate-alert cooldown"


async def get_or_create_preferences(db: AsyncIOMotorDatabase, user_id: str, brand_id: str) -> dict:
    existing = await db["mi_preferences"].find_one({"user_id": user_id, "brand_id": brand_id})
    if existing is not None:
        return existing
    prefs = MINotificationPreferences(user_id=user_id, brand_id=brand_id)
    await db["mi_preferences"].insert_one(prefs.dict())
    return prefs.dict()


def _local_hour(prefs: dict, now: Optional[datetime] = None) -> int:
    now = now or datetime.utcnow()
    try:
        tz = ZoneInfo(prefs.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        tz = ZoneInfo("UTC")
    return now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).hour


def _in_quiet_hours(prefs: dict, now: Optional[datetime] = None) -> bool:
    """PRD §14: default quiet hours 21:00-08:00, external delivery only —
    this function IS the external-delivery decision point, so it always
    applies whenever it's called."""
    hour = _local_hour(prefs, now)
    start = prefs.get("quiet_hours_start_local", 21)
    end = prefs.get("quiet_hours_end_local", 8)
    if start == end:
        return False
    if start > end:  # wraps midnight, e.g. 21 -> 8
        return hour >= start or hour < end
    return start <= hour < end


async def _get_user_email(db: AsyncIOMotorDatabase, user_id: str) -> Optional[str]:
    user = await db["users"].find_one({"userId": user_id})
    return (user or {}).get("email") or None


def _render_email(entries_with_insights: list[tuple[dict, dict]]) -> tuple[str, str]:
    """Builds a subject + HTML body strictly from real, already-composed
    insight content — never invents new claims at delivery time, matching
    the anti-hallucination posture insight_composer.py already holds."""
    if len(entries_with_insights) == 1:
        _, insight = entries_with_insights[0]
        subject = f"Uri Market Intelligence: {insight['headline']}"
    else:
        subject = f"Uri Market Intelligence: {len(entries_with_insights)} updates"

    rows = []
    for _, insight in entries_with_insights:
        rows.append(
            "<div style='margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #eee'>"
            f"<div style='font-weight:700'>{insight['headline']}</div>"
            f"<div style='color:#444;margin-top:4px'>{insight['observed_change']}</div>"
            f"<div style='color:#7a1a4a;margin-top:8px'><strong>Suggested next step:</strong> {insight['suggested_next_step']}</div>"
            "</div>"
        )
    body = "<div style='font-family:sans-serif'>" + "".join(rows) + "</div>"
    return subject, body


async def process_outbox(db: AsyncIOMotorDatabase, now: Optional[datetime] = None) -> dict:
    """Drains QUEUED, IMMEDIATE-mode outbox entries. Digest-mode entries
    wait for send_daily_digests instead — they're due at the recipient's
    configured hour, not "as soon as possible"."""
    now = now or datetime.utcnow()
    sent = suppressed = failed = 0

    async for entry in db["mi_outbox"].find(
        {"status": OutboxStatus.QUEUED.value, "delivery_mode": OutboxDeliveryMode.IMMEDIATE.value}
    ):
        outcome = await _process_one_immediate(db, entry, now)
        if outcome == "sent":
            sent += 1
        elif outcome == "failed":
            failed += 1
        else:
            suppressed += 1

    return {"sent": sent, "suppressed": suppressed, "failed": failed}


async def _suppress(db: AsyncIOMotorDatabase, entry: dict, reason: str) -> str:
    await db["mi_outbox"].update_one(
        {"id": entry["id"]}, {"$set": {"status": OutboxStatus.SUPPRESSED.value, "suppression_reason": reason}}
    )
    # PRD §26 instrumentation: excludes raw insight/evidence content, just
    # ids/category/reason — the same "no raw private content" rule §26
    # names explicitly.
    track_event(entry["user_id"], "alert_suppressed", {
        "brand_id": entry["brand_id"], "insight_id": entry["insight_id"], "category": entry["category"], "reason": reason,
    })
    return "suppressed"


async def _process_one_immediate(db: AsyncIOMotorDatabase, entry: dict, now: datetime) -> str:
    prefs = await get_or_create_preferences(db, entry["user_id"], entry["brand_id"])

    insight = await db["mi_insights"].find_one({"id": entry["insight_id"]})
    if insight is None:
        return await _suppress(db, entry, "insight no longer exists")

    # PRD §14: "Recheck expiry before delivery. Drop expired inquiries from
    # pending alerts."
    if entry["category"] == NotificationCategory.QUALIFIED_INQUIRY.value and not insight.get("urgency", {}).get(
        "is_urgent", False
    ):
        return await _suppress(db, entry, "inquiry expired before delivery")

    if insight.get("status") != "active":
        return await _suppress(db, entry, f"insight status is {insight.get('status')}, not active")

    if entry["topic_id"] in prefs.get("muted_topic_ids", []):
        return await _suppress(db, entry, "topic muted")

    if entry["category"] in prefs.get("muted_categories", []):
        return await _suppress(db, entry, "category muted")

    if entry["insight_id"] in prefs.get("snoozed_insight_ids", []):
        return await _suppress(db, entry, "insight snoozed")

    if not prefs.get("email_enabled", False):
        return await _suppress(db, entry, "email not opted in — in-app only")

    is_material_update = entry["category"] == NotificationCategory.MATERIAL_UPDATE.value

    # PRD §14: "Explicit deadline changes, cancellations or retractions can
    # bypass the topic cooldown."
    if not is_material_update:
        cooldown_start = now - timedelta(hours=TOPIC_COOLDOWN_HOURS)
        recent = await db["mi_outbox"].find_one({
            "topic_id": entry["topic_id"], "status": OutboxStatus.SENT.value, "sent_at": {"$gte": cooldown_start},
        })
        if recent is not None:
            return await _suppress(db, entry, "topic cooldown active")

    # PRD §14: "quiet hours still apply unless the user has enabled an
    # urgent override" — this applies to material updates too, unlike the
    # topic cooldown above, which they bypass unconditionally.
    bypass_quiet_hours = is_material_update and prefs.get("urgent_override", False)
    if not bypass_quiet_hours and _in_quiet_hours(prefs, now):
        return await _suppress(db, entry, "quiet hours")

    # PRD §14: "Limit ordinary immediate emails to three per recipient per
    # day. Bundle additional eligible items into the digest."
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = await db["mi_outbox"].count_documents({
        "user_id": entry["user_id"], "status": OutboxStatus.SENT.value,
        "delivery_mode": OutboxDeliveryMode.IMMEDIATE.value, "sent_at": {"$gte": today_start},
    })
    if sent_today >= DAILY_IMMEDIATE_EMAIL_CAP:
        await db["mi_outbox"].update_one(
            {"id": entry["id"]},
            {"$set": {
                "status": OutboxStatus.SUPPRESSED.value,
                "suppression_reason": "daily immediate-email cap reached — visible in-app; will bundle into the next digest",
                "delivery_mode": OutboxDeliveryMode.DIGEST.value,
            }},
        )
        return "suppressed"

    to_email = await _get_user_email(db, entry["user_id"])
    if not to_email:
        await db["mi_outbox"].update_one(
            {"id": entry["id"]}, {"$set": {"status": OutboxStatus.FAILED.value, "failed_reason": "no email on file for recipient"}}
        )
        return "failed"

    subject, body = _render_email([(entry, insight)])
    try:
        ok = await email_service.send_raw_email(to_email, subject, body)
    except Exception as e:
        await db["mi_outbox"].update_one({"id": entry["id"]}, {"$set": {"status": OutboxStatus.FAILED.value, "failed_reason": str(e)}})
        return "failed"

    if ok:
        await db["mi_outbox"].update_one({"id": entry["id"]}, {"$set": {"status": OutboxStatus.SENT.value, "sent_at": now}})
        track_event(entry["user_id"], "alert_sent", {
            "brand_id": entry["brand_id"], "insight_id": entry["insight_id"], "category": entry["category"], "mode": "immediate",
        })
        return "sent"
    await db["mi_outbox"].update_one(
        {"id": entry["id"]}, {"$set": {"status": OutboxStatus.FAILED.value, "failed_reason": "send_raw_email returned False"}}
    )
    return "failed"


async def _try_claim_daily_digest(db: AsyncIOMotorDatabase, user_id: str, now: datetime) -> bool:
    """Same atomic-claim pattern as notification_scheduler.py's
    _try_claim_job_run — necessary for the identical reason: 4 uvicorn
    workers, each ticking their own scheduler."""
    from pymongo.errors import DuplicateKeyError

    claim_id = f"mi_digest:{user_id}:{now.strftime('%Y-%m-%d')}"
    try:
        await db["mi_digest_claims"].insert_one({"_id": claim_id, "user_id": user_id, "claimed_at": now})
        return True
    except DuplicateKeyError:
        return False


async def send_daily_digests(db: AsyncIOMotorDatabase, now: Optional[datetime] = None) -> dict:
    """PRD §14: 'Default daily digest time is 08:00 in the workspace
    timezone... If no useful change exists, skip the digest rather than
    create filler.' Called hourly; only acts for a recipient whose
    configured digest hour matches the current hour in their own timezone."""
    now = now or datetime.utcnow()
    sent = skipped_empty = skipped_not_due = 0

    grouped: dict[tuple[str, str], list[dict]] = {}
    async for entry in db["mi_outbox"].find(
        {"status": OutboxStatus.QUEUED.value, "delivery_mode": OutboxDeliveryMode.DIGEST.value}
    ):
        grouped.setdefault((entry["user_id"], entry["brand_id"]), []).append(entry)

    for (user_id, brand_id), entries in grouped.items():
        prefs = await get_or_create_preferences(db, user_id, brand_id)

        if _local_hour(prefs, now) != prefs.get("digest_hour_local", 8):
            skipped_not_due += 1
            continue

        if not prefs.get("email_enabled", False):
            for entry in entries:
                await _suppress(db, entry, "email not opted in — in-app only")
            continue

        if not await _try_claim_daily_digest(db, user_id, now):
            continue  # another worker already sent today's digest for this user

        eligible: list[tuple[dict, dict]] = []
        for entry in entries:
            insight = await db["mi_insights"].find_one({"id": entry["insight_id"]})
            if insight is None or insight.get("status") != "active":
                await _suppress(db, entry, "insight no longer active by digest time")
                continue
            if (
                entry["topic_id"] in prefs.get("muted_topic_ids", [])
                or entry["category"] in prefs.get("muted_categories", [])
                or entry["insight_id"] in prefs.get("snoozed_insight_ids", [])
            ):
                await _suppress(db, entry, "muted or snoozed")
                continue
            eligible.append((entry, insight))

        if not eligible:
            skipped_empty += 1
            continue

        to_email = await _get_user_email(db, user_id)
        if not to_email:
            for entry, _ in eligible:
                await db["mi_outbox"].update_one(
                    {"id": entry["id"]}, {"$set": {"status": OutboxStatus.FAILED.value, "failed_reason": "no email on file"}}
                )
            continue

        subject, body = _render_email(eligible)
        try:
            ok = await email_service.send_raw_email(to_email, subject, body)
        except Exception as e:
            for entry, _ in eligible:
                await db["mi_outbox"].update_one(
                    {"id": entry["id"]}, {"$set": {"status": OutboxStatus.FAILED.value, "failed_reason": str(e)}}
                )
            continue

        for entry, _ in eligible:
            update = {"status": OutboxStatus.SENT.value if ok else OutboxStatus.FAILED.value}
            if ok:
                update["sent_at"] = now
            else:
                update["failed_reason"] = "send_raw_email returned False"
            await db["mi_outbox"].update_one({"id": entry["id"]}, {"$set": update})
            if ok:
                track_event(entry["user_id"], "alert_sent", {
                    "brand_id": entry["brand_id"], "insight_id": entry["insight_id"], "category": entry["category"], "mode": "digest",
                })
        if ok:
            sent += 1

    return {"digests_sent": sent, "skipped_empty": skipped_empty, "skipped_not_due": skipped_not_due}
