"""Order workflow. Prices come from the catalog, never the caller; reservations are all-or-none;
payment is only ever recorded from a verified source; the three status dimensions move independently."""
from __future__ import annotations

import secrets
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.agents.catalog import events, reservations
from app.agents.catalog.items import effective_price, get_item, list_variants
from app.agents.catalog.models import CatalogError, utcnow

from . import notify
from .models import (
    AUDIT, AWAITING_CUSTOMER, CANCELLED, COMPLETED, CONFIRMED, COUNTERS, DEFAULT_PAYMENT_WINDOW_HOURS, DELIVERY_FAILED, DRAFT, EDITABLE,
    EXPIRED, FAILED, FULFILLED, IN_PROGRESS, LINE_FULFILMENT, MAX_LINES, NOT_STARTED, ORDERS, OPEN, PARTIALLY_FULFILLED,
    PARTIALLY_PAID, PARTIALLY_REFUNDED, PAID, PAYMENT_SOURCES, PAYMENTS, PENDING, PROCESSING, QUEUED, REFUNDED, REFUNDS, SOURCES, UNPAID, OrderError,
    positive_int, to_minor,
)


# ── audit and events ─────────────────────────────────────────────────────────

_transport = None      # tests swap in a fake chat transport

async def _log(db, order: dict, kind: str, actor: str, payload: Optional[dict] = None, dedupe: str = "") -> None:
    await db[AUDIT].insert_one({"_id": uuid.uuid4().hex, "business_id": order["business_id"], "order_id": order["_id"],
                                "type": kind, "actor": actor, "payload": payload or {}, "created_at": utcnow()})
    await events.emit(db, order["business_id"], f"order.{kind}", {"order_id": order["_id"], **(payload or {})},
                      dedupe_key=dedupe or f"{kind}:{order['_id']}:{uuid.uuid4().hex[:8]}")
    if order["_id"] != "export":
        try:
            await notify.maybe(db, order["_id"], kind, actor, payload or {}, _transport)
        except Exception as e:
            print(f"[Orders] notification for {kind} failed: {e}", flush=True)


async def get_order(db, business_id: str, order_id: str) -> dict:
    doc = await db[ORDERS].find_one({"_id": order_id, "business_id": business_id})
    if not doc:
        raise OrderError("Order not found.", 404)
    return doc


async def get_by_token(db, token: str) -> dict:
    doc = await db[ORDERS].find_one({"public_token": token}) if token else None
    if not doc:
        raise OrderError("Order not found.", 404)
    return doc


async def timeline(db, business_id: str, order_id: str) -> list[dict]:
    await get_order(db, business_id, order_id)
    return await db[AUDIT].find({"business_id": business_id, "order_id": order_id}).sort("created_at", 1).to_list(500)


# ── totals ───────────────────────────────────────────────────────────────────

def compute_totals(lines: list[dict], fees: int = 0, discount: int = 0, tax: int = 0) -> dict:
    subtotal = sum(line["line_total"] for line in lines)
    discount = min(discount, subtotal)
    return {"subtotal": subtotal, "discount_total": discount, "fees_total": fees, "tax_total": tax,
            "total": subtotal - discount + fees + tax}


def _money(order: dict) -> dict:
    return compute_totals(order["lines"], order.get("fees_total", 0), order.get("discount_total", 0), order.get("tax_total", 0))


async def _snapshot_line(db, business_id: str, raw: dict, now: datetime) -> dict:
    qty = positive_int(raw.get("quantity", 1), "Quantity")
    try:
        item = await get_item(db, business_id, str(raw.get("catalog_item_id") or raw.get("item_id") or ""))
    except CatalogError as e:
        raise OrderError("One of those items isn't in the catalog.", 404, "item_not_found") from e
    if item["status"] != "active":
        raise OrderError(f"{item['name']} isn't for sale right now.", 409, "not_for_sale")
    variant = None
    if raw.get("variant_id"):
        variant = next((v for v in await list_variants(db, business_id, item["_id"]) if v["_id"] == raw["variant_id"]), None)
        if not variant:
            raise OrderError("That option doesn't exist.", 404, "unknown_variant")
    price, currency = effective_price(item, variant)
    unit = to_minor(price)
    return {"id": uuid.uuid4().hex[:12], "catalog_item_id": item["_id"], "variant_id": raw.get("variant_id"),
            "item_type": item["item_type"], "quantity": qty, "currency": currency, "unit_amount": unit,
            "line_total": (unit or 0) * qty, "needs_quote": unit is None,
            "start_at": raw.get("start_at"), "end_at": raw.get("end_at"),
            "fulfilment_status": NOT_STARTED, "reservation_id": None,
            "snapshot": {"name": item["name"], "description": item.get("description", ""), "sku": item.get("sku", ""),
                         "variant": variant["name"] if variant else "", "options": (variant or {}).get("attributes", {}),
                         "billing_interval": item.get("billing_interval", "one_time"), "taken_at": now}}


