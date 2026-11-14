"""
GET  /features                 which new capabilities this user's brand sees
GET  /api/admin/features           every switch with its rollout (admins)
PUT  /api/admin/features/{flag}    change a switch's rollout (admins)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.auth_bearer import JWTBearer
from app.dependencies import get_active_brand_context, get_db_dependency

from . import flags

router = APIRouter(tags=["Features"])


async def _is_staff(jwt_payload: dict, db) -> bool:
    from app.routers.admin_router import _is_admin_email
    email = ((jwt_payload or {}).get("claims") or {}).get("email") or ""
    return bool(email) and await _is_admin_email(email, db)


@router.get("/features")
async def my_features(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                      ctx: dict = Depends(get_active_brand_context),
                      jwt_payload: dict = Depends(JWTBearer())) -> dict:
    return {"flags": await flags.enabled_for(db, ctx.get("brand_id") or "", await _is_staff(jwt_payload, db))}


class RolloutBody(BaseModel):
    percent: Optional[int] = None
    staff: Optional[bool] = None


@router.get("/api/admin/features")
async def list_rollouts(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                        jwt_payload: dict = Depends(JWTBearer())) -> dict:
    if not await _is_staff(jwt_payload, db):
        raise HTTPException(status_code=403, detail="Admins only.")
    docs = await flags._settings(db)
    return {"features": [flags.setting(docs, f) for f in flags.FLAGS]}


@router.put("/api/admin/features/{flag}")
async def set_rollout(flag: str, body: RolloutBody, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                      jwt_payload: dict = Depends(JWTBearer())) -> dict:
    if not await _is_staff(jwt_payload, db):
        raise HTTPException(status_code=403, detail="Admins only.")
    try:
        email = ((jwt_payload or {}).get("claims") or {}).get("email") or ""
        return await flags.update(db, flag, percent=body.percent, staff=body.staff, by=email)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such feature.")
