"""
How the background job queue is doing, per job type, and an alert when it isn't.

stats(): in the last `hours`, per job type — how many are waiting, running, done and failed,
the failure rate, how long finished jobs took (median and slowest), and how long the oldest
waiting job has waited. check(): raises a platform alert (dashboard + admin email, with its own
cooldown) when a type's failure rate is high or jobs are waiting too long.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from . import queue

FAILURE_RATE_ALERT = 0.30
MIN_FINISHED_FOR_RATE = 5
WAITING_ALERT_MINUTES = 10


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def stats(db, hours: int = 24, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    docs = await db[queue.COLLECTION].find(
        {"$or": [{"status": {"$in": list(queue.ACTIVE)}}, {"finished_at": {"$gte": since}}]},
        {"_id": 0, "type": 1, "status": 1, "created_at": 1, "started_at": 1, "finished_at": 1, "attempts": 1},
    ).to_list(length=50_000)
    by: dict[str, dict] = {}
    for d in docs:
        t = by.setdefault(d["type"], {"type": d["type"], "queued": 0, "running": 0, "done": 0, "failed": 0,
                                       "retries": 0, "_durations": [], "oldest_waiting_minutes": 0.0})
        t[d["status"]] = t.get(d["status"], 0) + 1
        t["retries"] += max(0, int(d.get("attempts") or 0) - 1)
        if d["status"] == queue.QUEUED and d.get("created_at"):
            waited = (now - _aware(d["created_at"])).total_seconds() / 60
            t["oldest_waiting_minutes"] = round(max(t["oldest_waiting_minutes"], waited), 1)
        if d.get("finished_at") and d.get("started_at"):
            t["_durations"].append((_aware(d["finished_at"]) - _aware(d["started_at"])).total_seconds())
    out = []
    for t in by.values():
        durs = sorted(t.pop("_durations"))
        finished = t["done"] + t["failed"]
        t["failure_rate"] = round(t["failed"] / finished, 3) if finished else 0.0
        t["median_seconds"] = round(durs[len(durs) // 2], 1) if durs else None
        t["slowest_seconds"] = round(durs[-1], 1) if durs else None
        out.append(t)
    out.sort(key=lambda t: t["type"])
    return {"since": since.isoformat(), "hours": hours, "types": out}


def problems(report: dict) -> list[str]:
    found = []
    for t in report["types"]:
        finished = t["done"] + t["failed"]
        if finished >= MIN_FINISHED_FOR_RATE and t["failure_rate"] >= FAILURE_RATE_ALERT:
            found.append(f"{t['type']}: {t['failed']} of {finished} failed in the last hour "
                         f"({round(t['failure_rate'] * 100)}%).")
        if t["oldest_waiting_minutes"] >= WAITING_ALERT_MINUTES:
            found.append(f"{t['type']}: a job has waited {t['oldest_waiting_minutes']:.0f} minutes to start "
                         f"({t['queued']} waiting) — workers may be down or overloaded.")
    return found


async def check(db, now: Optional[datetime] = None) -> list[str]:
    report = await stats(db, hours=1, now=now)
    found = problems(report)
    if found:
        from app.services import platform_alerts
        await platform_alerts.raise_alert(db, kind="background_jobs_unhealthy",
                                          title="Background jobs need attention", detail=" ".join(found))
    return found
