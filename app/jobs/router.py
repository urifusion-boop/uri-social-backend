"""GET /api/admin/jobs/stats — how the background job queue is doing, per job type (admins)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_db_dependency
from app.routers.admin_router import verify_admin

from . import monitor

router = APIRouter(tags=["Admin"])


@router.get("/api/admin/jobs/stats")
async def job_stats(hours: int = 24, db=Depends(get_db_dependency), _admin: dict = Depends(verify_admin)) -> dict:
    report = await monitor.stats(db, hours=max(1, min(168, hours)))
    report["problems"] = monitor.problems(await monitor.stats(db, hours=1))
    return report


@router.get("/api/admin/jobs/failures")
async def job_failures(limit: int = 20, db=Depends(get_db_dependency), _admin: dict = Depends(verify_admin)) -> dict:
    """The latest crashed attempts and what went wrong, so a failure can be traced without server logs."""
    from . import queue
    return {"failures": await queue.recent_failures(db, max(1, min(100, limit)))}
