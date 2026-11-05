"""
Making an ad as a background job the page checks on.

An ad can take over 100 seconds when a format's checks need retries, and the load
balancer cuts any request silent for 120, losing the ad. So the request only starts the
work, and the result waits in Mongo for whichever worker the page's next check reaches.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

from fastapi import HTTPException

JOBS = "jane_ads_ad_jobs"
# A job whose worker was replaced by a deploy never finishes; the page stops waiting here.
STALE_AFTER_SECONDS = 300


async def start(db, brand_id: str, work: Callable[[], Awaitable[dict]]) -> str:
    job_id = f"adjob_{uuid.uuid4().hex[:16]}"
    await db[JOBS].insert_one({"job_id": job_id, "brand_id": brand_id, "status": "running",
                               "created_at": datetime.now(timezone.utc)})

    async def run() -> None:
        update: dict
        try:
            update = {"status": "done", "result": await work()}
        except HTTPException as e:
            update = {"status": "failed", "status_code": e.status_code, "detail": str(e.detail)}
        except Exception as e:
            print(f"[ad job] {job_id} failed: {e}", flush=True)
            update = {"status": "failed", "status_code": 500, "detail": "We couldn't make that ad. Please try again."}
        await db[JOBS].update_one({"job_id": job_id}, {"$set": update})

    asyncio.create_task(run())
    return job_id


async def read(db, brand_id: str, job_id: str) -> dict:
    job = await db[JOBS].find_one({"job_id": job_id, "brand_id": brand_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="That job wasn't found.")
    if job["status"] == "running":
        created = job["created_at"]
        age = datetime.now(timezone.utc) - created.replace(tzinfo=created.tzinfo or timezone.utc)
        if age.total_seconds() > STALE_AFTER_SECONDS:
            return {"status": "failed", "detail": "That took too long. Please try again."}
        return {"status": "running"}
    return {k: job.get(k) for k in ("status", "result", "detail", "status_code") if k in job}
