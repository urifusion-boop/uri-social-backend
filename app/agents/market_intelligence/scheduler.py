"""
Uri Market Intelligence — scheduled collection (PRD §18).

Pilot default: eligible active topics with keep_updating=True are checked
hourly (PRD §18's proposed cadence) via the shared notification_scheduler's
single APScheduler instance and its per-minute-bucketed claim mechanism —
see notification_scheduler.py's _try_claim_job_run docstring for why that's
necessary (the container runs 4 uvicorn workers, each starting its own
scheduler; without the claim, this would fire 4x every hour).

Known simplification, stated plainly rather than left silent: PRD §18 asks
for a tracked "last committed coverage boundary" per topic so each run only
retrieves a bounded overlap window forward from where the last one left off.
This pilot does not track that boundary — every scheduled run re-requests
the topic's full requested_days window from "now," exactly like a manual
scan. That is safe (not a duplication bug) because execute_scan's existing
source_id dedup already discards anything already stored for the topic; it
is simply not bandwidth-optimal against a real metered provider. Tightening
this to true incremental collection is a follow-up once a real (non-mock,
non-zero-cost) adapter makes the inefficiency actually matter.

Similarly, PRD §9's "updates the existing insight" (rather than a fresh,
unrelated one each run) depends on clusters/insights persisting identity
across scans, which this pilot's clustering does not yet do (each scan's
clusters are built fresh from that scan's own new evidence). A recurring
scheduled topic will therefore currently produce a new InsightVersion per
run rather than revising one — a known, separate gap, not something this
scheduler papers over.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorDatabase

from .models import ScanStatus, Topic
from .scan_runner import create_scan_run, execute_scan

ACTIVE_SCAN_STATUSES = {ScanStatus.QUEUED.value, ScanStatus.COLLECTING.value, ScanStatus.ANALYSING.value}
DEFAULT_REFRESH_CADENCE_HOURS = 1


async def _due_for_refresh(db: AsyncIOMotorDatabase, topic_id: str, cadence_hours: int) -> bool:
    """A topic is due if it has no prior scan, or its most recent scan
    started more than `cadence_hours` ago and isn't currently in flight —
    never double-schedule a topic that's still collecting/analysing."""
    last_scan = await db["mi_scans"].find_one({"topic_id": topic_id}, sort=[("started_at", -1)])
    if last_scan is None:
        return True
    if last_scan.get("status") in ACTIVE_SCAN_STATUSES:
        return False
    started_at = last_scan.get("started_at")
    if started_at is None:
        return True
    return datetime.utcnow() - started_at >= timedelta(hours=cadence_hours)


async def run_scheduled_market_intelligence_scans(db: AsyncIOMotorDatabase) -> dict:
    """Called hourly by the shared scheduler. Returns a small summary dict
    for the caller's own log line, matching every other scheduled job in
    this codebase (see notification_scheduler.py's _job_* functions)."""
    cursor = db["mi_topics"].find({"active": True, "keep_updating": True})
    topics = [Topic(**{k: v for k, v in doc.items() if k != "_id"}) async for doc in cursor]

    started = 0
    skipped = 0
    for topic in topics:
        cadence_hours = min(
            (s.refresh_cadence_hours for s in topic.sources), default=DEFAULT_REFRESH_CADENCE_HOURS
        )
        if not await _due_for_refresh(db, topic.id, cadence_hours):
            skipped += 1
            continue

        run = await create_scan_run(topic, db)
        if run.status == ScanStatus.BUDGET_LIMITED:
            skipped += 1
            continue

        await execute_scan(topic, run.id, db)
        started += 1

    return {"topics_checked": len(topics), "scans_started": started, "skipped": skipped}
