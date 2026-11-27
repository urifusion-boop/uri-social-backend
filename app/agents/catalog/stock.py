"""Stock counters, the movement ledger and bookable slots.

Every counter change is ONE conditional update ("only if enough is available"), never a
read followed by a write, so two customers reaching for the last unit cannot both win.
`available` is kept as its own counter (on_hand - reserved) for that reason: the check
and the change are the same atomic operation. Every change also writes a movement, and
`reconcile` proves the counters still equal the ledger.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .models import ADJUSTMENT_TYPES, MOVEMENTS, SLOTS, STOCK, CatalogError, utcnow


def _key(business_id: str, item_id: str, variant_id: Optional[str], period: str = "") -> dict:
    return {"business_id": business_id, "item_id": item_id, "variant_id": variant_id or None,
            "location_id": "default", "period_key": period}


async def get_stock(db, business_id: str, item_id: str, variant_id: Optional[str] = None,
                    period: str = "") -> Optional[dict]:
    return await db[STOCK].find_one(_key(business_id, item_id, variant_id, period))


async def record_movement(db, business_id: str, item_id: str, variant_id: Optional[str], movement_type: str,
                          on_hand_change: int = 0, reserved_change: int = 0, reason: str = "",
                          reference_id: str = "", actor: str = "", on_hand_after: Optional[int] = None,
                          period: str = "") -> dict:
    doc = {"_id": uuid.uuid4().hex, "business_id": business_id, "item_id": item_id,
           "variant_id": variant_id or None, "period_key": period, "movement_type": movement_type,
           "quantity_change": on_hand_change, "reserved_change": reserved_change, "reason": reason[:300],
           "reference_id": reference_id, "created_by": actor, "on_hand_after": on_hand_after,
           "created_at": utcnow()}
    await db[MOVEMENTS].insert_one(doc)
    return doc


async def ensure_stock(db, business_id: str, item_id: str, variant_id: Optional[str] = None, period: str = "",
                       opening: int = 0, actor: str = "system", reason: str = "Opening balance",
                       threshold: Optional[int] = None) -> dict:
    existing = await get_stock(db, business_id, item_id, variant_id, period)
    if existing:
        return existing
    doc = {**_key(business_id, item_id, variant_id, period), "_id": uuid.uuid4().hex, "on_hand": opening,
           "reserved": 0, "available": opening, "low_stock_threshold": threshold, "updated_at": utcnow()}
    try:
        await db[STOCK].insert_one(doc)
    except DuplicateKeyError:
        return await get_stock(db, business_id, item_id, variant_id, period)
    if opening:
        await record_movement(db, business_id, item_id, variant_id, "received", opening, 0, reason,
                              actor=actor, on_hand_after=opening, period=period)
    return doc


def _signed(movement_type: str, quantity: Any, quantity_change: Any) -> int:
    if movement_type not in ADJUSTMENT_TYPES:
        raise CatalogError(f"Movement type must be one of: {', '.join(ADJUSTMENT_TYPES)}.")
    if movement_type == "adjusted":
        try:
            change = int(quantity_change)
        except (TypeError, ValueError):
            raise CatalogError("Give the change as a whole number, such as -3 or 10.")
        if change == 0:
            raise CatalogError("A stock adjustment can't be zero.")
        return change
    try:
        amount = int(quantity)
    except (TypeError, ValueError):
        raise CatalogError("Quantity must be a whole number.")
    if amount <= 0:
        raise CatalogError("Quantity must be more than zero.")
    return amount if movement_type in ("received", "returned") else -amount


async def adjust(db, business_id: str, item_id: str, variant_id: Optional[str], movement_type: str,
                 quantity: Any = None, quantity_change: Any = None, reason: str = "", actor: str = "",
                 reference_id: str = "", period: str = "", threshold: Optional[int] = None) -> dict:
    """Change on-hand stock and record why. Reserved units can't be taken away."""
    change = _signed(movement_type, quantity, quantity_change)
    if not reason.strip() and movement_type in ("adjusted", "damaged", "lost"):
        raise CatalogError("Say why the stock changed.")
    await ensure_stock(db, business_id, item_id, variant_id, period, actor=actor)
    flt = _key(business_id, item_id, variant_id, period)
    if change < 0:
        flt["available"] = {"$gte": -change}
    update: dict[str, Any] = {"$inc": {"on_hand": change, "available": change}, "$set": {"updated_at": utcnow()}}
    if threshold is not None:
        update["$set"]["low_stock_threshold"] = int(threshold)
    after = await db[STOCK].find_one_and_update(flt, update, return_document=ReturnDocument.AFTER)
    if not after:
        have = await get_stock(db, business_id, item_id, variant_id, period)
        raise CatalogError(
            f"Only {(have or {}).get('available', 0)} unreserved in stock, so {-change} can't be removed.",
            409, "insufficient_stock", available=(have or {}).get("available", 0))
    await record_movement(db, business_id, item_id, variant_id, movement_type, change, 0, reason,
                          reference_id, actor, after["on_hand"], period)
    after["_change"] = change
    return after


async def set_on_hand(db, business_id: str, item_id: str, variant_id: Optional[str], target: int,
                      reason: str, actor: str, period: str = "") -> dict:
    if target < 0:
        raise CatalogError("Stock can't be negative.")
    have = await ensure_stock(db, business_id, item_id, variant_id, period, actor=actor)
    if target == have["on_hand"]:
        return have
    return await adjust(db, business_id, item_id, variant_id, "adjusted", quantity_change=target - have["on_hand"],
                        reason=reason or "Stock count", actor=actor, period=period)