# ── creation and drafts ──────────────────────────────────────────────────────

async def _next_number(db, business_id: str) -> tuple[int, str]:
    row = await db[COUNTERS].find_one_and_update({"_id": business_id}, {"$inc": {"seq": 1}}, upsert=True,
                                                 return_document=ReturnDocument.AFTER)
    return row["seq"], f"ORD-{row['seq']:05d}"


def _customer(raw: Optional[dict]) -> dict:
    raw = raw or {}
    return {k: str(raw.get(k) or "").strip()[:120] for k in ("name", "phone", "email", "handle", "channel") if raw.get(k)}


def _delivery(raw: Optional[dict]) -> dict:
    raw = raw or {}
    out = {k: str(raw.get(k) or "").strip()[:300] for k in ("method", "recipient", "phone", "address", "instructions") if raw.get(k)}
    return out


def _fee(raw: Optional[dict]) -> int:
    fee = (raw or {}).get("fee")
    if fee in (None, ""):
        return 0
    if not isinstance(fee, (int, float)) or isinstance(fee, bool) or fee < 0:
        raise OrderError("The delivery fee can't be negative.")
    return to_minor(fee)


async def create_order(db, business_id: str, body: dict, actor: str, idempotency_key: str = "",
                       now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    source = body.get("source") or "dashboard"
    if source not in SOURCES:
        raise OrderError(f"Source must be one of: {', '.join(SOURCES)}.")
    raw_lines = body.get("items") or []
    if not raw_lines or len(raw_lines) > MAX_LINES:
        raise OrderError(f"Add between 1 and {MAX_LINES} items to the order.")
    key = (idempotency_key or "").strip()[:120]
    if key:
        again = await db[ORDERS].find_one({"business_id": business_id, "idempotency_key": key})
        if again:
            return {**again, "idempotent_replay": True}
    lines = [await _snapshot_line(db, business_id, raw, now) for raw in raw_lines]
    currencies = {line["currency"] for line in lines}
    if len(currencies) > 1:
        raise OrderError("One order can't mix currencies.", 409, "mixed_currency")
    delivery = _delivery(body.get("delivery"))
    seq, number = await _next_number(db, business_id)
    doc = {"_id": uuid.uuid4().hex, "business_id": business_id, "order_number": number, "number_scope": f"{business_id}:{seq}",
           "public_token": secrets.token_urlsafe(24), "source": source, "source_ref": str(body.get("source_ref") or "")[:200],
           "customer": _customer(body.get("customer")), "delivery": delivery, "currency": currencies.pop(), "lines": lines,
           "fees_total": _fee(body.get("delivery")), "discount_total": 0, "tax_total": 0, "discounts": [],
           "status": DRAFT, "payment_status": UNPAID, "fulfilment_status": NOT_STARTED, "paid_amount": 0,
           "refunded_amount": 0, "assignee": "", "tags": [], "internal_notes": str(body.get("internal_notes") or "")[:2000],
           "created_by": actor, "created_at": now, "updated_at": now}
    doc.update(_money(doc))
    doc["due_amount"] = doc["total"]
    if key:
        doc["idempotency_key"], doc["idem_scope"] = key, f"{business_id}:{key}"
    try:
        await db[ORDERS].insert_one(doc)
    except DuplicateKeyError:
        return {**(await db[ORDERS].find_one({"business_id": business_id, "idempotency_key": key})), "idempotent_replay": True}
    await _log(db, doc, "created", actor, {"source": source, "source_ref": doc["source_ref"]}, f"created:{doc['_id']}")
    return doc


async def update_draft(db, business_id: str, order_id: str, body: dict, actor: str) -> dict:
    order = await get_order(db, business_id, order_id)
    if order["status"] not in EDITABLE:
        raise OrderError("A confirmed order can't be edited directly. Use an adjustment.", 409, "not_editable")
    changes: dict[str, Any] = {}
    if "customer" in body:
        changes["customer"] = _customer(body["customer"])
    if "delivery" in body:
        changes["delivery"], changes["fees_total"] = _delivery(body["delivery"]), _fee(body["delivery"])
    if "internal_notes" in body:
        changes["internal_notes"] = str(body["internal_notes"] or "")[:2000]
    if "items" in body:
        raw_lines = body["items"] or []
        if not raw_lines or len(raw_lines) > MAX_LINES:
            raise OrderError(f"Add between 1 and {MAX_LINES} items to the order.")
        changes["lines"] = [await _snapshot_line(db, business_id, raw, utcnow()) for raw in raw_lines]
    merged = {**order, **changes}
    changes.update(_money(merged))
    changes["due_amount"] = changes["total"] - order["paid_amount"]
    changes["updated_at"] = utcnow()
    won = await db[ORDERS].find_one_and_update({"_id": order_id, "status": {"$in": list(EDITABLE)}}, {"$set": changes},
                                               return_document=ReturnDocument.AFTER)
    if not won:
        raise OrderError("A confirmed order can't be edited directly. Use an adjustment.", 409, "not_editable")
    await _log(db, won, "updated", actor, {"fields": sorted(k for k in changes if k not in ("updated_at",))})
    return won


async def set_quote_price(db, business_id: str, order_id: str, line_id: str, unit_amount: float, actor: str) -> dict:
    """A quote-based line has no price until the business approves one."""
    order = await get_order(db, business_id, order_id)
    if order["status"] not in EDITABLE:
        raise OrderError("A confirmed order can't be re-quoted.", 409, "not_editable")
    if not isinstance(unit_amount, (int, float)) or isinstance(unit_amount, bool) or unit_amount < 0:
        raise OrderError("Enter a price of zero or more.")
    lines = order["lines"]
    line = next((x for x in lines if x["id"] == line_id), None)
    if not line:
        raise OrderError("That line isn't on this order.", 404)
    line.update(unit_amount=to_minor(unit_amount), needs_quote=False)
    line["line_total"] = line["unit_amount"] * line["quantity"]
    totals = compute_totals(lines, order["fees_total"], order["discount_total"], order["tax_total"])
    await db[ORDERS].update_one({"_id": order_id}, {"$set": {"lines": lines, **totals, "due_amount": totals["total"] - order["paid_amount"],
                                                             "updated_at": utcnow()}})
    await _log(db, order, "quote_priced", actor, {"line_id": line_id, "unit_amount": line["unit_amount"]})
    return await get_order(db, business_id, order_id)


async def send_quote(db, business_id: str, order_id: str, actor: str) -> dict:
    order = await get_order(db, business_id, order_id)
    if any(line["needs_quote"] for line in order["lines"]):
        raise OrderError("Set the quoted price before sending this to the customer.", 409, "quote_needed")
    won = await db[ORDERS].find_one_and_update({"_id": order_id, "status": {"$in": list(EDITABLE)}},
                                               {"$set": {"status": AWAITING_CUSTOMER, "updated_at": utcnow()}},
                                               return_document=ReturnDocument.AFTER)
    if not won:
        raise OrderError(f"This order is {order['status']}.", 409)
    await _log(db, won, "awaiting_customer", actor, {"total": won["total"]}, f"awaiting:{order_id}")
    return won


# ── confirmation: validate, reserve everything or nothing ────────────────────

async def _reserve_all(db, order: dict, actor: str) -> list[dict]:
    made: list[dict] = []
    try:
        for index, line in enumerate(order["lines"]):
            made.append(await reservations.reserve(
                db, order["business_id"], line["catalog_item_id"], line["quantity"], line.get("variant_id"),
                order_id=order["_id"], start_at=line.get("start_at"), end_at=line.get("end_at"), actor=actor,
                idempotency_key=f"order:{order['_id']}:{index}", source="order", customer=order.get("customer")))
    except CatalogError as e:
        for res in made:
            await reservations.release(db, order["business_id"], res["_id"], "Order could not be fully reserved", actor)
        name = order["lines"][len(made)]["snapshot"]["name"]
        raise OrderError(f"{name}: {e.detail}", e.status, e.reason or "unavailable")
    return made


async def confirm(db, business_id: str, order_id: str, customer_accepted: Any, actor: str,
                  payment_window_hours: int = DEFAULT_PAYMENT_WINDOW_HOURS, now: Optional[datetime] = None) -> dict:
    """Needs explicit acceptance. Prices are re-read from the catalog, so a stale quote can't be honoured silently."""
    if customer_accepted is not True:
        raise OrderError("The customer must accept the order first.", 400, "not_accepted")
    now = now or utcnow()
    order = await get_order(db, business_id, order_id)
    if order["status"] == CONFIRMED or order["status"] in (IN_PROGRESS, COMPLETED):
        return order
    if order["status"] not in EDITABLE:
        raise OrderError(f"This order is {order['status']}.", 409)
    if any(line["needs_quote"] for line in order["lines"]):
        raise OrderError("A quoted price must be approved before this can be confirmed.", 409, "quote_needed")
    claimed = await db[ORDERS].find_one_and_update({"_id": order_id, "status": {"$in": list(EDITABLE)}},
                                                   {"$set": {"status": "confirming", "updated_at": now}},
                                                   return_document=ReturnDocument.BEFORE)
    if not claimed:
        return await get_order(db, business_id, order_id)
    try:
        lines = order["lines"]
        for line in lines:
            fresh = await _snapshot_line(db, business_id, {**line, "item_id": line["catalog_item_id"]}, now)
            if fresh["unit_amount"] != line["unit_amount"]:
                raise OrderError("A price changed since this order was prepared. Review and send it again.", 409, "price_changed")
            line["snapshot"] = fresh["snapshot"]
        made = await _reserve_all(db, order, actor)
    except Exception:
        await db[ORDERS].update_one({"_id": order_id}, {"$set": {"status": claimed["status"]}})
        raise
    for line, res in zip(lines, made):
        line["reservation_id"], line["fulfilment_status"] = res["_id"], QUEUED
    paid_now = order["total"] == 0
    await db[ORDERS].update_one({"_id": order_id}, {"$set": {
        "status": CONFIRMED, "lines": lines, "fulfilment_status": QUEUED, "confirmed_at": now, "accepted_at": now,
        "payment_due_at": None if paid_now else now + timedelta(hours=payment_window_hours),
        "payment_status": PAID if paid_now else UNPAID, "updated_at": now}})
    order = await get_order(db, business_id, order_id)
    await _log(db, order, "confirmed", actor, {"total": order["total"]}, f"confirmed:{order_id}")
    return await _commit_if_paid(db, order, actor) if paid_now else order


# ── payment: attempts, verified events, balances ─────────────────────────────

def _tag(business_id: str) -> str:
    """Order numbers repeat across businesses, but a payment reference is looked up by the provider alone."""
    return hashlib.sha1(str(business_id).encode()).hexdigest()[:6]


async def request_payment(db, business_id: str, order_id: str, amount: Optional[int], actor: str) -> dict:
    """Creates a reference to pay against. It never marks anything paid."""
    order = await get_order(db, business_id, order_id)
    if order["status"] not in OPEN:
        raise OrderError("Confirm the order before asking for payment.", 409, "not_confirmed")
    due = order["total"] - order["paid_amount"]
    if due <= 0:
        raise OrderError("Nothing is owed on this order.", 409, "nothing_due")
    amount = due if amount is None else positive_int(amount, "Amount")
    if amount > due:
        raise OrderError("That's more than the balance due.", 409, "over_balance")
    count = await db[PAYMENTS].count_documents({"order_id": order_id}) + 1
    pay = {"_id": uuid.uuid4().hex, "business_id": business_id, "order_id": order_id, "reference": f"{order['order_number']}-P{count}-{_tag(business_id)}",
           "amount": amount, "currency": order["currency"], "status": PENDING, "source": "", "requested_by": actor,
           "created_at": utcnow()}
    await db[PAYMENTS].insert_one(pay)
    if order["payment_status"] == UNPAID:
        await db[ORDERS].update_one({"_id": order_id, "payment_status": UNPAID}, {"$set": {"payment_status": PENDING}})
    await _log(db, order, "payment_requested", actor, {"reference": pay["reference"], "amount": amount})
    return pay


async def record_payment(db, business_id: str, order_id: str, amount: int, source: str, actor: str, reference: str = "",
                         transaction_id: str = "", method: str = "", note: str = "") -> dict:
    """Only a provider event or a permitted merchant entry lands here; a chat message or screenshot never does."""
    if source not in PAYMENT_SOURCES:
        raise OrderError("Payment must come from the provider or be recorded by the business.", 400, "bad_source")
    amount = positive_int(amount, "Amount")
    order = await get_order(db, business_id, order_id)
    late = source == "provider" and order["status"] in (CANCELLED, EXPIRED)
    if order["status"] not in OPEN and not late:
        raise OrderError("This order isn't open for payment.", 409, "not_open")
    key = (transaction_id or "").strip()[:120]
    if key:
        again = await db[PAYMENTS].find_one({"txn_scope": f"{business_id}:{key}"})
        if again:
            return await get_order(db, business_id, order_id)
    due = order["total"] - order["paid_amount"]
    if amount > due and not late:
        await _log(db, order, "payment_mismatch", actor, {"amount": amount, "due": due, "reference": reference})
        raise OrderError("That payment is more than the balance due and needs a manual review.", 409, "payment_mismatch")
    pay = await db[PAYMENTS].find_one({"order_id": order_id, "reference": reference, "status": PENDING}) if reference else None
    fields = {"status": PAID, "source": source, "amount": amount, "method": method[:40], "note": note[:300],
              "confirmed_by": actor, "confirmed_at": utcnow(), "transaction_id": key,
              **({"txn_scope": f"{business_id}:{key}"} if key else {})}
    try:
        if pay:
            await db[PAYMENTS].update_one({"_id": pay["_id"], "status": PENDING}, {"$set": fields})
        else:
            await db[PAYMENTS].insert_one({"_id": uuid.uuid4().hex, "business_id": business_id, "order_id": order_id,
                                           "reference": reference or f"{order['order_number']}-M{uuid.uuid4().hex[:6]}",
                                           "currency": order["currency"], "created_at": utcnow(), **fields})
    except DuplicateKeyError:
        return await get_order(db, business_id, order_id)
    order = await _recompute_payment(db, order, actor)
    if late:
        # The customer paid for an order that was already cancelled: keep the record and owe them the money back.
        await db[ORDERS].update_one({"_id": order_id}, {"$set": {"refund_due": True}})
        order = await get_order(db, business_id, order_id)
        await _log(db, order, "payment_after_cancel", actor, {"amount": amount, "status": order["status"]})
        return order
    await _log(db, order, "payment_status_changed", actor, {"payment_status": order["payment_status"], "amount": amount,
                                                           "source": source})
    return await _commit_if_paid(db, order, actor)


async def _recompute_payment(db, order: dict, actor: str) -> dict:
    paid = sum(p["amount"] for p in await db[PAYMENTS].find({"order_id": order["_id"], "status": PAID}).to_list(500))
    status = PAID if paid >= order["total"] else PARTIALLY_PAID if paid else order["payment_status"]
    if status == PAID:
        await db[PAYMENTS].update_many({"order_id": order["_id"], "status": PENDING}, {"$set": {"status": "closed", "closed_at": utcnow()}})
    await db[ORDERS].update_one({"_id": order["_id"]}, {"$set": {"paid_amount": paid, "due_amount": order["total"] - paid,
                                                                  "payment_status": status, "updated_at": utcnow()}})
    return await get_order(db, order["business_id"], order["_id"])


async def mark_payment_failed(db, business_id: str, order_id: str, reference: str, actor: str) -> dict:
    order = await get_order(db, business_id, order_id)
    won = await db[PAYMENTS].find_one_and_update({"order_id": order_id, "reference": reference, "status": PENDING},
                                                 {"$set": {"status": FAILED, "failed_at": utcnow()}})
    if won and order["payment_status"] == PENDING:
        still = await db[PAYMENTS].count_documents({"order_id": order_id, "status": PENDING})
        if not still:
            await db[ORDERS].update_one({"_id": order_id}, {"$set": {"payment_status": UNPAID}})
    await _log(db, order, "payment_failed", actor, {"reference": reference})
    return await get_order(db, business_id, order_id)


async def _commit_if_paid(db, order: dict, actor: str) -> dict:
    """Full payment turns every hold into a sale. A line the team still has to approve stays held and is flagged."""
    if order["payment_status"] != PAID:
        return order
    flags = []
    for line in order["lines"]:
        if not line.get("reservation_id"):
            continue
        try:
            try:
                await reservations.commit(db, order["business_id"], line["reservation_id"], actor, order["_id"])
            except CatalogError as e:
                if e.reason != "needs_approval":
                    raise
                # A request-to-book item waits for the team's yes. A verified payment in full on a confirmed order is that yes.
                await reservations.approve(db, order["business_id"], line["reservation_id"], actor)
                await reservations.commit(db, order["business_id"], line["reservation_id"], actor, order["_id"])
        except CatalogError as e:
            flags.append({"line_id": line["id"], "reason": e.reason or e.detail})
    if flags:
        await db[ORDERS].update_one({"_id": order["_id"]}, {"$set": {"exception_flags": flags}})
        await _log(db, order, "needs_attention", actor, {"flags": flags})
    return await get_order(db, order["business_id"], order["_id"])


# ── adjustments and revisions ────────────────────────────────────────────────

ADJUSTMENT_KINDS = ("discount", "fee")


async def add_adjustment(db, business_id: str, order_id: str, kind: str, amount: int, reason: str, actor: str) -> dict:
    """A documented change to the total. On a confirmed order it leaves a revision with before and after."""
    if kind not in ADJUSTMENT_KINDS:
        raise OrderError(f"Adjustment must be one of: {', '.join(ADJUSTMENT_KINDS)}.")
    amount = positive_int(amount, "Amount")
    if not (reason or "").strip():
        raise OrderError("Say why this is being adjusted.")
    order = await get_order(db, business_id, order_id)
    if order["status"] in (CANCELLED, EXPIRED, COMPLETED):
        raise OrderError(f"This order is {order['status']}.", 409)
    discount = order["discount_total"] + (amount if kind == "discount" else 0)
    fees = order["fees_total"] + (amount if kind == "fee" else 0)
    totals = compute_totals(order["lines"], fees, discount, order["tax_total"])
    if totals["total"] < order["paid_amount"]:
        raise OrderError("That would bring the total below what has already been paid. Refund the difference instead.", 409,
                         "below_paid")
    entry = {"id": uuid.uuid4().hex[:10], "kind": kind, "amount": amount, "reason": reason.strip()[:300], "actor": actor,
             "created_at": utcnow()}
    revision = {"number": len(order.get("revisions", [])) + 1, "reason": entry["reason"], "actor": actor, "created_at": utcnow(),
                "before": {"total": order["total"], "fees_total": order["fees_total"], "discount_total": order["discount_total"]},
                "after": {"total": totals["total"], "fees_total": fees, "discount_total": totals["discount_total"]},
                "customer_acceptance_needed": order["status"] in OPEN and totals["total"] > order["total"]}
    await db[ORDERS].update_one({"_id": order_id}, {"$set": {**totals, "fees_total": fees, "due_amount": totals["total"] - order["paid_amount"],
                                                             "updated_at": utcnow()},
                                                    "$push": {"adjustments": entry, "revisions": revision}})
    order = await get_order(db, business_id, order_id)
    await _log(db, order, "adjusted", actor, {"kind": kind, "amount": amount, "reason": entry["reason"], "revision": revision["number"]})
    if order["status"] in OPEN and order["paid_amount"] >= order["total"] and order["payment_status"] not in (PAID, REFUNDED, PARTIALLY_REFUNDED):
        order = await _recompute_payment(db, order, actor)
        order = await _commit_if_paid(db, order, actor)
    return order


# ── fulfilment ───────────────────────────────────────────────────────────────

def _summarise(lines: list[dict]) -> str:
    states = [line["fulfilment_status"] for line in lines]
    if all(s == FULFILLED for s in states):
        return FULFILLED
    if any(s == DELIVERY_FAILED for s in states):
        return DELIVERY_FAILED
    if any(s == FULFILLED for s in states):
        return PARTIALLY_FULFILLED
    if any(s == PROCESSING for s in states):
        return PROCESSING
    return QUEUED


async def update_fulfilment(db, business_id: str, order_id: str, line_id: str, status: str, actor: str,
                            reference: str = "", note: str = "") -> dict:
    if status not in LINE_FULFILMENT:
        raise OrderError(f"Status must be one of: {', '.join(LINE_FULFILMENT)}.")
    order = await get_order(db, business_id, order_id)
    if order["status"] not in OPEN:
        raise OrderError("Only a confirmed order can be fulfilled.", 409, "not_open")
    line = next((x for x in order["lines"] if x["id"] == line_id), None)
    if not line:
        raise OrderError("That line isn't on this order.", 404)
    if status == FULFILLED and order["payment_status"] != PAID:
        raise OrderError("This order hasn't been paid in full, so it can't be marked fulfilled.", 409, "unpaid")
    if status == FULFILLED and line.get("reservation_id"):
        try:
            await reservations.fulfil(db, business_id, line["reservation_id"], actor)
        except CatalogError as e:
            raise OrderError(e.detail, e.status, e.reason)
    line.update(fulfilment_status=status, **({"fulfilment_reference": reference[:120]} if reference else {}),
                **({"fulfilment_note": note[:300]} if note else {}))
    summary = _summarise(order["lines"])
    done = summary == FULFILLED and order["payment_status"] == PAID
    await db[ORDERS].update_one({"_id": order_id}, {"$set": {
        "lines": order["lines"], "fulfilment_status": summary, "updated_at": utcnow(),
        "status": COMPLETED if done else IN_PROGRESS if summary != QUEUED else order["status"],
        **({"completed_at": utcnow()} if done else {})}})
    order = await get_order(db, business_id, order_id)
    await _log(db, order, "fulfilment_updated", actor, {"line_id": line_id, "status": status})
    if done:
        await _log(db, order, "completed", actor, {}, f"completed:{order_id}")
    return order


# ── cancellation and refunds ─────────────────────────────────────────────────

async def cancel(db, business_id: str, order_id: str, reason: str, actor: str, requested_by: str = "merchant") -> dict:
    order = await get_order(db, business_id, order_id)
    if order["status"] in (CANCELLED, EXPIRED):
        return order
    if order["status"] == COMPLETED:
        raise OrderError("A completed order can't be cancelled. Request a refund instead.", 409, "completed")
    released, kept = [], []
    for line in order["lines"]:
        if not line.get("reservation_id"):
            continue
        try:
            await reservations.release(db, business_id, line["reservation_id"], reason or "Order cancelled", actor)
            released.append(line["id"])
        except CatalogError:
            kept.append(line["id"])
    paid = order["paid_amount"]
    await db[ORDERS].update_one({"_id": order_id}, {"$set": {
        "status": CANCELLED, "cancelled_at": utcnow(), "cancel_reason": reason[:300], "cancelled_by": actor,
        "cancel_requested_by": requested_by, "refund_due": paid > 0, "updated_at": utcnow()}})
    order = await get_order(db, business_id, order_id)
    await _log(db, order, "cancelled", actor, {"reason": reason[:300], "released": released, "fulfilled_kept": kept,
                                                "refund_due": paid, "requested_by": requested_by}, f"cancelled:{order_id}")
    return order


async def request_refund(db, business_id: str, order_id: str, amount: int, reason: str, actor: str) -> dict:
    """A request only. The order's payment position changes when the provider confirms the refund."""
    order = await get_order(db, business_id, order_id)
    amount = positive_int(amount, "Amount")
    open_refunds = sum(r["amount"] for r in await db[REFUNDS].find(
        {"order_id": order_id, "status": {"$in": ["requested", "completed"]}}).to_list(100))
    if amount + open_refunds > order["paid_amount"]:
        raise OrderError("That's more than has been paid.", 409, "over_refund")
    doc = {"_id": uuid.uuid4().hex, "business_id": business_id, "order_id": order_id, "amount": amount, "reason": reason[:300],
           "status": "requested", "requested_by": actor, "created_at": utcnow()}
    await db[REFUNDS].insert_one(doc)
    await _log(db, order, "refund_status_changed", actor, {"status": "requested", "amount": amount})
    return doc


async def complete_refund(db, business_id: str, order_id: str, refund_id: str, provider_reference: str, actor: str) -> dict:
    """The provider has sent the money back. Only now does the order's payment position change."""
    order = await get_order(db, business_id, order_id)
    won = await db[REFUNDS].find_one_and_update(
        {"_id": refund_id, "order_id": order_id, "business_id": business_id, "status": "requested"},
        {"$set": {"status": "completed", "provider_reference": provider_reference[:120], "completed_at": utcnow()}})
    if not won:
        return order
    refunded = order.get("refunded_amount", 0) + won["amount"]
    full = refunded >= order["paid_amount"]
    await db[ORDERS].update_one({"_id": order_id}, {"$set": {
        "refunded_amount": refunded, "payment_status": REFUNDED if full else PARTIALLY_REFUNDED,
        "refund_due": not full and order.get("refund_due", False), "updated_at": utcnow()}})
    order = await get_order(db, business_id, order_id)
    await _log(db, order, "refund_status_changed", actor, {"status": "completed", "amount": won["amount"], "refund_id": refund_id},
               f"refund:{refund_id}")
    return order


async def expire_due(db, business_id: Optional[str] = None, now: Optional[datetime] = None) -> int:
    """Unpaid orders past their payment window are cancelled and their holds returned."""
    now = now or utcnow()
    flt: dict[str, Any] = {"status": CONFIRMED, "payment_status": {"$in": [UNPAID]}, "payment_due_at": {"$lte": now, "$ne": None}}
    if business_id:
        flt["business_id"] = business_id
    count = 0
    for order in await db[ORDERS].find(flt).to_list(200):
        await cancel(db, order["business_id"], order["_id"], "Payment window ended", "system", "system")
        await db[ORDERS].update_one({"_id": order["_id"]}, {"$set": {"status": EXPIRED}})
        count += 1
    return count


# ── reading ──────────────────────────────────────────────────────────────────

async def list_orders(db, business_id: str, q: str = "", status: str = "", payment_status: str = "", fulfilment_status: str = "",
                      source: str = "", assignee: str = "", customer_phone: str = "", limit: int = 50, skip: int = 0) -> dict:
    flt: dict[str, Any] = {"business_id": business_id}
    for key, value in (("status", status), ("payment_status", payment_status), ("fulfilment_status", fulfilment_status),
                       ("source", source), ("assignee", assignee), ("customer.phone", customer_phone)):
        if value:
            flt[key] = {"$in": value.split(",")} if key != "customer.phone" else value
    if q:
        import re
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        flt["$or"] = [{"order_number": rx}, {"customer.name": rx}, {"customer.phone": rx}, {"lines.snapshot.name": rx}]
    total = await db[ORDERS].count_documents(flt)
    rows = await db[ORDERS].find(flt).sort("created_at", -1).skip(max(0, skip)).limit(max(1, min(limit, 200))).to_list(200)
    return {"orders": rows, "total": total}


async def customer_orders(db, business_id: str, phone: str) -> list[dict]:
    return (await list_orders(db, business_id, customer_phone=phone, limit=100))["orders"]


async def assign(db, business_id: str, order_id: str, assignee: str, actor: str) -> dict:
    order = await get_order(db, business_id, order_id)
    await db[ORDERS].update_one({"_id": order_id}, {"$set": {"assignee": assignee[:80], "updated_at": utcnow()}})
    await _log(db, order, "assigned", actor, {"assignee": assignee[:80]})
    return await get_order(db, business_id, order_id)


def export_csv(rows: list[dict]) -> str:
    import csv
    import io
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["order_number", "created_at", "status", "payment_status", "fulfilment_status", "customer", "phone",
                     "currency", "total", "paid", "due", "source"])
    for r in rows:
        writer.writerow([r["order_number"], r["created_at"].isoformat(), r["status"], r["payment_status"], r["fulfilment_status"],
                         _safe(r["customer"].get("name", "")), _safe(r["customer"].get("phone", "")), r["currency"],
                         r["total"] / 100, r["paid_amount"] / 100, r["due_amount"] / 100, r["source"]])
    return out.getvalue()


def _safe(value: str) -> str:
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value
