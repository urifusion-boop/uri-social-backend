"""Returns: requested, approved or rejected, received, then resolved with a disposition.
Goods only go back on sale when the business says they are resellable; a refund is a separate request."""
from __future__ import annotations

import uuid
from typing import Any

from app.agents.catalog import reservations, stock
from app.agents.catalog.models import utcnow

from . import service
from .models import FULFILLED, ORDERS, RETURNED, OrderError, positive_int

RETURNS = "order_returns"
DISPOSITIONS = ("restock", "damaged", "write_off")


async def get_return(db, business_id: str, return_id: str) -> dict:
    doc = await db[RETURNS].find_one({"_id": return_id, "business_id": business_id})
    if not doc:
        raise OrderError("Return not found.", 404)
    return doc


async def list_returns(db, business_id: str, order_id: str = "") -> list[dict]:
    flt: dict[str, Any] = {"business_id": business_id, **({"order_id": order_id} if order_id else {})}
    return await db[RETURNS].find(flt).sort("created_at", -1).to_list(200)


async def request_return(db, business_id: str, order_id: str, line_id: str, quantity: int, reason: str, actor: str) -> dict:
    order = await service.get_order(db, business_id, order_id)
    line = next((x for x in order["lines"] if x["id"] == line_id), None)
    if not line:
        raise OrderError("That line isn't on this order.", 404)
    if line["fulfilment_status"] not in (FULFILLED, RETURNED):
        raise OrderError("Only something that was delivered can be returned.", 409, "not_fulfilled")
    quantity = positive_int(quantity, "Quantity")
    taken = sum(r["quantity"] for r in await db[RETURNS].find(
        {"order_id": order_id, "line_id": line_id, "status": {"$in": ["requested", "approved", "received", "resolved"]}}).to_list(100))
    if quantity + taken > line["quantity"]:
        raise OrderError("That's more than was ordered.", 409, "over_return")
    doc = {"_id": uuid.uuid4().hex, "business_id": business_id, "order_id": order_id, "line_id": line_id, "quantity": quantity,
           "reason": (reason or "").strip()[:300], "status": "requested", "requested_by": actor, "created_at": utcnow()}
    await db[RETURNS].insert_one(doc)
    await service._log(db, order, "return_status_changed", actor, {"status": "requested", "return_id": doc["_id"]})
    return doc


async def _move(db, ret: dict, frm: str, to: str, actor: str, extra: dict) -> dict:
    won = await db[RETURNS].find_one_and_update({"_id": ret["_id"], "status": frm}, {"$set": {"status": to, "updated_at": utcnow(), **extra}},
                                                return_document=True)
    if not won:
        raise OrderError(f"This return is {ret['status']}.", 409)
    order = await service.get_order(db, ret["business_id"], ret["order_id"])
    await service._log(db, order, "return_status_changed", actor, {"status": to, "return_id": ret["_id"]})
    return won


async def decide(db, business_id: str, return_id: str, approve: bool, note: str, actor: str) -> dict:
    ret = await get_return(db, business_id, return_id)
    return await _move(db, ret, "requested", "approved" if approve else "rejected", actor, {"decision_note": (note or "")[:300]})


async def receive(db, business_id: str, return_id: str, actor: str) -> dict:
    return await _move(db, await get_return(db, business_id, return_id), "approved", "received", actor, {"received_at": utcnow()})


async def resolve(db, business_id: str, return_id: str, disposition: str, actor: str) -> dict:
    if disposition not in DISPOSITIONS:
        raise OrderError(f"Disposition must be one of: {', '.join(DISPOSITIONS)}.")
    ret = await get_return(db, business_id, return_id)
    order = await service.get_order(db, business_id, ret["order_id"])
    line = next(x for x in order["lines"] if x["id"] == ret["line_id"])
    done = await _move(db, ret, "received", "resolved", actor, {"disposition": disposition})
    if line.get("reservation_id"):
        hold = (await reservations.get_reservation(db, business_id, line["reservation_id"])).get("hold") or {}
        if hold.get("kind") == "stock":
            item, variant, period = line["catalog_item_id"], hold.get("variant_id"), hold.get("period_key", "")
            if disposition == "restock":
                row = await stock.restock(db, business_id, item, variant, ret["quantity"], period)
                await stock.record_movement(db, business_id, item, variant, "returned", ret["quantity"], 0,
                                            "Return inspected and resellable", ret["_id"], actor, (row or {}).get("on_hand"), period)
            else:
                await stock.record_movement(db, business_id, item, variant, "damaged" if disposition == "damaged" else "lost", 0, 0,
                                            f"Returned, not resellable ({disposition.replace('_', ' ')})", ret["_id"], actor, period=period)
    returned = sum(r["quantity"] for r in await db[RETURNS].find({"order_id": order["_id"], "line_id": line["id"], "status": "resolved"}).to_list(100))
    if returned >= line["quantity"]:
        line["fulfilment_status"] = RETURNED
        await db[ORDERS].update_one({"_id": order["_id"]}, {"$set": {"lines": order["lines"], "updated_at": utcnow()}})
    return done
