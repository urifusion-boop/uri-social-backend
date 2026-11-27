"""Reserving, committing and releasing availability.

A reservation is a promise that holds real availability until it is paid for, released or
runs out. Every state change is a conditional update on the reservation itself, and only the
caller that wins that update touches the counters, so a payment confirmation racing an expiry
sweep can't both release and sell the same units.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from . import events, schedule, stock
from .items import get_item, list_variants
from .models import (
    ACTIVE_RESERVATION, AWAITING, CANCELLED, CONFIRMED, DECLINED, EXPIRED, FULFILLED, MAX_RESERVATION_MINUTES,
    PENDING, RELEASED, RESERVATIONS, CatalogError, period_key, to_db, utcnow,
)

PAYMENT_GRACE_MINUTES = 30


def _expiry(item: dict, awaiting: bool, now: datetime) -> Optional[datetime]:
    if awaiting:
        return now + timedelta(hours=item.get("confirmation_hours") or 24)
    minutes = item.get("reservation_minutes")
    return now + timedelta(minutes=minutes) if minutes else None


async def get_reservation(db, business_id: str, reservation_id: str) -> dict:
    res = await db[RESERVATIONS].find_one({"_id": reservation_id, "business_id": business_id})
    if not res:
        raise CatalogError("Reservation not found.", 404)
    return res


async def list_reservations(db, business_id: str, status: str = "", item_id: str = "", order_id: str = "",
                            limit: int = 100, skip: int = 0) -> dict:
    flt: dict[str, Any] = {"business_id": business_id}
    if status:
        flt["status"] = {"$in": status.split(",")}
    if item_id:
        flt["item_id"] = item_id
    if order_id:
        flt["order_id"] = order_id
    total = await db[RESERVATIONS].count_documents(flt)
    rows = await db[RESERVATIONS].find(flt).sort("created_at", -1).skip(max(0, skip)).limit(max(1, min(limit, 200))).to_list(200)
    return {"reservations": rows, "total": total}


# ── taking and giving back availability, by model ────────────────────────────

async def _take_hold(db, item: dict, variant_id: Optional[str], qty: int, start: Optional[datetime],
                     now: datetime) -> dict:
    biz, item_id, kind, cfg = item["business_id"], item["_id"], item["availability_type"], item["availability_config"]
    if kind in ("unlimited", "manual"):
        return {"kind": "none"}
    if kind in ("quantity", "capped", "made_to_order"):
        stock_variant, period = variant_id, ""
        if kind == "capped":
            stock_variant, period = None, period_key(cfg["period"], now)
            await stock.ensure_stock(db, biz, item_id, None, period, opening=cfg["cap"], reason="New sales period")
        elif kind == "made_to_order":
            if cfg.get("max_orders") is None:
                return {"kind": "none"}
            stock_variant, period = None, period_key(cfg["period"], now)
            await stock.ensure_stock(db, biz, item_id, None, period, opening=cfg["max_orders"], reason="New production period")
        row = await stock.hold_stock(db, biz, item_id, stock_variant, qty, period)
        if not row:
            have = await stock.get_stock(db, biz, item_id, stock_variant, period)
            left = (have or {}).get("available", 0)
            raise CatalogError("Not enough available." if left else "Out of stock.", 409,
                               "insufficient_availability", available=left)
        return {"kind": "stock", "variant_id": stock_variant, "period_key": period}
    if start is None:
        raise CatalogError("Choose the date and time first.", 400, "time_required")
    if kind == "schedule":
        day = schedule.local_day(item, start)
        await schedule.ensure_slots(db, item, day, day, now)
    slot = await stock.find_slot(db, biz, item_id, start)
    if not slot:
        raise CatalogError("There is no session at that time.", 409, "no_such_slot")
    if not schedule.bookable(item, slot, 1, now):
        raise CatalogError("That time can't be booked.", 409, "slot_closed")
    held = await stock.hold_slot(db, slot["_id"], qty)
    if not held:
        raise CatalogError("Not enough places left at that time.", 409, "insufficient_availability",
                           available=slot["remaining"])
    return {"kind": "slot", "slot_id": slot["_id"]}


async def _give_back(db, res: dict, committed: bool, reason: str, actor: str) -> None:
    hold, biz, qty = res.get("hold") or {"kind": "none"}, res["business_id"], res["quantity"]
    if hold["kind"] == "stock":
        item_id, variant, period = res["item_id"], hold.get("variant_id"), hold.get("period_key", "")
        if committed:
            row = await stock.restock(db, biz, item_id, variant, qty, period)
            await stock.record_movement(db, biz, item_id, variant, "returned", qty, 0, reason or "Order cancelled",
                                        res["_id"], actor, (row or {}).get("on_hand"), period)
        elif await stock.release_stock(db, biz, item_id, variant, qty, period):
            await stock.record_movement(db, biz, item_id, variant, "released", 0, -qty, reason, res["_id"], actor,
                                        period=period)
    elif hold["kind"] == "slot":
        await stock.release_slot(db, hold["slot_id"], qty, confirmed=committed)


# ── lifecycle ────────────────────────────────────────────────────────────────

async def reserve(db, business_id: str, item_id: str, quantity: int = 1, variant_id: Optional[str] = None,
                  order_id: str = "", start_at: Any = None, end_at: Any = None, resource_id: str = "",
                  actor: str = "", idempotency_key: str = "", source: str = "api",
                  customer: Optional[dict] = None, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise CatalogError("Quantity must be at least 1.")
    key = (idempotency_key or "").strip()[:120]
    if key:
        again = await db[RESERVATIONS].find_one({"business_id": business_id, "idempotency_key": key})
        if again:
            return {**again, "idempotent_replay": True}
    await expire_due(db, business_id, item_id=item_id, now=now)
    item = await get_item(db, business_id, item_id)
    if item["status"] != "active":
        raise CatalogError("This item isn't for sale right now.", 409, "not_for_sale")
    variants = [v for v in await list_variants(db, business_id, item_id) if v["status"] == "active"]
    if variant_id and variant_id not in {v["_id"] for v in variants}:
        raise CatalogError("That option doesn't exist.", 404, "unknown_variant")
    if item["availability_type"] == "quantity" and variants and not variant_id:
        raise CatalogError("Choose an option (such as size or colour) first.", 400, "variant_required")
    start = to_db(start_at)
    hold = await _take_hold(db, item, variant_id, quantity, start, now)
    awaiting = bool(item.get("requires_confirmation"))
    doc = {"_id": uuid.uuid4().hex, "business_id": business_id, "item_id": item_id, "variant_id": variant_id or None,
           "order_id": order_id, "quantity": quantity, "resource_id": resource_id,
           "start_at": start, "end_at": to_db(end_at), "status": AWAITING if awaiting else PENDING,
           "expires_at": _expiry(item, awaiting, now), "hold": hold, "source": source, "created_by": actor,
           "customer": customer or {}, "created_at": now, "updated_at": now, "payment_status": ""}
    if hold["kind"] == "slot":
        slot = await db["catalog_slots"].find_one({"_id": hold["slot_id"]})
        doc["start_at"], doc["end_at"] = slot["start_at"], slot["end_at"]
    if key:
        doc["idempotency_key"] = key
        doc["idem_scope"] = f"{business_id}:{key}"
    try:
        await db[RESERVATIONS].insert_one(doc)
    except DuplicateKeyError:
        await _give_back(db, doc, False, "duplicate request", actor)
        return {**(await db[RESERVATIONS].find_one({"business_id": business_id, "idempotency_key": key})),
                "idempotent_replay": True}
    if hold["kind"] == "stock":
        await stock.record_movement(db, business_id, item_id, hold.get("variant_id"), "reserved", 0, quantity,
                                    "Reserved", doc["_id"], actor, period=hold.get("period_key", ""))
    await events.emit(db, business_id, "inventory.reservation.created", {"reservation_id": doc["_id"], "item_id": item_id},
                      dedupe_key=f"created:{doc['_id']}")
    await events.emit(db, business_id, "inventory.availability.changed", {"item_id": item_id})
    return doc


async def _claim(db, res_id: str, from_status: list[str], to_status: str, extra: Optional[dict] = None) -> Optional[dict]:
    return await db[RESERVATIONS].find_one_and_update(
        {"_id": res_id, "status": {"$in": from_status}},
        {"$set": {"status": to_status, "updated_at": utcnow(), **(extra or {})}}, return_document=ReturnDocument.AFTER)


async def commit(db, business_id: str, reservation_id: str, actor: str = "", order_id: str = "",
                 now: Optional[datetime] = None) -> dict:
    """Payment verified: turn the hold into a sale (or a confirmed booking)."""
    now = now or utcnow()
    res = await get_reservation(db, business_id, reservation_id)
    if res["status"] in (CONFIRMED, FULFILLED):
        return res
    if res["status"] == AWAITING:
        raise CatalogError("The team needs to approve this request before it can be paid for.", 409, "needs_approval")
    if res["status"] in (RELEASED, CANCELLED, DECLINED):
        raise CatalogError(f"This reservation was {res['status']}.", 409, "not_active")
    if res["status"] == EXPIRED:
        res = await _rehold(db, res, actor, now)
    won = await _claim(db, reservation_id, [PENDING], CONFIRMED,
                       {"committed_at": now, "expires_at": None, "payment_status": "paid",
                        **({"order_id": order_id} if order_id else {})})
    if not won:
        return await get_reservation(db, business_id, reservation_id)
    hold = won.get("hold") or {"kind": "none"}
    if hold["kind"] == "stock":
        row = await stock.sell_stock(db, business_id, won["item_id"], hold.get("variant_id"), won["quantity"],
                                     hold.get("period_key", ""))
        await stock.record_movement(db, business_id, won["item_id"], hold.get("variant_id"), "sold",
                                    -won["quantity"], -won["quantity"], "Sale", reservation_id, actor,
                                    (row or {}).get("on_hand"), hold.get("period_key", ""))
    elif hold["kind"] == "slot":
        await stock.confirm_slot(db, hold["slot_id"], won["quantity"])
    await events.emit(db, business_id, "inventory.reservation.confirmed",
                      {"reservation_id": reservation_id, "item_id": won["item_id"]}, dedupe_key=f"confirmed:{reservation_id}")
    return won


async def _rehold(db, res: dict, actor: str, now: datetime) -> dict:
    """Paid after the hold lapsed: take it again if the units are still free, else say so."""
    item = await get_item(db, res["business_id"], res["item_id"])
    try:
        hold = await _take_hold(db, item, res.get("variant_id"), res["quantity"], res.get("start_at"), now)
    except CatalogError:
        raise CatalogError("This reservation expired and the availability has since been taken. The payment "
                           "needs a refund or a manual decision.", 409, "expired_unavailable")
    back = await db[RESERVATIONS].find_one_and_update(
        {"_id": res["_id"], "status": EXPIRED}, {"$set": {"status": PENDING, "hold": hold, "updated_at": now,
                                                          "expires_at": now + timedelta(minutes=PAYMENT_GRACE_MINUTES)}},
        return_document=ReturnDocument.AFTER)
    if not back:                               # someone else revived it first: give ours back
        await _give_back(db, {**res, "hold": hold}, False, "duplicate revive", actor)
        return await get_reservation(db, res["business_id"], res["_id"])
    if hold["kind"] == "stock":
        await stock.record_movement(db, res["business_id"], res["item_id"], hold.get("variant_id"), "reserved", 0,
                                    res["quantity"], "Re-reserved after payment", res["_id"], actor,
                                    period=hold.get("period_key", ""))
    return back


async def approve(db, business_id: str, reservation_id: str, actor: str, now: Optional[datetime] = None) -> dict:
    """The business confirms a request that needed its say-so; payment can follow."""
    now = now or utcnow()
    res = await get_reservation(db, business_id, reservation_id)
    if res["status"] == PENDING:
        return res
    item = await get_item(db, business_id, res["item_id"])
    won = await _claim(db, reservation_id, [AWAITING], PENDING,
                       {"expires_at": _expiry({**item, "requires_confirmation": False}, False, now), "approved_by": actor,
                        "approved_at": now})
    if not won:
        raise CatalogError(f"This request is {res['status']}.", 409, "not_awaiting")
    await events.emit(db, business_id, "inventory.reservation.created", {"reservation_id": reservation_id, "approved": True},
                      dedupe_key=f"approved:{reservation_id}")
    return won


async def decline(db, business_id: str, reservation_id: str, reason: str, actor: str) -> dict:
    res = await get_reservation(db, business_id, reservation_id)
    won = await _claim(db, reservation_id, [AWAITING], DECLINED, {"release_reason": reason[:300], "released_at": utcnow()})
    if not won:
        raise CatalogError(f"This request is {res['status']}.", 409, "not_awaiting")
    await _give_back(db, won, False, reason or "Declined", actor)
    await events.emit(db, business_id, "inventory.reservation.released", {"reservation_id": reservation_id},
                      dedupe_key=f"released:{reservation_id}")
    return won


async def release(db, business_id: str, reservation_id: str, reason: str = "", actor: str = "") -> dict:
    """Give the availability back. Paid and unfulfilled goes back to stock; fulfilled never does on its own."""
    res = await get_reservation(db, business_id, reservation_id)
    if res["status"] == FULFILLED:
        raise CatalogError("This order was fulfilled. Add returned goods back to stock yourself if they can be resold.",
                           409, "fulfilled_no_restock")
    if res["status"] in (RELEASED, CANCELLED, DECLINED, EXPIRED):
        return res
    committed = res["status"] == CONFIRMED
    won = await _claim(db, reservation_id, [CONFIRMED] if committed else list(ACTIVE_RESERVATION),
                       CANCELLED if committed else RELEASED, {"release_reason": reason[:300], "released_at": utcnow()})
    if not won:
        return await get_reservation(db, business_id, reservation_id)
    await _give_back(db, won, committed, reason, actor)
    await events.emit(db, business_id, "inventory.reservation.released", {"reservation_id": reservation_id},
                      dedupe_key=f"released:{reservation_id}")
    await events.emit(db, business_id, "inventory.availability.changed", {"item_id": won["item_id"]})
    return won


async def extend(db, business_id: str, reservation_id: str, minutes: int, actor: str = "",
                 now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    if not isinstance(minutes, int) or not 1 <= minutes <= MAX_RESERVATION_MINUTES:
        raise CatalogError(f"Extend by 1 to {MAX_RESERVATION_MINUTES} minutes.")
    res = await get_reservation(db, business_id, reservation_id)
    if res["status"] not in ACTIVE_RESERVATION:
        raise CatalogError(f"A {res['status']} reservation can't be extended.", 409, "not_active")
    if not res.get("expires_at"):
        return res
    ceiling = res["created_at"] + timedelta(minutes=MAX_RESERVATION_MINUTES)
    new = min(max(res["expires_at"], now) + timedelta(minutes=minutes), ceiling)
    await db[RESERVATIONS].update_one({"_id": reservation_id, "status": {"$in": list(ACTIVE_RESERVATION)}},
                                      {"$set": {"expires_at": new, "updated_at": now}})
    return await get_reservation(db, business_id, reservation_id)


async def fulfil(db, business_id: str, reservation_id: str, actor: str = "") -> dict:
    res = await get_reservation(db, business_id, reservation_id)
    if res["status"] == FULFILLED:
        return res
    won = await _claim(db, reservation_id, [CONFIRMED], FULFILLED, {"fulfilled_at": utcnow(), "fulfilled_by": actor})
    if not won:
        raise CatalogError("Only a paid, confirmed order can be marked fulfilled.", 409, "not_confirmed")
    return won


async def mark_payment_processing(db, business_id: str, reservation_id: str) -> dict:
    """The payment provider says a transfer is on its way: a lapse then extends once instead of releasing."""
    res = await get_reservation(db, business_id, reservation_id)
    if res["status"] not in ACTIVE_RESERVATION:
        return res
    await db[RESERVATIONS].update_one({"_id": reservation_id}, {"$set": {"payment_status": "processing",
                                                                         "updated_at": utcnow()}})
    return await get_reservation(db, business_id, reservation_id)


async def expire_due(db, business_id: Optional[str] = None, item_id: Optional[str] = None,
                     now: Optional[datetime] = None, limit: int = 500) -> int:
    """Release every hold whose time is up. Safe to run anywhere, any number of times."""
    now = now or utcnow()
    flt: dict[str, Any] = {"status": {"$in": list(ACTIVE_RESERVATION)}, "expires_at": {"$lte": now, "$ne": None}}
    if business_id:
        flt["business_id"] = business_id
    if item_id:
        flt["item_id"] = item_id
    expired = 0
    for res in await db[RESERVATIONS].find(flt).to_list(limit):
        if res.get("payment_status") == "processing" and not res.get("payment_extended"):
            await db[RESERVATIONS].update_one(
                {"_id": res["_id"], "status": res["status"]},
                {"$set": {"expires_at": now + timedelta(minutes=PAYMENT_GRACE_MINUTES), "payment_extended": True}})
            continue
        won = await db[RESERVATIONS].find_one_and_update(
            {"_id": res["_id"], "status": {"$in": list(ACTIVE_RESERVATION)}, "expires_at": {"$lte": now}},
            {"$set": {"status": EXPIRED, "released_at": now, "release_reason": "expired", "updated_at": now}},
            return_document=ReturnDocument.AFTER)
        if not won:
            continue
        await _give_back(db, won, False, "Reservation expired", "system")
        await events.emit(db, won["business_id"], "inventory.reservation.expired", {"reservation_id": won["_id"]},
                          dedupe_key=f"expired:{won['_id']}")
        expired += 1
    return expired
