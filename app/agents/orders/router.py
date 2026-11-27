"""Orders API. Business-scoped like the catalog; totals and prices are always computed server-side."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.agents.catalog.models import iso
from app.agents.catalog.router import Ctx, _ctx, _manager
from app.dependencies import get_db_dependency

from . import service
from .indexes import ensure_indexes
from .models import OrderError

router = APIRouter(prefix="/orders", tags=["Orders"])

_INTERNAL = {"idem_scope", "number_scope", "idempotency_key"}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {("id" if k == "_id" else k): _clean(v) for k, v in value.items() if k not in _INTERNAL}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return iso(value) if hasattr(value, "isoformat") else value


def _fail(e: OrderError):
    raise HTTPException(status_code=e.status, detail=e.detail)


def _public(order: dict) -> dict:
    keep = ("order_number", "status", "payment_status", "fulfilment_status", "currency", "subtotal", "discount_total",
            "fees_total", "tax_total", "total", "paid_amount", "due_amount", "created_at", "confirmed_at", "payment_due_at")
    lines = [{"name": x["snapshot"]["name"], "variant": x["snapshot"].get("variant", ""), "quantity": x["quantity"],
              "unit_amount": x["unit_amount"], "line_total": x["line_total"], "fulfilment_status": x["fulfilment_status"]}
             for x in order["lines"]]
    return _clean({**{k: order.get(k) for k in keep}, "lines": lines, "customer_name": order["customer"].get("name", "")})


async def _db_ready(db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> AsyncIOMotorDatabase:
    await ensure_indexes(db)
    return db


@router.get("/public/{token}")
async def public_order(token: str, db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    try:
        return _public(await service.get_by_token(db, token))
    except OrderError as e:
        _fail(e)


@router.post("", status_code=201)
async def create_order(body: dict = Body(...), idempotency_key: str = Header(default="", alias="Idempotency-Key"),
                       ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    try:
        return _clean(await service.create_order(db, ctx["business_id"], body, ctx["user_id"], idempotency_key))
    except OrderError as e:
        _fail(e)


@router.get("")
async def list_orders(q: str = "", status: str = "", payment_status: str = "", fulfilment_status: str = "", source: str = "",
                      assignee: str = "", limit: int = Query(50, le=200), skip: int = 0, ctx: Ctx = Depends(_ctx),
                      db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    await service.expire_due(db, ctx["business_id"])
    out = await service.list_orders(db, ctx["business_id"], q, status, payment_status, fulfilment_status, source, assignee,
                                    limit=limit, skip=skip)
    return {"orders": [_clean({k: v for k, v in o.items() if k != "lines"} | {"line_count": len(o["lines"])}) for o in out["orders"]],
            "total": out["total"]}


@router.get("/export")
async def export_orders(ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(_db_ready)) -> Response:
    _manager(ctx)
    rows = (await service.list_orders(db, ctx["business_id"], limit=200))["orders"]
    await service._log(db, {"_id": "export", "business_id": ctx["business_id"]}, "exported", ctx["user_id"], {"rows": len(rows)})
    return Response(content=service.export_csv(rows), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=orders.csv"})


@router.get("/{order_id}")
async def get_order(order_id: str, ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    try:
        order = _clean(await service.get_order(db, ctx["business_id"], order_id))
    except OrderError as e:
        _fail(e)
    payments = await db["order_payments"].find({"order_id": order_id}).sort("created_at", 1).to_list(100)
    return {**order, "payments": _clean(payments)}


@router.patch("/{order_id}")
async def update_order(order_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                       db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    try:
        if "assignee" in body:
            _manager(ctx)
            return _clean(await service.assign(db, ctx["business_id"], order_id, str(body["assignee"] or ""), ctx["user_id"]))
        return _clean(await service.update_draft(db, ctx["business_id"], order_id, body, ctx["user_id"]))
    except OrderError as e:
        _fail(e)


@router.post("/{order_id}/quote")
async def quote(order_id: str, body: dict = Body(default={}), ctx: Ctx = Depends(_ctx),
                db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    try:
        if body.get("line_id"):
            _manager(ctx)
            await service.set_quote_price(db, ctx["business_id"], order_id, str(body["line_id"]), body.get("unit_price"), ctx["user_id"])
        return _clean(await service.send_quote(db, ctx["business_id"], order_id, ctx["user_id"]))
    except OrderError as e:
        _fail(e)


@router.post("/{order_id}/confirm")
async def confirm_order(order_id: str, body: dict = Body(default={}), ctx: Ctx = Depends(_ctx),
                        db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    try:
        return _clean(await service.confirm(db, ctx["business_id"], order_id, body.get("customer_accepted"), ctx["user_id"]))
    except OrderError as e:
        _fail(e)


@router.post("/{order_id}/payment-requests", status_code=201)
async def payment_request(order_id: str, body: dict = Body(default={}), ctx: Ctx = Depends(_ctx),
                          db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    try:
        return _clean(await service.request_payment(db, ctx["business_id"], order_id, body.get("amount"), ctx["user_id"]))
    except OrderError as e:
        _fail(e)


@router.post("/{order_id}/payments")
async def record_offline_payment(order_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                                 db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    """Cash or transfer the business saw itself. Labelled merchant-recorded, never provider-verified."""
    _manager(ctx)
    try:
        return _clean(await service.record_payment(
            db, ctx["business_id"], order_id, body.get("amount"), "merchant_recorded", ctx["user_id"],
            reference=str(body.get("reference") or ""), method=str(body.get("method") or ""), note=str(body.get("note") or "")))
    except OrderError as e:
        _fail(e)


@router.post("/{order_id}/fulfilments")
async def fulfilment(order_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                     db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    try:
        return _clean(await service.update_fulfilment(db, ctx["business_id"], order_id, str(body.get("line_id") or ""),
                                                      str(body.get("status") or ""), ctx["user_id"],
                                                      str(body.get("reference") or ""), str(body.get("note") or "")))
    except OrderError as e:
        _fail(e)


@router.post("/{order_id}/cancel")
async def cancel_order(order_id: str, body: dict = Body(default={}), ctx: Ctx = Depends(_ctx),
                       db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    try:
        order = await service.get_order(db, ctx["business_id"], order_id)
        if order["paid_amount"] > 0:
            _manager(ctx)
        return _clean(await service.cancel(db, ctx["business_id"], order_id, str(body.get("reason") or ""), ctx["user_id"]))
    except OrderError as e:
        _fail(e)


@router.post("/{order_id}/refund-requests", status_code=201)
async def refund_request(order_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                         db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    _manager(ctx)
    try:
        return _clean(await service.request_refund(db, ctx["business_id"], order_id, body.get("amount"),
                                                   str(body.get("reason") or ""), ctx["user_id"]))
    except OrderError as e:
        _fail(e)


@router.get("/{order_id}/events")
async def order_events(order_id: str, ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(_db_ready)) -> dict:
    try:
        return {"events": _clean(await service.timeline(db, ctx["business_id"], order_id))}
    except OrderError as e:
        _fail(e)
