"""Jane turns buying intent into an order, and only the customer's own yes confirms it.

A draft order reserves nothing. Confirming needs the customer to accept a summary they have actually
been sent, and every price comes from the catalog, never from the model."""
from __future__ import annotations

import json
from typing import Awaitable, Callable, Optional

from app.agents.catalog import ai_tools
from app.agents.catalog.items import list_variants
from app.agents.catalog.models import CatalogError
from app.agents.orders import service
from app.agents.orders.indexes import ensure_indexes
from app.agents.orders.models import AWAITING_CUSTOMER, CONFIRMED, DRAFT, ORDERS, OrderError

Llm = Callable[[str, str], Awaitable[dict]]
SOURCE_ID = "catalog:order"
ACTOR = "jane"

_SYSTEM = """You read the customer's latest message in a sales chat and say whether they want to place an order.
Reply with JSON: {"intent": "none"|"order"|"accept"|"decline", "items": [{"name": str, "quantity": int, "option": str}]}
- order: the customer clearly wants to buy, book or subscribe to specific things from CATALOG_ITEMS (use the names exactly as listed; quantity defaults to 1; option is a size, colour, plan variant or empty).
- accept: OPEN_ORDER is present and the customer clearly says yes to going ahead with exactly what OPEN_ORDER contains. A yes that goes with a different item, size or quantity than OPEN_ORDER is not accept.
- If OPEN_ORDER is present and the customer now asks for something different from it (another item, option or quantity), the intent is "order", and items lists everything they now want, using the recent conversation for any item or option they refer to as "it" or "that".
- decline: OPEN_ORDER is present and the customer says no, cancel it, or never mind.
- Without OPEN_ORDER, a customer who says they want an item just discussed, or picks its size, option or quantity ("yes, in large"), is placing an order: intent "order", with that item and option taken from the recent conversation.
- none: anything else, including questions, browsing, and "maybe". When unsure, none."""


def _text(order: dict) -> str:
    lines = "; ".join(f"{x['quantity']} x {x['snapshot']['name']}" + (f" ({x['snapshot']['variant']})" if x["snapshot"].get("variant") else "")
                      + f" at {ai_tools.money(x['unit_amount'] / 100, order['currency'])}" for x in order["lines"])
    fee = f", delivery {ai_tools.money(order['fees_total'] / 100, order['currency'])}" if order["fees_total"] else ""
    return f"{lines}{fee}. Total {ai_tools.money(order['total'] / 100, order['currency'])}."


def _source(text: str, label: str = "Order") -> dict:
    return {"id": SOURCE_ID, "label": f"Catalog · {label}", "text": text, "approved": True, "price_or_stock": False}


async def open_order(db, workspace_id: str, conversation_id: str) -> Optional[dict]:
    return await db[ORDERS].find_one({"business_id": workspace_id, "source": "onebox", "source_ref": conversation_id,
                                      "status": {"$in": [DRAFT, AWAITING_CUSTOMER, CONFIRMED]}, "payment_status": {"$ne": "paid"}},
                                     sort=[("created_at", -1)])


def _naive(at):
    return at.replace(tzinfo=None) if at is not None and at.tzinfo else at


def _seen_summary(order: dict, history: list[dict]) -> bool:
    since = _naive(order.get("awaiting_at"))
    return bool(since) and any(m["direction"] == "outbound" and m.get("received_at") and _naive(m["received_at"]) >= since
                               for m in history)


async def _resolve(db, workspace_id: str, entry: dict) -> tuple[Optional[dict], str]:
    query = f"{entry.get('name', '')} {entry.get('option', '')}".strip()
    found = await ai_tools.search(db, workspace_id, query, 1)
    if not found:
        return None, f"I couldn't match '{entry.get('name', '')}' to anything in the catalog"
    item = found[0]
    variants = [v for v in await list_variants(db, workspace_id, item["_id"]) if v["status"] == "active"]
    variant = await ai_tools._variant_named(db, workspace_id, item, query) if variants else None
    if variants and item["availability_type"] == "quantity" and not variant:
        return None, f"{item['name']} comes in options ({', '.join(v['name'] for v in variants[:6])}); ask which one they want"
    qty = entry.get("quantity") if isinstance(entry.get("quantity"), int) and entry["quantity"] > 0 else 1
    return {"catalog_item_id": item["_id"], "variant_id": variant["_id"] if variant else None, "quantity": min(qty, 50)}, ""


