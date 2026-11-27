"""What needs the owner's attention: low and empty stock, thin capacity, holds about to lapse, stale availability."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from .availability import _stale
from .models import ACTIVE_RESERVATION, ALERT_LOG, ITEMS, RESERVATIONS, SLOTS, STOCK, VARIANTS, utcnow

EXPIRING_SOON = timedelta(minutes=15)
UNUSUAL_MIN = 50


def _alert(kind: str, severity: str, item: dict, title: str, message: str, key: str, variant_id: Optional[str] = None) -> dict:
    return {"key": key, "type": kind, "severity": severity, "item_id": item["_id"], "variant_id": variant_id,
            "item_name": item["name"], "title": title, "message": message}


async def compute_alerts(db, business_id: str, now: Optional[datetime] = None) -> list[dict]:
    now = now or utcnow()
    items = await db[ITEMS].find({"business_id": business_id, "status": "active"}).to_list(5000)
    by_id = {i["_id"]: i for i in items}
    out: list[dict] = []
    variants = await db[VARIANTS].find({"business_id": business_id}).to_list(10000)
    names = {v["_id"]: v["name"] for v in variants}
    has_options = {v["item_id"] for v in variants if v.get("status", "active") == "active"}
    for row in await db[STOCK].find({"business_id": business_id, "period_key": ""}).to_list(20000):
        item = by_id.get(row["item_id"])
        if not item or item["availability_type"] != "quantity":
            continue
        if not row.get("variant_id") and row["item_id"] in has_options:
            continue                            # an item sold by option has no stock of its own to run out
        label = item["name"] + (f" ({names[row['variant_id']]})" if row.get("variant_id") in names else "")
        if row["available"] <= 0:
            out.append(_alert("out_of_stock", "high", item, f"{label} is out of stock",
                              "It can't be sold until you add stock.", f"oos:{row['_id']}", row.get("variant_id")))
        elif row.get("low_stock_threshold") is not None and row["available"] <= row["low_stock_threshold"]:
            out.append(_alert("low_stock", "medium", item, f"{label} is running low",
                              f"{row['available']} left (alert at {row['low_stock_threshold']}).",
                              f"low:{row['_id']}", row.get("variant_id")))
    for item in items:
        threshold = (item.get("availability_config") or {}).get("low_capacity_threshold")
        if item["availability_type"] == "capacity" and threshold is not None:
            for slot in await db[SLOTS].find({"business_id": business_id, "item_id": item["_id"], "status": "open",
                                              "start_at": {"$gte": now}}).to_list(500):
                if 0 < slot["remaining"] <= threshold:
                    when = slot["start_at"].strftime("%d %b %Y")
                    out.append(_alert("capacity_low", "medium", item, f"{item['name']}: {slot['remaining']} left on {when}",
                                      f"Capacity is running low for {slot.get('label') or when}.", f"cap:{slot['_id']}"))
        if _stale(item, now):
            out.append(_alert("stale_availability", "medium", item, f"{item['name']}: availability is out of date",
                              "Update or re-confirm it so customers aren't told something old.",
                              f"stale:{item['_id']}:{now:%Y-%m-%d}"))
    for res in await db[RESERVATIONS].find({"business_id": business_id, "status": {"$in": list(ACTIVE_RESERVATION)},
                                            "expires_at": {"$gt": now, "$lte": now + EXPIRING_SOON}}).to_list(500):
        item = by_id.get(res["item_id"])
        if item:
            out.append(_alert("reservation_expiring", "low", item, f"A hold on {item['name']} is about to expire",
                              f"{res['quantity']} reserved; it lapses at {res['expires_at']:%H:%M} UTC.",
                              f"exp:{res['_id']}"))
    return out


def is_unusual(on_hand_before: int, change: int) -> bool:
    return abs(change) >= max(UNUSUAL_MIN, int(on_hand_before * 0.5))


Notifier = Callable[[Any, str, Optional[str], str, str, str], Awaitable[None]]


async def _default_notify(db, user_id: str, brand_id: Optional[str], event: str, title: str, message: str) -> None:
    from app.services.brand_notifier import notify_in_app
    await notify_in_app(db, user_id, brand_id, event, title, message, "/workspace")


async def _send_once(db, business_id: str, user_id: str, key: str, title: str, message: str,
                     notifier: Optional[Notifier], now: datetime) -> bool:
    """Tell the owner about this alert once per day."""
    if not user_id:
        return False
    log_key = f"{business_id}:{key}:{now:%Y-%m-%d}"
    if await db[ALERT_LOG].find_one({"_id": log_key}):
        return False
    try:
        await db[ALERT_LOG].insert_one({"_id": log_key, "business_id": business_id, "at": now})
    except Exception:
        return False
    try:
        await (notifier or _default_notify)(db, user_id, business_id, "catalog_alert", title, message)
    except Exception as e:                      # an alert must never break the job
        print(f"[Catalog] alert not sent: {e}", flush=True)
        return False
    return True


async def send_alerts(db, notifier: Optional[Notifier] = None, now: Optional[datetime] = None) -> int:
    now = now or utcnow()
    sent = 0
    for business_id in await db[ITEMS].distinct("business_id", {"status": "active"}):
        owner_of = {i["_id"]: i.get("created_by", "") for i in
                    await db[ITEMS].find({"business_id": business_id}, {"created_by": 1}).to_list(5000)}
        for alert in await compute_alerts(db, business_id, now):
            if alert["severity"] == "low":
                continue                            # shown in the dashboard, not worth a notification
            if await _send_once(db, business_id, owner_of.get(alert["item_id"], ""), alert["key"], alert["title"],
                                alert["message"], notifier, now):
                sent += 1
    return sent


async def notify_unusual(db, business_id: str, item: dict, on_hand_before: int, change: int, actor: str,
                         notifier: Optional[Notifier] = None) -> bool:
    if not is_unusual(on_hand_before, change):
        return False
    verb = "added" if change > 0 else "removed"
    return await _send_once(db, business_id, item.get("created_by", ""), f"unusual:{item['_id']}:{actor}:{change}:{utcnow():%H%M}",
                            f"Large stock change on {item['name']}",
                            f"{abs(change)} units {verb} (was {on_hand_before}). If that wasn't you, check the history.",
                            notifier, utcnow())
