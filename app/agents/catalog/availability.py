"""Can this be sold, how many, and how sure are we? One handler per availability model.

The answer always says where it came from and how fresh it is. Manually kept availability
is never presented as live provider data, and stale data falls back the way the business
configured: ask for confirmation, treat it as unavailable, or trust it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from . import schedule, stock
from .items import effective_price, get_item, list_variants
from .models import CatalogError, iso, period_key, to_db, utcnow

DEFAULT_STALE_HOURS = 24
UPCOMING = 12


def _base(item: dict, variant_id: Optional[str], quantity: int, now: datetime) -> dict:
    return {"item_id": item["_id"], "variant_id": variant_id, "quantity": quantity,
            "availability_type": item["availability_type"], "available": False, "available_quantity": None,
            "requires_confirmation": bool(item.get("requires_confirmation")), "source": item.get("availability_source", "uri"),
            "last_updated": iso(item.get("availability_updated_at")), "stale": False, "reason": "",
            "checked_at": iso(now)}


def _stale(item: dict, now: datetime) -> bool:
    """Only data a person typed in or a provider reported can go stale; URI's own counters are live."""
    external = item.get("availability_source") == "external"
    if not (external or item["availability_type"] == "manual"):
        return False
    hours = (item.get("availability_config") or {}).get("stale_after_hours", DEFAULT_STALE_HOURS)
    seen = item.get("last_synced_at") if external else item.get("availability_updated_at")
    return not seen or now - seen > timedelta(hours=hours)


def _apply_stale(out: dict, item: dict, now: datetime) -> dict:
    if not _stale(item, now):
        return out
    out["stale"] = True
    fallback = (item.get("availability_config") or {}).get("stale_fallback", "require_confirmation")
    if fallback == "assume_unavailable":
        out.update(available=False, reason="stale_availability")
    elif fallback == "require_confirmation":
        out["requires_confirmation"] = True
    return out


async def _stock_availability(db, item: dict, variant_id: Optional[str], quantity: int, period: str = "",
                              default: Optional[int] = None) -> dict:
    row = await stock.get_stock(db, item["business_id"], item["_id"], variant_id, period)
    have = row["available"] if row else (default or 0)
    out = {"available_quantity": have, "available": have >= quantity}
    threshold = (row or {}).get("low_stock_threshold")
    if threshold is not None and have <= threshold:
        out["low_stock"] = True
    if not out["available"]:
        out["reason"] = "out_of_stock" if have == 0 else "not_enough_stock"
    return out


async def _quantity(db, item, variant_id, quantity, now) -> dict:
    variants = await list_variants(db, item["business_id"], item["_id"])
    active = [v for v in variants if v["status"] == "active"]
    if active and not variant_id:
        rows = []
        for variant in active:
            part = await _stock_availability(db, item, variant["_id"], quantity)
            rows.append({"variant_id": variant["_id"], "name": variant["name"], "attributes": variant["attributes"],
                         "available_quantity": part["available_quantity"], "available": part["available"],
                         "price": effective_price(item, variant)[0]})
        return {"variant_required": True, "variants": rows, "available": any(r["available"] for r in rows),
                "available_quantity": sum(r["available_quantity"] for r in rows),
                "reason": "" if any(r["available"] for r in rows) else "out_of_stock"}
    if variant_id and variant_id not in {v["_id"] for v in active}:
        return {"reason": "unknown_variant"}
    if not variant_id and variants and not active:
        return {"reason": "out_of_stock"}
    return await _stock_availability(db, item, variant_id, quantity)


async def _capped(db, item, variant_id, quantity, now) -> dict:
    cfg = item["availability_config"]
    return await _stock_availability(db, item, None, quantity, period_key(cfg["period"], now), default=cfg["cap"])


async def _made_to_order(db, item, variant_id, quantity, now) -> dict:
    cfg = item["availability_config"]
    out: dict[str, Any] = {"deposit_percent": cfg.get("deposit_percent")}
    if cfg.get("lead_time_days") is not None:
        out["lead_time_days"] = cfg["lead_time_days"]
        out["estimated_ready"] = iso(now + timedelta(days=cfg["lead_time_days"]))
    if cfg.get("max_orders") is None:
        out.update(available=True, available_quantity=None)
        return out
    out.update(await _stock_availability(db, item, None, quantity, period_key(cfg["period"], now), default=cfg["max_orders"]))
    if not out["available"]:
        out["reason"] = "production_full"
    return out


