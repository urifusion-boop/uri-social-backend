"""Products & Services API: the catalog, availability, stock, reservations, reports, and the
controlled tools the AI sales agent calls. Every query is scoped to the active brand, and
backend rules — not the caller — decide what is allowed."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_active_brand_context, get_db_dependency

from . import ai_tools, alerts, availability, events, importer, items, reports, reservations, schedule, stock
from .indexes import ensure_indexes
from .models import (
    ADJUSTMENT_TYPES, EVENTS, MOVEMENTS, SLOTS, STOCK, CatalogError, iso, to_db, utcnow,
)

router = APIRouter(prefix="/catalog", tags=["Catalog & Availability"])


class Ctx(dict):
    pass


async def _ctx(brand_ctx: dict = Depends(get_active_brand_context),
               db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> Ctx:
    await ensure_indexes(db)
    role = "owner"
    if brand_ctx.get("agency_id"):
        from app.services.AgencyService import AgencyService
        admin = await AgencyService.is_agency_admin(brand_ctx["user_id"], brand_ctx["agency_id"], db)
        role = "manager" if admin else "staff"
    return Ctx(business_id=brand_ctx["brand_id"], user_id=brand_ctx["user_id"], role=role)


def _manager(ctx: Ctx) -> None:
    if ctx["role"] not in ("owner", "manager"):
        raise HTTPException(status_code=403, detail="Only the owner or a manager can change this.")


def _fail(e: CatalogError):
    raise HTTPException(status_code=e.status, detail=e.detail)


def _ser(doc: Optional[dict]) -> Optional[dict]:
    return items.serialise(doc, "owner")


# ── items ────────────────────────────────────────────────────────────────────

@router.post("/items", status_code=201)
async def create_item(body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                      db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    try:
        doc = await items.create_item(db, ctx["business_id"], body, ctx["user_id"])
        return await items.detail(db, ctx["business_id"], doc["_id"], items.view_for(ctx["role"]))
    except CatalogError as e:
        _fail(e)


@router.get("/items")
async def list_items(q: str = "", status: str = "", item_type: str = "", category: str = "",
                     limit: int = Query(50, le=200), skip: int = 0, ctx: Ctx = Depends(_ctx),
                     db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    return await items.list_items(db, ctx["business_id"], q, status, item_type, category, limit, skip,
                                  items.view_for(ctx["role"]))


@router.get("/items/{item_id}")
async def get_item(item_id: str, ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return await items.detail(db, ctx["business_id"], item_id, items.view_for(ctx["role"]))
    except CatalogError as e:
        _fail(e)


@router.patch("/items/{item_id}")
async def update_item(item_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                      db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    try:
        await items.update_item(db, ctx["business_id"], item_id, body, ctx["user_id"])
        return await items.detail(db, ctx["business_id"], item_id, items.view_for(ctx["role"]))
    except CatalogError as e:
        _fail(e)


@router.delete("/items/{item_id}")
async def archive_item(item_id: str, ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    try:
        return _ser(await items.archive_item(db, ctx["business_id"], item_id, ctx["user_id"]))
    except CatalogError as e:
        _fail(e)


@router.post("/items/{item_id}/variants", status_code=201)
async def create_variant(item_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                         db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    try:
        return _ser(await items.create_variant(db, ctx["business_id"], item_id, body, ctx["user_id"]))
    except CatalogError as e:
        _fail(e)


@router.patch("/items/{item_id}/variants/{variant_id}")
async def update_variant(item_id: str, variant_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                         db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    try:
        return _ser(await items.update_variant(db, ctx["business_id"], item_id, variant_id, body, ctx["user_id"]))
    except CatalogError as e:
        _fail(e)


@router.post("/items/{item_id}/manual-availability")
async def manual_availability(item_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                              db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    try:
        return _ser(await items.set_manual_availability(db, ctx["business_id"], item_id, bool(body.get("available")),
                                                        str(body.get("note") or ""), ctx["user_id"]))
    except CatalogError as e:
        _fail(e)


@router.post("/items/{item_id}/sync")
async def external_sync(item_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                        db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    """What a provider integration reports; marks the item as externally sourced with the sync time."""
    _manager(ctx)
    try:
        qty = body.get("available_quantity")
        return _ser(await items.record_external_sync(
            db, ctx["business_id"], item_id, str(body.get("provider") or "provider"),
            None if qty in (None, "") else int(qty), body.get("available"), ctx["user_id"]))
    except (CatalogError, ValueError) as e:
        _fail(e if isinstance(e, CatalogError) else CatalogError("Quantity must be a whole number."))


# ── sessions, departures and appointment times ───────────────────────────────

@router.post("/items/{item_id}/slots", status_code=201)
async def create_slot(item_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                      db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    try:
        item = await items.get_item(db, ctx["business_id"], item_id)
        if item["availability_type"] != "capacity":
            raise CatalogError("Add sessions to items whose availability is capacity-based.")
        start, end = to_db(body.get("start_at")), to_db(body.get("end_at"))
        total = int(body.get("total") or 0)
        if not start or not end or end <= start:
            raise CatalogError("Give a start and an end, with the end after the start.")
        if total < 1:
            raise CatalogError("Capacity must be at least 1.")
        slot = await stock.upsert_slot(db, ctx["business_id"], item_id, start, end, total,
                                       resource_id=str(body.get("resource_id") or ""), label=str(body.get("label") or ""))
        return _ser(slot)
    except CatalogError as e:
        _fail(e)


@router.patch("/slots/{slot_id}")
async def update_slot(slot_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                      db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    slot = await db[SLOTS].find_one({"_id": slot_id, "business_id": ctx["business_id"]})
    if not slot:
        raise HTTPException(status_code=404, detail="Session not found.")
    changes: dict[str, Any] = {}
    if body.get("status") in ("open", "closed"):
        changes["status"] = body["status"]
    if body.get("label") is not None:
        changes["label"] = str(body["label"])[:120]
    if body.get("total") is not None:
        total = int(body["total"])
        if total < slot["reserved"] + slot["confirmed"]:
            raise HTTPException(status_code=409, detail="Capacity can't go below what is already reserved or booked.")
        res = await db[SLOTS].update_one(
            {"_id": slot_id, "reserved": slot["reserved"], "confirmed": slot["confirmed"]},
            {"$set": {"total": total, "remaining": total - slot["reserved"] - slot["confirmed"], **changes}})
        if res.modified_count != 1:
            raise HTTPException(status_code=409, detail="Bookings changed while you edited. Try again.")
    elif changes:
        await db[SLOTS].update_one({"_id": slot_id}, {"$set": changes})
    return _ser(await db[SLOTS].find_one({"_id": slot_id}))


# ── availability ─────────────────────────────────────────────────────────────

@router.get("/availability/check")
async def check(item_id: str, variant_id: Optional[str] = None, quantity: int = 1, start_at: Optional[str] = None,
                end_at: Optional[str] = None, ctx: Ctx = Depends(_ctx),
                db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        await reservations.expire_due(db, ctx["business_id"], item_id=item_id)
        return await availability.check(db, ctx["business_id"], item_id, variant_id, quantity, start_at, end_at)
    except CatalogError as e:
        _fail(e)


@router.get("/availability/calendar")
async def calendar(item_id: str, start: Optional[str] = None, end: Optional[str] = None, quantity: int = 1,
                   ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    """Dates and times that can be booked, for capacity and appointment items."""
    try:
        item = await items.get_item(db, ctx["business_id"], item_id)
        if item["availability_type"] not in ("capacity", "schedule"):
            raise CatalogError("This item has no calendar.")
        now = utcnow()
        first, last = to_db(start) or now, to_db(end) or now + timedelta(days=30)
        if item["availability_type"] == "schedule":
            await schedule.ensure_slots(db, item, schedule.local_day(item, first), schedule.local_day(item, last), now)
        rows = await stock.list_slots(db, ctx["business_id"], item_id, first, last, 1000)
        return {"item_id": item_id, "slots": [{**_ser(s), "bookable": schedule.bookable(item, s, quantity, now)} for s in rows]}
    except CatalogError as e:
        _fail(e)


# ── stock ────────────────────────────────────────────────────────────────────

@router.post("/inventory/adjustments", status_code=201)
async def adjust_stock(body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                       db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    biz, actor = ctx["business_id"], ctx["user_id"]
    try:
        item = await items.get_item(db, biz, body.get("item_id", ""))
        if item["availability_type"] != "quantity":
            raise CatalogError("Only items with quantity-based availability track stock.")
        variant_id = body.get("variant_id") or None
        if variant_id:
            await items.get_variant(db, biz, item["_id"], variant_id)
        before = (await stock.ensure_stock(db, biz, item["_id"], variant_id, actor=actor))["on_hand"]
        threshold = body.get("low_stock_threshold")
        if body.get("set_to") not in (None, ""):
            row = await stock.set_on_hand(db, biz, item["_id"], variant_id, int(body["set_to"]),
                                          str(body.get("reason") or "Stock count"), actor)
        else:
            if body.get("movement_type") not in ADJUSTMENT_TYPES:
                raise CatalogError(f"Movement type must be one of: {', '.join(ADJUSTMENT_TYPES)}.")
            row = await stock.adjust(db, biz, item["_id"], variant_id, body["movement_type"], body.get("quantity"),
                                     body.get("quantity_change"), str(body.get("reason") or ""), actor,
                                     threshold=None if threshold in (None, "") else int(threshold))
        if threshold not in (None, "") and body.get("set_to") not in (None, ""):
            await db[STOCK].update_one({"_id": row["_id"]}, {"$set": {"low_stock_threshold": int(threshold)}})
        change = row["on_hand"] - before
        if change:
            await events.emit(db, biz, "inventory.stock.adjusted", {"item_id": item["_id"], "variant_id": variant_id, "change": change})
            await events.emit(db, biz, "inventory.availability.changed", {"item_id": item["_id"]})
            await alerts.notify_unusual(db, biz, item, before, change, actor)
        return {"on_hand": row["on_hand"], "reserved": row["reserved"], "available": row["available"], "change": change}
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Quantities must be whole numbers.")
    except CatalogError as e:
        _fail(e)


@router.get("/inventory/movements")
async def movements(item_id: str = "", variant_id: str = "", limit: int = Query(100, le=500), skip: int = 0,
                    ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    flt: dict[str, Any] = {"business_id": ctx["business_id"]}
    if item_id:
        flt["item_id"] = item_id
    if variant_id:
        flt["variant_id"] = variant_id
    total = await db[MOVEMENTS].count_documents(flt)
    rows = await db[MOVEMENTS].find(flt).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    names = {i["_id"]: i["name"] for i in await db["catalog_items"].find(
        {"business_id": ctx["business_id"], "_id": {"$in": list({r["item_id"] for r in rows})}}).to_list(500)}
    return {"movements": [{**_ser(r), "item_name": names.get(r["item_id"], "")} for r in rows], "total": total}


@router.get("/inventory/reconcile")
async def reconcile(item_id: str, variant_id: Optional[str] = None, ctx: Ctx = Depends(_ctx),
                    db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    return await stock.reconcile(db, ctx["business_id"], item_id, variant_id)


# ── reservations ─────────────────────────────────────────────────────────────

@router.post("/reservations", status_code=201)
async def reserve(body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                  db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        res = await reservations.reserve(
            db, ctx["business_id"], body.get("item_id", ""), body.get("quantity", 1), body.get("variant_id") or None,
            str(body.get("order_id") or ""), body.get("start_at"), body.get("end_at"), str(body.get("resource_id") or ""),
            ctx["user_id"], str(body.get("idempotency_key") or ""), "dashboard", body.get("customer") or {})
        return _ser(res)
    except CatalogError as e:
        _fail(e)


@router.get("/reservations")
async def list_reservations(status: str = "", item_id: str = "", order_id: str = "", limit: int = Query(100, le=200),
                            skip: int = 0, ctx: Ctx = Depends(_ctx),
                            db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    await reservations.expire_due(db, ctx["business_id"])
    out = await reservations.list_reservations(db, ctx["business_id"], status, item_id, order_id, limit, skip)
    names = {i["_id"]: i["name"] for i in await db["catalog_items"].find(
        {"business_id": ctx["business_id"], "_id": {"$in": list({r["item_id"] for r in out["reservations"]})}}).to_list(500)}
    return {"reservations": [{**_ser(r), "item_name": names.get(r["item_id"], "")} for r in out["reservations"]],
            "total": out["total"]}


@router.get("/reservations/{reservation_id}")
async def get_reservation(reservation_id: str, ctx: Ctx = Depends(_ctx),
                          db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return _ser(await reservations.get_reservation(db, ctx["business_id"], reservation_id))
    except CatalogError as e:
        _fail(e)


@router.post("/reservations/{reservation_id}/extend")
async def extend(reservation_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                 db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return _ser(await reservations.extend(db, ctx["business_id"], reservation_id, body.get("minutes"), ctx["user_id"]))
    except CatalogError as e:
        _fail(e)


@router.post("/reservations/{reservation_id}/release")
async def release(reservation_id: str, body: dict = Body(default={}), ctx: Ctx = Depends(_ctx),
                  db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return _ser(await reservations.release(db, ctx["business_id"], reservation_id, str(body.get("reason") or ""), ctx["user_id"]))
    except CatalogError as e:
        _fail(e)


def _transition(name: str):
    async def handler(reservation_id: str, body: dict = Body(default={}), ctx: Ctx = Depends(_ctx),
                      db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
        _manager(ctx)
        biz, actor = ctx["business_id"], ctx["user_id"]
        try:
            if name == "confirm":       # payment verified (the payment service calls the same code)
                return _ser(await reservations.commit(db, biz, reservation_id, actor, str(body.get("order_id") or "")))
            if name == "approve":
                return _ser(await reservations.approve(db, biz, reservation_id, actor))
            if name == "decline":
                return _ser(await reservations.decline(db, biz, reservation_id, str(body.get("reason") or ""), actor))
            if name == "fulfil":
                return _ser(await reservations.fulfil(db, biz, reservation_id, actor))
            return _ser(await reservations.mark_payment_processing(db, biz, reservation_id))
        except CatalogError as e:
            _fail(e)
    return handler


for _name, _path in (("confirm", "confirm"), ("approve", "approve"), ("decline", "decline"), ("fulfil", "fulfil"),
                     ("processing", "payment-processing")):
    router.post(f"/reservations/{{reservation_id}}/{_path}")(_transition(_name))


# ── alerts, reports, events, import and export ───────────────────────────────

@router.get("/alerts")
async def get_alerts(ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    await reservations.expire_due(db, ctx["business_id"])
    return {"alerts": await alerts.compute_alerts(db, ctx["business_id"])}


@router.get("/reports/summary")
async def report(days: int = 30, ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    return await reports.summary(db, ctx["business_id"], days)


@router.get("/events")
async def list_events(limit: int = Query(50, le=200), ctx: Ctx = Depends(_ctx),
                      db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    rows = await db[EVENTS].find({"business_id": ctx["business_id"]}).sort("created_at", -1).limit(limit).to_list(limit)
    return {"events": [{k: (iso(v) if hasattr(v, "isoformat") else v) for k, v in r.items() if k != "dedupe_key"}
                       for r in rows]}


@router.get("/export")
async def export_catalog(ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> Response:
    _manager(ctx)
    return Response(content=await importer.export_csv(db, ctx["business_id"]), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=products-and-services.csv"})


@router.post("/import")
async def import_catalog(body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                         db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    _manager(ctx)
    text = str(body.get("csv") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="The file is empty.")
    try:
        return await importer.import_csv(db, ctx["business_id"], text, ctx["user_id"], bool(body.get("dry_run")))
    except CatalogError as e:
        _fail(e)


# ── the AI sales agent's tools ───────────────────────────────────────────────

@router.get("/ai/search")
async def ai_search(q: str, limit: int = 5, ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    return {"items": await ai_tools.ai_search(db, ctx["business_id"], q, limit)}


@router.get("/ai/items/{item_id}")
async def ai_item(item_id: str, ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return await ai_tools.ai_details(db, ctx["business_id"], item_id)
    except CatalogError as e:
        _fail(e)


@router.get("/ai/check")
async def ai_check(item_id: str, variant_id: Optional[str] = None, quantity: int = 1, start_at: Optional[str] = None,
                   end_at: Optional[str] = None, ctx: Ctx = Depends(_ctx),
                   db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return await ai_tools.ai_check(db, ctx["business_id"], item_id, variant_id, quantity, start_at, end_at)
    except CatalogError as e:
        _fail(e)


@router.post("/ai/compare")
async def ai_compare(body: dict = Body(...), ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return {"items": await ai_tools.ai_compare(db, ctx["business_id"], list(body.get("item_ids") or []))}
    except CatalogError as e:
        _fail(e)


@router.get("/ai/alternatives/{item_id}")
async def ai_alternatives(item_id: str, ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return {"items": await ai_tools.ai_alternatives(db, ctx["business_id"], item_id)}
    except CatalogError as e:
        _fail(e)


@router.post("/ai/draft-orders", status_code=201)
async def ai_draft_order(body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                         db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return _ser(await ai_tools.create_draft_order(db, ctx["business_id"], list(body.get("lines") or []),
                                                      body.get("customer") or {}))
    except CatalogError as e:
        _fail(e)


@router.post("/ai/draft-orders/{draft_id}/reserve")
async def ai_reserve(draft_id: str, body: dict = Body(...), ctx: Ctx = Depends(_ctx),
                     db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return _ser(await ai_tools.reserve_draft_order(db, ctx["business_id"], draft_id, body.get("customer_confirmed")))
    except CatalogError as e:
        _fail(e)


@router.post("/ai/draft-orders/{draft_id}/release")
async def ai_release(draft_id: str, ctx: Ctx = Depends(_ctx), db: AsyncIOMotorDatabase = Depends(get_db_dependency)) -> dict:
    try:
        return _ser(await ai_tools.release_draft_order(db, ctx["business_id"], draft_id))
    except CatalogError as e:
        _fail(e)
