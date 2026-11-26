"""Catalog items and their variants: create, read, search, change, archive."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError

from . import events, stock
from .models import (
    COMMITMENTS, ITEMS, RESERVATIONS, STOCK, VARIANTS, CatalogError, iso, normalize_item, normalize_variant,
    period_key, utcnow,
)

PRIVATE_FIELDS = ("cost_price",)
INTERNAL_FIELDS = ("sku_scope", "idem_scope")


def scope(business_id: str, value: str) -> dict:
    """The field the unique index sits on; absent when there is nothing to keep unique."""
    return {"sku_scope": f"{business_id}:{value.lower()}"} if value else {}
STAFF_ROLES = ("owner", "manager")


def serialise(doc: Optional[dict], view: str = "staff") -> Optional[dict]:
    """JSON-safe copy. Cost price only ever leaves for owners and managers; a customer-facing
    view (the AI, a public page) also drops internal sales notes."""
    if doc is None:
        return None
    out = {("id" if k == "_id" else k): (iso(v) if isinstance(v, datetime) else v)
           for k, v in doc.items() if k not in INTERNAL_FIELDS}
    if view != "owner":
        for field in PRIVATE_FIELDS:
            out.pop(field, None)
    if view == "customer":
        out.pop("sales_notes", None)
        out.pop("created_by", None)
    return out


def effective_price(item: dict, variant: Optional[dict] = None) -> tuple[Optional[float], str]:
    if item.get("pricing_model") == "quote":
        return None, item.get("currency", "NGN")
    if variant and variant.get("price") is not None:
        return variant["price"], variant.get("currency") or item.get("currency", "NGN")
    return item.get("price"), item.get("currency", "NGN")


def view_for(role: str) -> str:
    return "owner" if role in STAFF_ROLES else "staff"


async def _unique_sku(db, business_id: str, sku: str, collection: str, keep: Optional[str] = None) -> None:
    if not sku:
        return
    other = await db[collection].find_one({"business_id": business_id, "sku": sku})
    if other and other["_id"] != keep:
        raise CatalogError(f"Another {'item' if collection == ITEMS else 'variant'} already uses SKU {sku}.",
                           409, "duplicate_sku")


async def get_item(db, business_id: str, item_id: str) -> dict:
    item = await db[ITEMS].find_one({"_id": item_id, "business_id": business_id})
    if not item:
        raise CatalogError("Item not found.", 404)
    return item


async def get_variant(db, business_id: str, item_id: str, variant_id: str) -> dict:
    variant = await db[VARIANTS].find_one({"_id": variant_id, "item_id": item_id, "business_id": business_id})
    if not variant:
        raise CatalogError("Variant not found.", 404)
    return variant


async def create_item(db, business_id: str, raw: dict, actor: str) -> dict:
    data = normalize_item(raw)
    await _unique_sku(db, business_id, data["sku"], ITEMS)
    now = utcnow()
    doc = {"_id": uuid.uuid4().hex, "business_id": business_id, **data, **scope(business_id, data["sku"]),
           "created_by": actor, "created_at": now, "updated_at": now, "availability_updated_at": now}
    try:
        await db[ITEMS].insert_one(doc)
    except DuplicateKeyError:
        raise CatalogError(f"Another item already uses SKU {data['sku']}.", 409, "duplicate_sku")
    variants = raw.get("variants") or []
    if doc["availability_type"] == "quantity" and not variants:
        await stock.ensure_stock(db, business_id, doc["_id"], None, opening=_opening(raw), actor=actor,
                                 threshold=data["low_stock_threshold"])
    for variant in variants:
        await create_variant(db, business_id, doc["_id"], variant, actor)
    await events.emit(db, business_id, "catalog.item.created", {"item_id": doc["_id"], "name": doc["name"]})
    return doc


def _opening(raw: dict) -> int:
    value = raw.get("initial_quantity", raw.get("quantity"))
    if value in (None, ""):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise CatalogError("Starting quantity must be a whole number.")
    if count < 0:
        raise CatalogError("Starting quantity can't be negative.")
    return count


async def update_item(db, business_id: str, item_id: str, patch: dict, actor: str) -> dict:
    item = await get_item(db, business_id, item_id)
    data = normalize_item(patch, item)
    await _unique_sku(db, business_id, data["sku"], ITEMS, keep=item_id)
    shape_changed = (data["availability_type"] != item["availability_type"]
                     or data["item_type"] != item["item_type"])
    if shape_changed and await db[RESERVATIONS].count_documents(
            {"business_id": business_id, "item_id": item_id, "status": {"$in": list(COMMITMENTS)}}):
        raise CatalogError("This item has open reservations or paid bookings. Release or fulfil them before "
                           "changing its type or how availability works.", 409, "has_commitments")
    now = utcnow()
    changes = {**data, "updated_at": now}
    if data["availability_config"] != item.get("availability_config") or data["availability_type"] == "manual":
        changes["availability_updated_at"] = now
    update: dict[str, Any] = {"$set": {**changes, **scope(business_id, data["sku"])}}
    if not data["sku"]:
        update["$unset"] = {"sku_scope": ""}
    try:
        await db[ITEMS].update_one({"_id": item_id, "business_id": business_id}, update)
    except DuplicateKeyError:
        raise CatalogError(f"Another item already uses SKU {data['sku']}.", 409, "duplicate_sku")
    await _resize_period_stock(db, business_id, item, data, actor, now)
    if data["low_stock_threshold"] != item.get("low_stock_threshold"):
        await db[STOCK].update_many({"business_id": business_id, "item_id": item_id},
                                    {"$set": {"low_stock_threshold": data["low_stock_threshold"]}})
    await events.emit(db, business_id, "catalog.item.updated", {"item_id": item_id})
    return await get_item(db, business_id, item_id)


async def _resize_period_stock(db, business_id: str, before: dict, data: dict, actor: str, now: datetime) -> None:
    """A raised or lowered cap (or production limit) applies to the period already running."""
    kind = data["availability_type"]
    field = {"capped": "cap", "made_to_order": "max_orders"}.get(kind)
    if not field or before.get("availability_type") != kind:
        return
    old, new = (before.get("availability_config") or {}).get(field), data["availability_config"].get(field)
    if old == new or new is None:
        return
    period = period_key(data["availability_config"]["period"], now)
    have = await stock.get_stock(db, business_id, before["_id"], None, period)
    if not have:
        return
    try:
        await stock.adjust(db, business_id, before["_id"], None, "adjusted", quantity_change=new - (old or 0),
                           reason="Limit changed", actor=actor, period=period)
    except CatalogError:
        raise CatalogError("The new limit is below what is already reserved this period.", 409, "limit_below_reserved")


async def set_manual_availability(db, business_id: str, item_id: str, available: bool, note: str, actor: str) -> dict:
    item = await get_item(db, business_id, item_id)
    if item["availability_type"] != "manual":
        raise CatalogError("This item doesn't use manual availability.")
    config = {**item["availability_config"], "available": bool(available), "note": note[:300]}
    now = utcnow()
    await db[ITEMS].update_one({"_id": item_id}, {"$set": {"availability_config": config,
                                                            "availability_updated_at": now, "updated_at": now}})
    await events.emit(db, business_id, "inventory.availability.changed",
                      {"item_id": item_id, "available": bool(available)})
    return await get_item(db, business_id, item_id)


async def record_external_sync(db, business_id: str, item_id: str, provider: str, available_quantity: Optional[int],
                               available: Optional[bool], actor: str) -> dict:
    """What a provider integration reports. Labelled external with the time it was last heard from,
    so manually kept availability is never passed off as live provider data."""
    item = await get_item(db, business_id, item_id)
    now = utcnow()
    changes: dict[str, Any] = {"availability_source": "external", "external_provider": provider[:80],
                               "last_synced_at": now, "availability_updated_at": now, "updated_at": now}
    if item["availability_type"] == "manual" and available is not None:
        changes["availability_config"] = {**item["availability_config"], "available": bool(available)}
    await db[ITEMS].update_one({"_id": item_id}, {"$set": changes})
    if item["availability_type"] == "quantity" and available_quantity is not None:
        await stock.set_on_hand(db, business_id, item_id, None, int(available_quantity) + (
            (await stock.get_stock(db, business_id, item_id, None) or {}).get("reserved", 0)),
            f"Synced from {provider}", actor)
    await events.emit(db, business_id, "inventory.availability.changed", {"item_id": item_id, "source": "external"})
    return await get_item(db, business_id, item_id)


async def archive_item(db, business_id: str, item_id: str, actor: str) -> dict:
    await get_item(db, business_id, item_id)
    await db[ITEMS].update_one({"_id": item_id, "business_id": business_id},
                               {"$set": {"status": "archived", "updated_at": utcnow()}})
    await events.emit(db, business_id, "catalog.item.updated", {"item_id": item_id, "status": "archived"})
    return await get_item(db, business_id, item_id)


# ── variants ─────────────────────────────────────────────────────────────────

async def list_variants(db, business_id: str, item_id: str, include_archived: bool = False) -> list[dict]:
    query: dict[str, Any] = {"business_id": business_id, "item_id": item_id}
    if not include_archived:
        query["status"] = {"$ne": "archived"}
    return await db[VARIANTS].find(query).sort("created_at", 1).to_list(500)


async def create_variant(db, business_id: str, item_id: str, raw: dict, actor: str) -> dict:
    item = await get_item(db, business_id, item_id)
    data = normalize_variant(raw)
    await _unique_sku(db, business_id, data["sku"], VARIANTS)
    doc = {"_id": uuid.uuid4().hex, "business_id": business_id, "item_id": item_id, **data,
           **scope(business_id, data["sku"]), "created_at": utcnow(), "updated_at": utcnow()}
    try:
        await db[VARIANTS].insert_one(doc)
    except DuplicateKeyError:
        raise CatalogError(f"Another variant already uses SKU {data['sku']}.", 409, "duplicate_sku")
    if item["availability_type"] == "quantity":
        # Once variants exist the item-level pool is no longer sold from.
        await stock.ensure_stock(db, business_id, item_id, doc["_id"], opening=_opening(raw), actor=actor,
                                 threshold=raw.get("low_stock_threshold", item.get("low_stock_threshold")))
    await events.emit(db, business_id, "catalog.item.updated", {"item_id": item_id, "variant_id": doc["_id"]})
    return doc


async def update_variant(db, business_id: str, item_id: str, variant_id: str, patch: dict, actor: str) -> dict:
    variant = await get_variant(db, business_id, item_id, variant_id)
    data = normalize_variant(patch, variant)
    await _unique_sku(db, business_id, data["sku"], VARIANTS, keep=variant_id)
    update: dict[str, Any] = {"$set": {**data, **scope(business_id, data["sku"]), "updated_at": utcnow()}}
    if not data["sku"]:
        update["$unset"] = {"sku_scope": ""}
    try:
        await db[VARIANTS].update_one({"_id": variant_id}, update)
    except DuplicateKeyError:
        raise CatalogError(f"Another variant already uses SKU {data['sku']}.", 409, "duplicate_sku")
    await events.emit(db, business_id, "catalog.item.updated", {"item_id": item_id, "variant_id": variant_id})
    return await get_variant(db, business_id, item_id, variant_id)


# ── reading ──────────────────────────────────────────────────────────────────

def _stock_row(row: Optional[dict]) -> dict:
    row = row or {}
    return {"on_hand": row.get("on_hand", 0), "reserved": row.get("reserved", 0), "available": row.get("available", 0),
            "low_stock_threshold": row.get("low_stock_threshold")}


async def detail(db, business_id: str, item_id: str, view: str = "staff") -> dict:
    item = await get_item(db, business_id, item_id)
    out = serialise(item, view)
    variants = await list_variants(db, business_id, item_id, include_archived=True)
    rows = await db[STOCK].find({"business_id": business_id, "item_id": item_id, "period_key": ""}).to_list(2000)
    by_variant = {r.get("variant_id"): r for r in rows}
    out["variants"] = [{**serialise(v, view), "stock": _stock_row(by_variant.get(v["_id"]))}
                       for v in variants]
    out["stock"] = _stock_row(by_variant.get(None))
    return out


async def list_items(db, business_id: str, query: str = "", status: str = "", item_type: str = "",
                     category: str = "", limit: int = 50, skip: int = 0, view: str = "staff") -> dict:
    flt: dict[str, Any] = {"business_id": business_id}
    flt["status"] = status if status else {"$ne": "archived"}
    if item_type:
        flt["item_type"] = item_type
    if category:
        flt["category"] = category
    if query.strip():
        rx = {"$regex": re.escape(query.strip()), "$options": "i"}
        flt["$or"] = [{"name": rx}, {"description": rx}, {"sku": rx}, {"category": rx}]
    total = await db[ITEMS].count_documents(flt)
    rows = await db[ITEMS].find(flt).sort("updated_at", -1).skip(max(0, skip)).limit(max(1, min(limit, 200))).to_list(200)
    ids = [r["_id"] for r in rows]
    stock_rows = await db[STOCK].find({"business_id": business_id, "item_id": {"$in": ids}, "period_key": ""}).to_list(5000)
    variants = await db[VARIANTS].find({"business_id": business_id, "item_id": {"$in": ids},
                                        "status": {"$ne": "archived"}}).to_list(5000)
    count_by_item: dict[str, int] = {}
    for v in variants:
        count_by_item[v["item_id"]] = count_by_item.get(v["item_id"], 0) + 1
    items = []
    for row in rows:
        mine = [s for s in stock_rows if s["item_id"] == row["_id"]]
        entry = serialise(row, view)
        entry["variant_count"] = count_by_item.get(row["_id"], 0)
        if row["availability_type"] == "quantity":
            entry["available_quantity"] = sum(s["available"] for s in mine)
            entry["on_hand"] = sum(s["on_hand"] for s in mine)
            entry["low_stock"] = any(
                (s.get("low_stock_threshold") is not None and s["available"] <= s["low_stock_threshold"]) for s in mine)
        items.append(entry)
    categories = sorted({c for c in await db[ITEMS].distinct("category", {"business_id": business_id}) if c})
    return {"items": items, "total": total, "categories": categories}