async def _slot_rows(db, item, quantity, now, first: Optional[datetime] = None, limit: int = UPCOMING) -> list[dict]:
    if item["availability_type"] == "schedule":
        start = first or now
        await schedule.ensure_slots(db, item, schedule.local_day(item, start),
                                    schedule.local_day(item, start) + timedelta(days=13), now)
    slots = await stock.list_slots(db, item["business_id"], item["_id"], first or now, None, 400)
    rows = [s for s in slots if schedule.bookable(item, s, quantity, now)]
    return [{"slot_id": s["_id"], "start_at": iso(s["start_at"]), "end_at": iso(s["end_at"]), "label": s.get("label", ""),
             "remaining": s["remaining"], "total": s["total"]} for s in rows[:limit]]


async def _slotted(db, item, variant_id, quantity, start_at, now) -> dict:
    if start_at is None:
        rows = await _slot_rows(db, item, quantity, now)
        return {"available": bool(rows), "slots": rows, "reason": "" if rows else "no_open_slots",
                "needs_time": True}
    if item["availability_type"] == "schedule":
        await schedule.ensure_slots(db, item, schedule.local_day(item, start_at), schedule.local_day(item, start_at), now)
    slot = await stock.find_slot(db, item["business_id"], item["_id"], start_at)
    if not slot:
        return {"reason": "no_such_slot"}
    ok = schedule.bookable(item, slot, quantity, now)
    out = {"available": ok, "available_quantity": slot["remaining"], "slot_id": slot["_id"],
           "start_at": iso(slot["start_at"]), "end_at": iso(slot["end_at"])}
    if not ok:
        out["reason"] = "slot_full" if slot["remaining"] < quantity and slot["status"] == "open" else "slot_closed"
    return out


async def check(db, business_id: str, item_id: str, variant_id: Optional[str] = None, quantity: int = 1,
                start_at: Any = None, end_at: Any = None, now: Optional[datetime] = None) -> dict:
    """The availability of an item (and variant, date or slot) right now."""
    now = now or utcnow()
    if not isinstance(quantity, int) or quantity < 1:
        raise CatalogError("Quantity must be at least 1.")
    item = await get_item(db, business_id, item_id)
    out = _base(item, variant_id, quantity, now)
    if item["status"] != "active":
        out["reason"] = "not_for_sale"
        return _finish(out, item)
    kind, start = item["availability_type"], to_db(start_at)
    if kind == "quantity":
        out.update(await _quantity(db, item, variant_id, quantity, now))
    elif kind == "capped":
        out.update(await _capped(db, item, variant_id, quantity, now))
    elif kind == "unlimited":
        out.update(available=True)
    elif kind in ("capacity", "schedule"):
        out.update(await _slotted(db, item, variant_id, quantity, start, now))
    elif kind == "made_to_order":
        out.update(await _made_to_order(db, item, variant_id, quantity, now))
    elif kind == "manual":
        out.update(available=bool(item["availability_config"].get("available", True)),
                   note=item["availability_config"].get("note", ""), requires_confirmation=True)
        if not out["available"]:
            out["reason"] = "marked_unavailable"
    _apply_stale(out, item, now)
    return _finish(out, item)


def _finish(out: dict, item: dict) -> dict:
    out["message"] = describe(out)
    return out


def describe(out: dict) -> str:
    """One plain sentence for the dashboard and for the AI to quote."""
    qty, kind = out.get("available_quantity"), out.get("availability_type")
    if out.get("reason") == "not_for_sale":
        return "Not currently for sale."
    if out.get("stale") and out.get("reason") == "stale_availability":
        return "Availability was last updated a while ago, so it can't be promised. Ask the team."
    if not out["available"]:
        return {"out_of_stock": "Out of stock.", "not_enough_stock": f"Only {qty} available.",
                "production_full": "Not taking new orders right now.", "slot_full": "That time is fully booked.",
                "slot_closed": "That time isn't bookable.", "no_such_slot": "There is no session at that time.",
                "no_open_slots": "No open times right now.", "marked_unavailable": "Not available right now.",
                "unknown_variant": "That option doesn't exist."}.get(out.get("reason", ""), "Not available.")
    if out.get("requires_confirmation"):
        return ("Available, but the team must confirm it first"
                + (" (availability may be out of date)." if out.get("stale") else "."))
    if kind in ("unlimited",) or qty is None:
        return "Available."
    return f"{qty} available." if qty != 1 else "1 available."
