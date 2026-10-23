"""Onebox billing endpoints: plans, usage, subscribe, and the admin's configuration."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.auth_bearer import JWTBearer
from app.dependencies import get_active_brand_context, get_db_dependency

from . import onebox

router = APIRouter(prefix="/inbox/onebox", tags=["Unified Inbox — Onebox billing"])


def _ws(ctx: dict) -> str:
    return ctx.get("brand_id") or ""


async def _can_manage(db, ctx: dict) -> bool:
    """Buying a plan is an admin's decision: a solo workspace's owner, or an agency admin."""
    if not ctx.get("agency_id"):
        return True
    from app.services.AgencyService import AgencyService
    return await AgencyService.is_agency_admin(ctx.get("user_id", ""), ctx["agency_id"], db)


def _is_platform_admin(token: dict) -> bool:
    from app.agents.jane_ads.router import _is_ads_admin
    return _is_ads_admin(token)


@router.get("/usage")
async def get_usage(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                    ctx: dict = Depends(get_active_brand_context)) -> dict:
    return {"usage": await onebox.usage(db, _ws(ctx)), "plans": await onebox.list_plans(db),
            "can_manage": await _can_manage(db, ctx)}


@router.post("/subscribe")
async def subscribe(plan_id: str = Body(..., embed=True),
                    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                    ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Start a Squad checkout for a plan. Paying again renews; paying early queues the
    next period after the current one."""
    if not await _can_manage(db, ctx):
        raise HTTPException(status_code=403, detail="Only an admin can change the inbox plan.")
    plan = next((p for p in await onebox.list_plans(db) if p["plan_id"] == plan_id), None)
    if not plan:
        raise HTTPException(status_code=404, detail="That plan isn't available.")
    user = await db["users"].find_one({"userId": ctx.get("user_id", "")}) or {}
    from app.services.PaymentService import payment_service
    try:
        res = await payment_service.initialize_onebox_payment(
            ctx.get("user_id", ""), user.get("email", ""), _ws(ctx), plan)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"payment_url": res.payment_url, "transaction_ref": res.transaction_ref,
            "amount": res.amount, "public_key": res.public_key, "email": res.email}


@router.get("/admin/config")
async def get_admin_config(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                           token: dict = Depends(JWTBearer())) -> dict:
    if not _is_platform_admin(token):
        raise HTTPException(status_code=403, detail="Not authorized.")
    return {"config": await onebox.get_config(db), "plans": await onebox.list_plans(db, include_inactive=True)}


@router.put("/admin/config")
async def put_admin_config(payload: dict = Body(...), db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                           token: dict = Depends(JWTBearer())) -> dict:
    """Message cap, inactivity window, warning level, trial size, token limits."""
    if not _is_platform_admin(token):
        raise HTTPException(status_code=403, detail="Not authorized.")
    try:
        return {"config": await onebox.save_config(db, payload)}
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Every setting must be a whole number.")


@router.put("/admin/plans")
async def put_admin_plan(payload: dict = Body(...), db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         token: dict = Depends(JWTBearer())) -> dict:
    """Create or change a plan — price, allowance, active — with no deploy."""
    if not _is_platform_admin(token):
        raise HTTPException(status_code=403, detail="Not authorized.")
    try:
        return {"plan": await onebox.save_plan(db, payload)}
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e) or "Invalid plan.")


@router.get("/admin/costs")
async def get_admin_costs(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                          token: dict = Depends(JWTBearer())) -> dict:
    """AI spend by workspace, separate from what customers pay: where the money goes."""
    if not _is_platform_admin(token):
        raise HTTPException(status_code=403, detail="Not authorized.")
    rows = await db[onebox.AI_COSTS].find({}).to_list(20000)
    by_ws: dict[str, dict] = {}
    for r in rows:
        w = by_ws.setdefault(r["workspace_id"], {"workspace_id": r["workspace_id"], "conversations": set(),
                                                 "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0})
        w["conversations"].add(r["conversation_id"])
        w["input_tokens"] += r.get("input_tokens", 0)
        w["output_tokens"] += r.get("output_tokens", 0)
        w["estimated_cost_usd"] = round(w["estimated_cost_usd"] + r.get("estimated_cost_usd", 0), 6)
    out = sorted(({**w, "conversations": len(w["conversations"])} for w in by_ws.values()),
                 key=lambda w: -w["estimated_cost_usd"])
    return {"workspaces": out[:100], "total_estimated_cost_usd": round(sum(w["estimated_cost_usd"] for w in out), 4)}
