"""
Runs queued jobs inside every uvicorn process, a few at a time.

Each process polls for due jobs (backing off while the queue is empty), runs at most
CONCURRENCY at once, and keeps each job's lease alive while it works. More servers simply
mean more workers; nothing has to be configured per server.

A handler is `async def handler(db, job, ctx) -> dict`. It reports progress through `ctx` and
raises JobError for a failure it can explain to a person (retry=False when trying again won't
help, e.g. a website that refuses to be read).
"""
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from . import queue

CONCURRENCY = int(os.getenv("JOB_WORKER_CONCURRENCY", "3"))
IDLE_MIN, IDLE_MAX = 1.0, 5.0

Handler = Callable[..., Awaitable[dict]]
_handlers: dict[str, Handler] = {}
_task: Optional[asyncio.Task] = None
_stopping = False


class JobError(Exception):
    def __init__(self, message: str, *, code: str = "failed", retry: bool = False):
        super().__init__(message)
        self.message, self.code, self.retry = message, code, retry


def register(job_type: str, handler: Handler) -> None:
    _handlers[job_type] = handler


@dataclass
class JobContext:
    db: object
    job_id: str

    def __post_init__(self) -> None:
        self._orders: dict[str, int] = {}

    async def step(self, step_id: str, label: str, status: str = "running", detail: str = "") -> None:
        """One line of the progress list the page shows ("Reading your Services page"). The same
        step_id updates its line in place; a new one is added below the others."""
        order = self._orders.setdefault(step_id, len(self._orders) + 1)
        await queue.progress(self.db, self.job_id, step={
            "id": step_id, "label": label, "status": status, "detail": detail, "order": order})

    async def found(self, items: list[str]) -> None:
        """Things worth showing while the job is still running ("Airport runs")."""
        if items:
            await queue.progress(self.db, self.job_id, found=[str(i)[:80] for i in items])

    async def heartbeat(self) -> None:
        await queue.heartbeat(self.db, self.job_id)


async def run_one(db, job: dict) -> None:
    handler = _handlers.get(job["type"])
    if handler is None:
        await queue.fail(db, job, "This kind of job isn't supported any more.", retry=False)
        return
    if job.get("attempts", 1) > job.get("max_attempts", 1):
        await queue.fail(db, job, "This took too many tries. Please start it again.", retry=False)
        return
    ctx = JobContext(db=db, job_id=job["job_id"])
    beat = asyncio.create_task(_keep_alive(db, job["job_id"]))
    try:
        result = await handler(db, job, ctx)
        await queue.complete(db, job["job_id"], result or {})
    except JobError as e:
        await queue.fail(db, job, e.message, code=e.code, retry=e.retry)
    except Exception as e:  # unexpected: retry, and say something useful if it keeps failing
        print(f"[jobs] {job['type']} {job['job_id']} attempt {job.get('attempts')} crashed: {e!r}", flush=True)
        await queue.fail(db, job, "Something went wrong on our side. We'll try again shortly.")
    finally:
        beat.cancel()


async def _keep_alive(db, job_id: str) -> None:
    while True:
        await asyncio.sleep(queue.LEASE_SECONDS / 3)
        try:
            await queue.heartbeat(db, job_id)
        except Exception as e:
            print(f"[jobs] heartbeat failed for {job_id}: {e!r}", flush=True)


async def _loop(get_db: Callable[[], object]) -> None:
    running: set[asyncio.Task] = set()
    idle = IDLE_MIN
    while not _stopping:
        try:
            if len(running) >= CONCURRENCY or not _handlers:
                await asyncio.sleep(0.5)
                continue
            db = get_db()
            job = await queue.claim(db, list(_handlers))
            if job is None:
                await asyncio.sleep(idle + random.random() * 0.5)
                idle = min(IDLE_MAX, idle * 1.5)
                continue
            idle = IDLE_MIN
            t = asyncio.create_task(run_one(db, job))
            running.add(t)
            t.add_done_callback(running.discard)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[jobs] worker loop error: {e!r}", flush=True)
            await asyncio.sleep(IDLE_MAX)


def start(get_db: Callable[[], object]) -> None:
    """Start this process's worker. Safe to call more than once."""
    global _task, _stopping
    if os.getenv("JOB_WORKERS_ENABLED", "true").lower() == "false":
        print("[jobs] workers disabled by JOB_WORKERS_ENABLED=false", flush=True)
        return
    if _task is not None and not _task.done():
        return
    _stopping = False
    _task = asyncio.ensure_future(_loop(get_db))


def stop() -> None:
    """Stop claiming new jobs. Running ones finish or, if the process dies, their lease runs out
    and another server takes them over."""
    global _stopping
    _stopping = True