async def reconcile(db, business_id: str, item_id: str, variant_id: Optional[str] = None,
                    period: str = "") -> dict:
    """The counters must equal the sum of the ledger; a mismatch means a change went unrecorded."""
    stock = await get_stock(db, business_id, item_id, variant_id, period)
    rows = await db[MOVEMENTS].find({"business_id": business_id, "item_id": item_id,
                                     "variant_id": variant_id or None, "period_key": period}).to_list(100000)
    on_hand = sum(r.get("quantity_change", 0) for r in rows)
    reserved = sum(r.get("reserved_change", 0) for r in rows)
    have_on_hand, have_reserved = (stock or {}).get("on_hand", 0), (stock or {}).get("reserved", 0)
    return {"on_hand": have_on_hand, "reserved": have_reserved, "ledger_on_hand": on_hand,
            "ledger_reserved": reserved, "matches": have_on_hand == on_hand and have_reserved == reserved}


# ── counter moves used by reservations (each is one conditional update) ──────

async def hold_stock(db, business_id, item_id, variant_id, qty: int, period: str = "") -> Optional[dict]:
    return await db[STOCK].find_one_and_update(
        {**_key(business_id, item_id, variant_id, period), "available": {"$gte": qty}},
        {"$inc": {"available": -qty, "reserved": qty}, "$set": {"updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER)


async def release_stock(db, business_id, item_id, variant_id, qty: int, period: str = "") -> bool:
    res = await db[STOCK].update_one(
        {**_key(business_id, item_id, variant_id, period), "reserved": {"$gte": qty}},
        {"$inc": {"available": qty, "reserved": -qty}, "$set": {"updated_at": utcnow()}})
    return res.modified_count == 1


async def sell_stock(db, business_id, item_id, variant_id, qty: int, period: str = "") -> Optional[dict]:
    return await db[STOCK].find_one_and_update(
        {**_key(business_id, item_id, variant_id, period), "reserved": {"$gte": qty}},
        {"$inc": {"on_hand": -qty, "reserved": -qty}, "$set": {"updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER)


async def restock(db, business_id, item_id, variant_id, qty: int, period: str = "") -> Optional[dict]:
    return await db[STOCK].find_one_and_update(
        _key(business_id, item_id, variant_id, period),
        {"$inc": {"on_hand": qty, "available": qty}, "$set": {"updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER)


# ── slots: sessions, departures and appointment times ────────────────────────

def _slot_key(item_id: str, variant_id: Optional[str], resource_id: str, start: datetime) -> dict:
    return {"item_id": item_id, "variant_id": variant_id or None, "resource_id": resource_id or "",
            "start_at": start}


async def upsert_slot(db, business_id: str, item_id: str, start: datetime, end: datetime, total: int,
                      variant_id: Optional[str] = None, resource_id: str = "", label: str = "") -> dict:
    key = _slot_key(item_id, variant_id, resource_id, start)
    found = await db[SLOTS].find_one(key)
    if found:
        return found
    doc = {**key, "_id": uuid.uuid4().hex, "business_id": business_id, "end_at": end, "label": label[:120],
           "total": total, "reserved": 0, "confirmed": 0, "remaining": total, "status": "open",
           "created_at": utcnow()}
    try:
        await db[SLOTS].insert_one(doc)
    except DuplicateKeyError:
        return await db[SLOTS].find_one(key)
    return doc


async def list_slots(db, business_id: str, item_id: str, start: Optional[datetime] = None,
                     end: Optional[datetime] = None, limit: int = 500) -> list[dict]:
    query: dict[str, Any] = {"business_id": business_id, "item_id": item_id}
    if start or end:
        query["start_at"] = {k: v for k, v in (("$gte", start), ("$lt", end)) if v}
    return await db[SLOTS].find(query).sort("start_at", 1).to_list(limit)


async def find_slot(db, business_id: str, item_id: str, at: datetime, resource_id: str = "") -> Optional[dict]:
    """The slot that starts at this moment, or the one it falls inside."""
    exact = await db[SLOTS].find_one({"business_id": business_id, "item_id": item_id, "start_at": at,
                                      **({"resource_id": resource_id} if resource_id else {})})
    if exact:
        return exact
    return await db[SLOTS].find_one({"business_id": business_id, "item_id": item_id,
                                     "start_at": {"$lte": at}, "end_at": {"$gt": at},
                                     **({"resource_id": resource_id} if resource_id else {})})


async def hold_slot(db, slot_id: str, qty: int) -> Optional[dict]:
    return await db[SLOTS].find_one_and_update(
        {"_id": slot_id, "status": "open", "remaining": {"$gte": qty}},
        {"$inc": {"remaining": -qty, "reserved": qty}}, return_document=ReturnDocument.AFTER)


async def release_slot(db, slot_id: str, qty: int, confirmed: bool = False) -> bool:
    field = "confirmed" if confirmed else "reserved"
    res = await db[SLOTS].update_one({"_id": slot_id, field: {"$gte": qty}},
                                     {"$inc": {"remaining": qty, field: -qty}})
    return res.modified_count == 1


async def confirm_slot(db, slot_id: str, qty: int) -> bool:
    res = await db[SLOTS].update_one({"_id": slot_id, "reserved": {"$gte": qty}},
                                     {"$inc": {"reserved": -qty, "confirmed": qty}})
    return res.modified_count == 1