async def _start(db, workspace_id: str, conv: dict, conversation_id: str, entries: list, message_id: str,
                 replacing: Optional[dict] = None) -> list[dict]:
    lines, problems = [], []
    for entry in entries[:10]:
        line, problem = await _resolve(db, workspace_id, entry)
        (lines.append(line) if line else problems.append(problem))
    if problems or not lines:
        kept = (f" OPEN ORDER {replacing['order_number']} ({_text(replacing)}) is unchanged and still not confirmed; "
                "do not treat a yes as confirming it unless the customer means that order.") if replacing else ""
        return [_source("ORDER NOT STARTED: " + "; ".join(problems or ["nothing to order was identified"]) +
                        ". Ask the customer for what is missing. Do not say an order was made." + kept)]
    try:
        order = await service.create_order(db, workspace_id, {
            "source": "onebox", "source_ref": conversation_id, "items": lines,
            "customer": {"name": conv.get("display_name", ""), "channel": conv.get("platform", "")}}, ACTOR,
            idempotency_key=f"onebox:{message_id}")
        order = await service.send_quote(db, workspace_id, order["_id"], ACTOR)
    except OrderError as e:
        return [_source(f"ORDER NOT STARTED: {e.detail} Do not say an order was made; offer alternatives or say the team will follow up.")]
    await db[ORDERS].update_one({"_id": order["_id"]}, {"$set": {"awaiting_at": ai_tools.utcnow()}})
    swapped = ""
    if replacing:
        try:
            await service.cancel(db, workspace_id, replacing["_id"], "Customer changed the order", ACTOR, "customer")
            swapped = f" This replaces {replacing['order_number']} ({_text(replacing)}), which was cancelled because they changed the order."
        except OrderError:
            swapped = ""
    return [_source(f"ORDER SUMMARY {order['order_number']} (not confirmed yet, nothing is held or paid): {_text(order)}{swapped} "
                    "Read this back to the customer and ask them to reply yes to confirm. Do not say it is booked or reserved.")]


async def _accept(db, workspace_id: str, order: dict) -> list[dict]:
    try:
        done = await service.confirm(db, workspace_id, order["_id"], True, ACTOR)
    except OrderError as e:
        return [_source(f"ORDER {order['order_number']} COULD NOT BE CONFIRMED: {e.detail} Nothing is reserved. "
                        "Apologise, offer alternatives, and say the team will follow up. Do not say it is booked.")]
    return [_source(f"ORDER {done['order_number']} CONFIRMED and the items are held for the customer: {_text(done)} "
                    "Payment has NOT been received yet; say the team will send payment details shortly. "
                    "Do not say it is paid.")]


async def step(db, workspace_id: str, conv: dict, conversation_id: str, text: str, history: list[dict],
               message_id: str, llm: Llm) -> list[dict]:
    """Sources describing what just happened to the customer's order, for the reply to state faithfully."""
    await ensure_indexes(db)
    order = await open_order(db, workspace_id, conversation_id)
    recent = " ".join(m["text"] for m in history[-4:] if m["direction"] == "inbound")
    try:
        candidates = [i["name"] for i in await ai_tools.search(db, workspace_id, f"{recent} {text}", 5)]
    except CatalogError:
        candidates = []
    if not candidates and not order:
        return []
    data = await llm(_SYSTEM, json.dumps({
        "message": text, "recent_conversation": [{"from": m["direction"], "text": m["text"]} for m in history[-6:]],
        "CATALOG_ITEMS": candidates, "OPEN_ORDER": _text(order) if order and order["status"] == AWAITING_CUSTOMER else None}))
    intent = str(data.get("intent") or "none")
    if intent == "accept" and order and order["status"] == AWAITING_CUSTOMER and _seen_summary(order, history):
        return await _accept(db, workspace_id, order)
    if intent == "decline" and order and order["status"] in (DRAFT, AWAITING_CUSTOMER):
        await service.cancel(db, workspace_id, order["_id"], "Customer declined", ACTOR, "customer")
        return [_source(f"ORDER {order['order_number']} CANCELLED at the customer's request. Nothing was held or charged.")]
    if intent == "order":
        waiting = order if order and order["status"] == AWAITING_CUSTOMER else None
        return await _start(db, workspace_id, conv, conversation_id, data.get("items") or [], message_id, waiting)
    if order and order["status"] == AWAITING_CUSTOMER:
        return [_source(f"OPEN ORDER {order['order_number']} awaiting the customer's yes: {_text(order)}")]
    if order and order["status"] == CONFIRMED:
        return [_source(f"ORDER {order['order_number']} is confirmed, payment status: {order['payment_status']}.")]
    if intent == "accept":
        return [_source("NO ORDER EXISTS for this conversation yet, so there is nothing to confirm. Never say an order is placed, "
                        "confirmed or being processed; ask the customer exactly what they want to order.")]
    return []
