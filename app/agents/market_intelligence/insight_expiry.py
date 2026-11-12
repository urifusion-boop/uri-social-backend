"""
Uri Market Intelligence — purchase-inquiry expiry/re-scoring.

Confirmed live: 48 active insights, all type purchase_inquiry, all flagged
is_urgent=True, all carrying the exact pre-fix reason string "within 48h
freshness window." Two compounding gaps, not one:

1. urgency_for_inquiry/is_inquiry_eligible were conflated (see
   classification/scoring.py's docstrings) — fixed there, but that fix
   only applies going forward, to insights composed AFTER the fix shipped.
   An inquiry-type InsightVersion is written once, at compose time, and
   never revised afterward (unlike a cluster-backed insight, which
   _apply_revision can update when new evidence merges in) — there was no
   mechanism that ever touched an existing inquiry insight's stored
   urgency again.
2. Nothing ever expired an inquiry past its 48h action window either. A
   purchase inquiry is, by definition (PRD §12), only actionable while
   fresh — once the window closes, continuing to show it as "active" is
   as misleading as the urgency flag was.

This module is the fix for both: a scheduled pass that re-scores every
still-fresh active inquiry's urgency against current wall-clock time (so
the badge stays correct as inquiries age, not just at compose time), and
retracts the ones that have aged out of the 48h window entirely.
"""
from __future__ import annotations

from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from .classification.scoring import is_inquiry_eligible, urgency_for_inquiry
from .models import ActionReadiness, Evidence, EvidenceType, InsightStatus, UrgencyAssessment


async def expire_stale_inquiries(db: AsyncIOMotorDatabase, now: datetime = None) -> dict:
    """Re-scores every active purchase_inquiry insight's urgency against
    `now`, and marks EXPIRED any whose source evidence has aged past
    INQUIRY_FRESHNESS_HOURS (is_inquiry_eligible returns False). Returns a
    summary dict matching every other scheduled job in this codebase (see
    notification_scheduler.py's _job_* functions)."""
    now = now or datetime.utcnow()
    rescored = 0
    expired = 0
    unchanged = 0

    cursor = db["mi_insights"].find({"status": InsightStatus.ACTIVE.value, "type": EvidenceType.PURCHASE_INQUIRY.value})
    async for doc in cursor:
        evidence_ids = doc.get("evidence_ids") or []
        if not evidence_ids:
            continue
        evidence_doc = await db["mi_evidence"].find_one({"id": evidence_ids[0]})
        if evidence_doc is None:
            continue
        evidence = Evidence(**{k: v for k, v in evidence_doc.items() if k != "_id"})

        eligible, _ = is_inquiry_eligible(evidence, now)
        if not eligible:
            await db["mi_insights"].update_one(
                {"id": doc["id"]}, {"$set": {"status": InsightStatus.EXPIRED.value, "last_updated": now}}
            )
            expired += 1
            continue

        new_urgency = urgency_for_inquiry(evidence, now)
        relevance_is_low = (doc.get("relevance") or {}).get("band") == "low"
        # Same gate scan_runner.py's compose path applies at creation time
        # (_urgency_gated_by_relevance) — a stale re-score must not
        # resurrect "Urgent" on a match the insight's own stored relevance
        # already rates "low," for the same reason it was never meant to
        # say that in the first place.
        if new_urgency.is_urgent and relevance_is_low:
            new_urgency = UrgencyAssessment(
                is_urgent=False, deadline=new_urgency.deadline,
                reason=f"relevance too low to flag as urgent ({new_urgency.reason})",
            )

        # Confirmed live: the first pass of this job only ever touched
        # urgency, so a low-relevance insight kept showing "Ready to act"
        # (the badge driven by action_readiness, a SEPARATE stored field)
        # even after its "Urgent" tag was correctly cleared. Same gate
        # _action_readiness_from applies at compose time.
        new_readiness = ActionReadiness.CHECK_SUITABILITY.value if relevance_is_low else doc.get("action_readiness")

        updates = {}
        if new_urgency.is_urgent != doc.get("urgency", {}).get("is_urgent") or new_urgency.reason != doc.get("urgency", {}).get("reason"):
            updates["urgency"] = new_urgency.dict()
        if new_readiness != doc.get("action_readiness"):
            updates["action_readiness"] = new_readiness

        if not updates:
            unchanged += 1
            continue
        updates["last_updated"] = now
        await db["mi_insights"].update_one({"id": doc["id"]}, {"$set": updates})
        rescored += 1

    return {"rescored": rescored, "expired": expired, "unchanged": unchanged}
