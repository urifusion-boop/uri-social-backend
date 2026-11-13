"""
A job queue kept in Mongo, so slow work survives deploys and runs on whichever server is free.

Why not asyncio.create_task (what jane_ads/ad_jobs.py does): a task lives inside one uvicorn
process. ECS replaces processes on every deploy, so the work silently dies and the page waits
for nothing. Here a job is a document. A worker claims it with a lease and keeps the lease alive
while it works; if the worker disappears, the lease runs out and another worker picks the job up.

Contract:
  - enqueue() is idempotent on `key`: asking twice for the same thing returns the same job.
  - Claiming is one atomic find_one_and_update (served by the primary), so two workers never
    run the same job at the same time.
  - A job is retried with backoff up to `max_attempts`, then fails with a message a person can read.
  - Finished jobs expire after FINISHED_TTL via a TTL index; nothing grows without bound.
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

COLLECTION = "background_jobs"
QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
ACTIVE = (QUEUED, RUNNING)
LEASE_SECONDS = 60
FINISHED_TTL_SECONDS = 14 * 24 * 3600
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_indexes(db) -> None:
    col = db[COLLECTION]
    await col.create_index([("status", 1), ("run_at", 1)], name="claim")
    await col.create_index([("status", 1), ("lease_until", 1)], name="reclaim")
    await col.create_index([("brand_id", 1), ("type", 1), ("created_at", -1)], name="brand_type_recent")
    # Only active jobs hold their key, so the same thing can be asked for again once it finished.
    await col.create_index("active_key", unique=True, sparse=True, name="active_key_unique")
    await col.create_index("finished_at", expireAfterSeconds=FINISHED_TTL_SECONDS, name="finished_ttl")


async def enqueue(db, job_type: str, payload: dict, *, brand_id: str = "", user_id: str = "",
                  key: str = "", max_attempts: int = 3, run_at: Optional[datetime] = None) -> dict:
    """Queue a job, or return the active one already queued under `key`."""
    now = _now()
    job = {
        "job_id": f"job_{uuid.uuid4().hex}",
        "type": job_type,
        "brand_id": brand_id,
        "user_id": user_id,
        "payload": payload,
        "status": QUEUED,
        "attempts": 0,
        "max_attempts": max_attempts,
        "run_at": run_at or now,
        "progress": {"steps": [], "found": []},
        "created_at": now,
        "updated_at": now,
    }
    if key:
        job["active_key"] = f"{job_type}:{key}"
    try:
        await db[COLLECTION].insert_one(dict(job))
        return job
    except DuplicateKeyError:
        existing = await db[COLLECTION].find_one({"active_key": f"{job_type}:{key}"}, {"_id": 0})
        if existing:
            return existing
        # The active one finished between our insert and this read: queue a fresh one.
        return await enqueue(db, job_type, payload, brand_id=brand_id, user_id=user_id,
                             max_attempts=max_attempts, run_at=run_at)


async def get(db, job_id: str, brand_id: Optional[str] = None) -> Optional[dict]:
    q: dict[str, Any] = {"job_id": job_id}
    if brand_id is not None:
        q["brand_id"] = brand_id
    return await db[COLLECTION].find_one(q, {"_id": 0})


async def claim(db, job_types: list[str]) -> Optional[dict]:
    """Take the next job that is due, or one whose worker stopped holding its lease."""
    now = _now()
    job = await db[COLLECTION].find_one_and_update(
        {"type": {"$in": job_types},
         "$or": [{"status": QUEUED, "run_at": {"$lte": now}},
                 {"status": RUNNING, "lease_until": {"$lt": now}}]},
        {"$set": {"status": RUNNING, "worker_id": WORKER_ID, "started_at": now, "updated_at": now,
                  "lease_until": now + timedelta(seconds=LEASE_SECONDS)},
         "$inc": {"attempts": 1}},
        sort=[("run_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if job:
        job.pop("_id", None)
    return job


async def heartbeat(db, job_id: str) -> None:
    now = _now()
    await db[COLLECTION].update_one(
        {"job_id": job_id, "worker_id": WORKER_ID, "status": RUNNING},
        {"$set": {"lease_until": now + timedelta(seconds=LEASE_SECONDS), "updated_at": now}},
    )


async def progress(db, job_id: str, *, step: Optional[dict] = None, found: Optional[list[str]] = None) -> None:
    """Record what the job is doing now (a step) and what it has found so far, for the page to show.
    Doubles as a heartbeat."""
    now = _now()
    update: dict[str, Any] = {"$set": {"lease_until": now + timedelta(seconds=LEASE_SECONDS), "updated_at": now}}
    if step is not None:
        update["$set"][f"progress.step_index.{step['id']}"] = step
    if found:
        update["$push"] = {"progress.found": {"$each": found[:20], "$slice": -40}}
    await db[COLLECTION].update_one({"job_id": job_id, "worker_id": WORKER_ID}, update)


async def complete(db, job_id: str, result: dict) -> None:
    now = _now()
    await db[COLLECTION].update_one(
        {"job_id": job_id, "worker_id": WORKER_ID},
        {"$set": {"status": DONE, "result": result, "finished_at": now, "updated_at": now},
         "$unset": {"active_key": "", "lease_until": ""}},
    )


async def fail(db, job: dict, message: str, *, code: str = "failed", retry: bool = True) -> None:
    """Retry later if attempts remain and the failure is worth retrying, else fail for good."""
    now = _now()
    if retry and job.get("attempts", 1) < job.get("max_attempts", 1):
        backoff = min(300, 10 * (2 ** (job.get("attempts", 1) - 1)))
        await db[COLLECTION].update_one(
            {"job_id": job["job_id"], "worker_id": WORKER_ID},
            {"$set": {"status": QUEUED, "run_at": now + timedelta(seconds=backoff), "updated_at": now,
                      "last_error": message},
             "$unset": {"lease_until": "", "worker_id": ""}},
        )
        return
    await db[COLLECTION].update_one(
        {"job_id": job["job_id"]},
        {"$set": {"status": FAILED, "error": {"code": code, "message": message},
                  "finished_at": now, "updated_at": now},
         "$unset": {"active_key": "", "lease_until": ""}},
    )


def public_view(job: dict) -> dict:
    """What a page may see of a job: never the payload or internal bookkeeping."""
    steps = sorted((job.get("progress") or {}).get("step_index", {}).values(), key=lambda s: s.get("order", 0))
    view = {
        "job_id": job["job_id"],
        "status": job["status"],
        "steps": steps,
        "found": (job.get("progress") or {}).get("found", []),
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
    }
    if job["status"] == FAILED:
        view["error"] = job.get("error") or {"code": "failed", "message": "That didn't finish. Please try again."}
    return view
