"""
/brand-intel — read a business's website and turn it into Brand Playbook suggestions.

  POST /brand-intel/website-reads              start (or rejoin) a read → the job
  GET  /brand-intel/website-reads/latest       this brand's latest read, with findings vs Playbook
  GET  /brand-intel/website-reads/{read_id}    one read: progress while running, findings when done
  POST /brand-intel/website-reads/{read_id}/apply   save the fields the owner picked

The page polls the read every 1.5–5 s (the response says how long to wait) while it runs.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.dependencies import get_active_brand_context, get_db_dependency
from app.jobs import queue

from . import service
from .safe_fetch import FetchError

router = APIRouter(prefix="/brand-intel", tags=["Brand Intelligence"])


class StartReadBody(BaseModel):
    url: str = ""
    force: bool = False   # read again even if the site was read in the last day
    source: str = "website"   # or "instagram" / "facebook": the brand's own connected account


class ApplyBody(BaseModel):
    fields: list[str] = Field(default_factory=list)
    edits: dict = Field(default_factory=dict)   # {field: value} the owner changed before applying


async def _profile(db, ctx) -> dict:
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    res = await BrandProfileService.get(ctx["user_id"], db, brand_id=ctx["brand_id"])
    return (res.get("responseData") or {}) if isinstance(res, dict) and res.get("status") else {}


async def _view(db, ctx, job: dict) -> dict:
    view = queue.public_view(job)
    view["url"] = (job.get("payload") or {}).get("url", "")
    if job["status"] in queue.ACTIVE:
        age = len(view["steps"])
        view["poll_after_ms"] = 1500 if age < 3 else 2500
        return view
    if job["status"] == queue.DONE:
        result = job.get("result") or {}
        profile = await _profile(db, ctx)
        view.update({
            "site": result.get("site"), "read_at": result.get("read_at"), "pages": result.get("pages", []),
            "findings": service.compare(result.get("fields") or {}, profile),
            "applied": job.get("applied_fields", []),
        })
    return view


@router.post("/website-reads")
async def start_website_read(body: StartReadBody, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                             ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        job = await service.start_read(db, brand_id=ctx["brand_id"], user_id=ctx["user_id"],
                                       url=body.url, force=body.force, source=body.source)
    except FetchError as e:
        raise HTTPException(status_code=429 if e.code == "rate_limited" else 400, detail=e.message)
    return await _view(db, ctx, job)


@router.get("/website-reads/latest")
async def latest_website_read(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                              ctx: dict = Depends(get_active_brand_context)) -> dict:
    job = await db[queue.COLLECTION].find_one(
        {"brand_id": ctx["brand_id"], "type": service.JOB_TYPE}, {"_id": 0}, sort=[("created_at", -1)])
    if not job:
        return {"status": "none"}
    return await _view(db, ctx, job)


@router.get("/website-reads/{read_id}")
async def get_website_read(read_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                           ctx: dict = Depends(get_active_brand_context)) -> dict:
    job = await queue.get(db, read_id, brand_id=ctx["brand_id"])
    if not job or job.get("type") != service.JOB_TYPE:
        raise HTTPException(status_code=404, detail="That website read wasn't found.")
    return await _view(db, ctx, job)


@router.post("/website-reads/{read_id}/apply")
async def apply_website_read(read_id: str, body: ApplyBody, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                             ctx: dict = Depends(get_active_brand_context)) -> dict:
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService

    job = await queue.get(db, read_id, brand_id=ctx["brand_id"])
    if not job or job.get("type") != service.JOB_TYPE:
        raise HTTPException(status_code=404, detail="That website read wasn't found.")
    if job["status"] != queue.DONE:
        raise HTTPException(status_code=409, detail="That read hasn't finished yet.")
    fields = (job.get("result") or {}).get("fields") or {}
    unknown = [k for k in body.fields if k not in fields]
    if unknown:
        raise HTTPException(status_code=400, detail="Some of those weren't found on your website.")
    profile = await _profile(db, ctx)
    update = service.build_update(fields, body.fields, profile, edits=body.edits)
    if update:
        update["website_read"] = {"url": job["payload"]["url"], "read_id": read_id,
                                  "at": (job.get("result") or {}).get("read_at")}
        saved = await BrandProfileService.save(ctx["user_id"], update, db, brand_id=ctx["brand_id"])
        if isinstance(saved, dict) and saved.get("status") is False:
            raise HTTPException(status_code=500, detail="We couldn't save that to your Playbook. Please try again.")
        await db[queue.COLLECTION].update_one(
            {"job_id": read_id}, {"$addToSet": {"applied_fields": {"$each": body.fields}}})
    return {"applied": [k for k in body.fields if k in update], "profile_updates": update}
