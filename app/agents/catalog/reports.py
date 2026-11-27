"""What sold, what's left, what people wanted that wasn't available, and how reservations turned out."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .models import (
    CANCELLED, CONFIRMED, DECLINED, DEMAND, EXPIRED, FULFILLED, ITEMS, RELEASED, RESERVATIONS, SLOTS, STOCK, utcnow,
)

CAVEAT = "Enquiries show interest, not sales. They are a signal of possible missed demand, not lost revenue."


async def summary(db, business_id: str, days: int = 30, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    since = now - timedelta(days=max(1, min(days, 365)))
    items = {i["_id"]: i for i in await db[ITEMS].find({"business_id": business_id}).to_list(10000)}
    name = lambda iid: (items.get(iid) or {}).get("name", "Removed item")   # noqa: E731

    reservations = await db[RESERVATIONS].find({"business_id": business_id, "created_at": {"$gte": since}}).to_list(50000)
    sold: dict[str, int] = {}
    requested: dict[str, int] = {}
    outcome = {"converted": 0, "expired": 0, "released": 0, "open": 0}
    for r in reservations:
        requested[r["item_id"]] = requested.get(r["item_id"], 0) + 1
        if r["status"] in (CONFIRMED, FULFILLED):
            sold[r["item_id"]] = sold.get(r["item_id"], 0) + r["quantity"]
            outcome["converted"] += 1
        elif r["status"] == EXPIRED:
            outcome["expired"] += 1
        elif r["status"] in (RELEASED, CANCELLED, DECLINED):
            outcome["released"] += 1
        else:
            outcome["open"] += 1
    stock_rows = await db[STOCK].find({"business_id": business_id, "period_key": ""}).to_list(50000)
    remaining: dict[str, int] = {}
    low: list[dict] = []
    with_options = {s["item_id"] for s in stock_rows if s.get("variant_id")}
    for s in stock_rows:
        remaining[s["item_id"]] = remaining.get(s["item_id"], 0) + s["available"]
        if not s.get("variant_id") and s["item_id"] in with_options:
            continue
        if s.get("low_stock_threshold") is not None and s["available"] <= s["low_stock_threshold"]:
            low.append({"item_id": s["item_id"], "name": name(s["item_id"]), "available": s["available"]})

    demand = await db[DEMAND].find({"business_id": business_id, "created_at": {"$gte": since}}).to_list(50000)
    missed: dict[str, dict] = {}
    for d in demand:
        row = missed.setdefault(d["item_id"], {"item_id": d["item_id"], "name": name(d["item_id"]), "enquiries": 0,
                                               "last_at": d["created_at"].isoformat() + "Z"})
        row["enquiries"] += 1
    for d in demand:
        requested[d["item_id"]] = requested.get(d["item_id"], 0) + 1

    slots = await db[SLOTS].find({"business_id": business_id, "start_at": {"$gte": since, "$lte": now}}).to_list(50000)
    capacity = sum(s["total"] for s in slots)
    used = sum(s["confirmed"] + s["reserved"] for s in slots)

    closed = outcome["converted"] + outcome["expired"]
    top = lambda d: sorted(d.items(), key=lambda kv: -kv[1])[:10]   # noqa: E731
    return {
        "days": days,
        "items": {"total": len(items), "active": sum(1 for i in items.values() if i["status"] == "active")},
        "sold": [{"item_id": k, "name": name(k), "units": v, "remaining": remaining.get(k)} for k, v in top(sold)],
        "low_stock": sorted(low, key=lambda x: x["available"])[:20],
        "most_requested": [{"item_id": k, "name": name(k), "requests": v} for k, v in top(requested)],
        "most_unavailable": sorted(missed.values(), key=lambda x: -x["enquiries"])[:10],
        "reservations": {**outcome, "total": len(reservations),
                         "conversion_rate": round(outcome["converted"] / closed, 3) if closed else None},
        "utilisation": {"capacity": capacity, "booked": used, "rate": round(used / capacity, 3) if capacity else None},
        "demand_without_availability": sorted(missed.values(), key=lambda x: -x["enquiries"])[:20],
        "note": CAVEAT,
    }
