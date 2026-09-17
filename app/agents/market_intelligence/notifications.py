"""
Uri Market Intelligence — notification categorization and outbox queueing
(PRD §14 delivery-rules table, P0-11).

Deciding WHAT an insight is (its category) and WHETHER it's even worth
telling someone about (queueing an outbox entry) happens here, right after
an insight is composed. Deciding whether that queued entry ACTUALLY goes
out right now — preferences, quiet hours, caps, cooldown, expiry — is a
separate, later step (see notification_delivery.py) per PRD's own
"apply ... at send time" framing. Keeping these two decisions apart is what
lets a suppressed entry still show up in an audit trail instead of just
never having existed.
"""
from __future__ import annotations

import uuid
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from .models import (
    ConfidenceBand,
    EvidenceType,
    InsightVersion,
    Lifecycle,
    MIOutboxEntry,
    NotificationCategory,
    OutboxDeliveryMode,
    Topic,
)

# PRD §14: which categories get an immediate attempt vs. wait for the daily
# digest. EARLY_SIGNAL never appears here — categorize_insight() returns
# None for it, so no outbox entry (and therefore no delivery_mode lookup)
# is ever created in the first place.
_DELIVERY_MODE: dict[NotificationCategory, OutboxDeliveryMode] = {
    NotificationCategory.ACT_SOON: OutboxDeliveryMode.IMMEDIATE,
    NotificationCategory.QUALIFIED_INQUIRY: OutboxDeliveryMode.IMMEDIATE,
    NotificationCategory.MATERIAL_UPDATE: OutboxDeliveryMode.IMMEDIATE,
    NotificationCategory.PREPARE: OutboxDeliveryMode.DIGEST,
    NotificationCategory.USEFUL_PATTERN: OutboxDeliveryMode.DIGEST,
    NotificationCategory.COOLING: OutboxDeliveryMode.DIGEST,
}

_HIGH_MEDIUM = {ConfidenceBand.HIGH, ConfidenceBand.MEDIUM}


def categorize_insight(insight: InsightVersion) -> Optional[NotificationCategory]:
    """PRD §14's table, applied in the order the PRD itself gives more
    specific triggers priority over general ones: a revision bump always
    means MATERIAL_UPDATE regardless of the insight's own type/scores,
    since "changed... or meaningful evidence change" is about the fact that
    something changed, not about what the insight currently looks like.
    Returns None for early_signal — PRD: 'Watchlist only,' no delivery."""
    if insight.type == EvidenceType.REPUTATION_RISK:
        # PRD §14: "High-consequence reputation claims require human review
        # before an external alert. They remain available internally with
        # an unverified label." No automated review workflow exists in this
        # pilot, so the only safe interpretation is: never queue ANY outbox
        # entry for this type — in-app visibility (already the baseline for
        # every active insight) is all it gets until a human reviews it.
        # This intentionally overrides even a revision bump.
        return None

    if insight.revision > 1:
        return NotificationCategory.MATERIAL_UPDATE

    if insight.type == EvidenceType.PURCHASE_INQUIRY:
        return NotificationCategory.QUALIFIED_INQUIRY

    if insight.type == EvidenceType.UPCOMING_DEVELOPMENT:
        return NotificationCategory.PREPARE

    if insight.lifecycle == Lifecycle.COOLING:
        return NotificationCategory.COOLING

    if (
        insight.urgency.is_urgent
        and insight.confidence.band == ConfidenceBand.HIGH
        and insight.relevance.band == ConfidenceBand.HIGH
    ):
        return NotificationCategory.ACT_SOON

    if insight.confidence.band in _HIGH_MEDIUM and insight.relevance.band in _HIGH_MEDIUM:
        return NotificationCategory.USEFUL_PATTERN

    return None  # early_signal — insufficient evidence for a stronger conclusion


async def queue_notification(db: AsyncIOMotorDatabase, insight: InsightVersion, topic: Topic) -> Optional[MIOutboxEntry]:
    """Queues at most one outbox entry per (insight, revision, category,
    recipient) — a duplicate call with the same dedupe_key (e.g. a retried
    scan step) is a no-op, not a second entry."""
    category = categorize_insight(insight)
    if category is None:
        return None

    dedupe_key = f"{insight.brand_id}:{insight.id}:{insight.revision}:{category.value}:{topic.user_id}"

    existing = await db["mi_outbox"].find_one({"dedupe_key": dedupe_key})
    if existing is not None:
        return None

    entry = MIOutboxEntry(
        id=str(uuid.uuid4()),
        brand_id=insight.brand_id,
        user_id=topic.user_id,
        topic_id=topic.id,
        insight_id=insight.id,
        insight_revision=insight.revision,
        category=category,
        delivery_mode=_DELIVERY_MODE[category],
        dedupe_key=dedupe_key,
    )
    await db["mi_outbox"].insert_one(entry.dict())
    return entry
