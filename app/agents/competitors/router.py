"""
/competitors — the brand's competitors (PRD Workstream 2).

  GET    /competitors                      suggested, kept, discovery progress, removal reasons
  POST   /competitors/discover             start (or rejoin) a search → the job
  POST   /competitors                      add one by hand
  POST   /competitors/{id}/keep            a suggestion is a real competitor
  POST   /competitors/{id}/remove          not a competitor (with a reason Jane learns from)
  PATCH  /competitors/{id}                 direct ↔ similar
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.dependencies import get_active_brand_context, get_db_dependency
from app.jobs import queue

from . import service

router = APIRouter(prefix="/competitors", tags=["Competitors"])


class DiscoverBody(BaseModel):
    force: bool = False


class AddBody(BaseModel):
    name: str
    website: str = ""
    instagram: str = ""


class RemoveBody(BaseModel):
    reason: str = "not_a_competitor"


class KindBody(BaseModel):
    kind: str


def _not_found(e: LookupError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(e))


@router.get("")
async def list_competitors(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                           ctx: dict = Depends(get_active_brand_context)) -> dict:
    return await service.list_for(db, ctx["brand_id"])


@router.post("/discover")
async def discover(body: DiscoverBody, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                   ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        job = await service.start_discovery(db, brand_id=ctx["brand_id"], user_id=ctx["user_id"], force=body.force)
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e))
    return {**queue.public_view(job), "poll_after_ms": 2500}


@router.get("/discover/{job_id}")
async def discovery_status(job_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                           ctx: dict = Depends(get_active_brand_context)) -> dict:
    job = await queue.get(db, job_id, brand_id=ctx["brand_id"])
    if not job or job.get("type") != service.JOB_TYPE:
        raise HTTPException(status_code=404, detail="That search wasn't found.")
    return {**queue.public_view(job), "poll_after_ms": 2500}


@router.post("")
async def add_competitor(body: AddBody, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await service.add(db, brand_id=ctx["brand_id"], user_id=ctx["user_id"], name=body.name,
                                 website=body.website, instagram=body.instagram)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{competitor_id}/keep")
async def keep_competitor(competitor_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                          ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await service.keep(db, brand_id=ctx["brand_id"], user_id=ctx["user_id"], competitor_id=competitor_id)
    except LookupError as e:
        raise _not_found(e)


@router.post("/{competitor_id}/remove")
async def remove_competitor(competitor_id: str, body: RemoveBody, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                            ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await service.remove(db, brand_id=ctx["brand_id"], user_id=ctx["user_id"],
                                    competitor_id=competitor_id, reason=body.reason)
    except LookupError as e:
        raise _not_found(e)


@router.patch("/{competitor_id}")
async def update_competitor(competitor_id: str, body: KindBody, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                            ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await service.set_kind(db, brand_id=ctx["brand_id"], competitor_id=competitor_id, kind=body.kind)
    except LookupError as e:
        raise _not_found(e)
