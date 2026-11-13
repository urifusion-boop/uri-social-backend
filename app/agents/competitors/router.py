"""
/competitors — the brand's competitors (PRD Workstream 2).

  GET    /competitors                      suggested, kept, discovery progress, removal reasons
  POST   /competitors/discover             start (or rejoin) a search → the job
  POST   /competitors                      add one by hand
  POST   /competitors/{id}/keep            a suggestion is a real competitor
  POST   /competitors/{id}/remove          not a competitor (with a reason Jane learns from)
  PATCH  /competitors/{id}                 direct ↔ similar

  GET    /competitors/ads                  saved competitor ads (filter, paged)
  POST   /competitors/ads                  save one (Jane reads it in the background)
  DELETE /competitors/ads/{id}
  POST   /competitors/ads/{id}/recreate    make this business's own version → the request
  GET    /competitors/recreations/{id}     its versions, as they arrive
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.dependencies import get_active_brand_context, get_db_dependency
from app.jobs import queue

from . import ads, service

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


class SaveAdBody(BaseModel):
    image_url: str
    competitor_id: str = ""
    platform: str = ""
    link: str = ""
    note: str = ""


class RecreateBody(BaseModel):
    product: str = ""
    count: int = 3


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


@router.get("/ads")
async def list_competitor_ads(competitor_id: str = "", angle: str = "", platform: str = "",
                              before: Optional[datetime] = None, limit: int = 24,
                              db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                              ctx: dict = Depends(get_active_brand_context)) -> dict:
    return await ads.list_ads(db, ctx["brand_id"], competitor_id=competitor_id, angle=angle, platform=platform,
                              before=before, limit=limit)


@router.post("/ads")
async def save_competitor_ad(body: SaveAdBody, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                             ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await ads.save_ad(db, brand_id=ctx["brand_id"], user_id=ctx["user_id"], image_url=body.image_url,
                                 competitor_id=body.competitor_id, platform=body.platform, link=body.link,
                                 note=body.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/ads/{ad_id}")
async def delete_competitor_ad(ad_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                               ctx: dict = Depends(get_active_brand_context)) -> dict:
    if not await ads.delete_ad(db, ctx["brand_id"], ad_id):
        raise HTTPException(status_code=404, detail="That ad wasn't found.")
    return {"deleted": True}


@router.post("/ads/{ad_id}/recreate")
async def recreate_ad(ad_id: str, body: RecreateBody, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                      ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await ads.start_recreate(db, brand_id=ctx["brand_id"], user_id=ctx["user_id"], ad_id=ad_id,
                                        product=body.product, count=body.count)
    except LookupError as e:
        raise _not_found(e)
    except PermissionError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/recreations/{recreation_id}")
async def get_recreation(recreation_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         ctx: dict = Depends(get_active_brand_context)) -> dict:
    rec = await ads.get_recreation(db, ctx["brand_id"], recreation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="That request wasn't found.")
    return {**rec, "poll_after_ms": 3000}


class UseBody(BaseModel):
    index: int


class EditVersionBody(BaseModel):
    headline: str
    primary_text: str


@router.patch("/recreations/{recreation_id}/versions/{index}")
async def edit_version(recreation_id: str, index: int, body: EditVersionBody,
                       db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                       ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await ads.edit_version(db, brand_id=ctx["brand_id"], recreation_id=recreation_id, index=index,
                                      headline=body.headline, primary_text=body.primary_text)
    except LookupError as e:
        raise _not_found(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/recreations/{recreation_id}/versions/{index}")
async def remove_version(recreation_id: str, index: int, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await ads.remove_version(db, brand_id=ctx["brand_id"], recreation_id=recreation_id, index=index)
    except LookupError as e:
        raise _not_found(e)


@router.post("/recreations/{recreation_id}/versions/{index}/redo")
async def redo_version(recreation_id: str, index: int, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                       ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await ads.redo_version(db, brand_id=ctx["brand_id"], user_id=ctx["user_id"],
                                      recreation_id=recreation_id, index=index)
    except LookupError as e:
        raise _not_found(e)
    except PermissionError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/recreations/{recreation_id}/use")
async def use_recreation(recreation_id: str, body: UseBody, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await ads.use_version(db, brand_id=ctx["brand_id"], user_id=ctx["user_id"],
                                     recreation_id=recreation_id, index=body.index)
    except LookupError as e:
        raise _not_found(e)


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
