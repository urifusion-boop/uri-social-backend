"""Customer messages about an order: one per event, logged with channel, recipient and result.

The message goes into the conversation the order came from. A customer with no chat on file is
logged as unreachable, never guessed at. Duplicates are dropped by the (order, kind, key) record."""
from __future__ import annotations

import uuid
from typing import Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.agents.catalog.models import utcnow

from .models import ORDERS

NOTIFICATIONS = "order_notifications"
ACTOR = "orders"


def _money(minor: int, currency: str) -> str:
    symbol = {"NGN": "₦", "USD": "$", "GBP": "£", "EUR": "€"}.get(currency, f"{currency} ")
    return f"{symbol}{minor / 100:,.0f}" if minor % 100 == 0 else f"{symbol}{minor / 100:,.2f}"


def message_for(kind: str, order: dict, payload: dict, actor: str) -> tuple[str, str]:
    """(dedupe key, customer text); an empty text means nothing to send."""
    n, cur = order["order_number"], order["currency"]
    if kind == "confirmed" and actor != "jane":
        return "", f"Your order {n} is confirmed. Total {_money(order['total'], cur)}. We'll send payment details shortly."
    if kind == "payment_status_changed":
        if order["payment_status"] == "paid":
            return f"paid", f"We've received your payment for order {n}. Thank you! Next we'll get it to you."
        if order["payment_status"] == "partially_paid":
            return f"part:{order['paid_amount']}", (f"We've received {_money(order['paid_amount'], cur)} for order {n}. "
                                                    f"The balance of {_money(order['due_amount'], cur)} is still due.")
    if kind == "fulfilment_updated":
        status = payload.get("status")
        text = {"processing": f"Your order {n} is being prepared.", "fulfilled": f"Your order {n} is complete. Thank you!",
                "delivery_failed": f"There was a problem delivering order {n}. We'll be in touch to sort it out."}.get(status, "")
        return f"{payload.get('line_id')}:{status}", text
    if kind == "cancelled" and payload.get("requested_by") != "customer":
        due = f" Your refund of {_money(order['paid_amount'], cur)} will be handled by the team." if order["paid_amount"] else ""
        return "", f"Order {n} has been cancelled.{due}"
    if kind == "adjusted" and order["status"] in ("confirmed", "in_progress"):
        return f"rev:{payload.get('revision')}", (f"Order {n} was updated: the new total is {_money(order['total'], cur)}"
                                                   f" ({_money(order['due_amount'], cur)} still due).")
    if kind == "refund_status_changed" and payload.get("status") == "completed":
        return f"refund:{payload.get('refund_id')}", f"Your refund of {_money(payload.get('amount', 0), cur)} for order {n} has been sent."
    if kind == "return_status_changed" and payload.get("status") in ("approved", "rejected", "resolved"):
        return f"return:{payload.get('return_id')}:{payload.get('status')}", f"Update on your return for order {n}: {payload['status']}."
    return "", ""


async def _channel(db, order: dict) -> tuple[Optional[dict], Optional[dict]]:
    from app.agents.inbox.entities import CONVERSATIONS
    ref = order.get("source_ref") or ""
    if order.get("source") != "onebox" or not ObjectId.is_valid(ref):
        return None, None
    conv = await db[CONVERSATIONS].find_one({"_id": ObjectId(ref), "workspace_id": order["business_id"]})
    return conv, conv and {"platform": conv.get("platform"), "conversation_id": ref}


async def maybe(db, order_id: str, kind: str, actor: str, payload: dict, transport=None) -> Optional[dict]:
    order = await db[ORDERS].find_one({"_id": order_id})
    if not order:
        return None
    key, text = message_for(kind, order, payload, actor)
    if not text:
        return None
    note = {"business_id": order["business_id"], "order_id": order_id, "kind": kind, "dedupe": f"{order_id}:{kind}:{key}",
            "text": text, "created_at": utcnow(), "recipient": order["customer"].get("name") or order["customer"].get("phone", ""),
            "result": "pending"}
    if await db[NOTIFICATIONS].find_one({"dedupe": note["dedupe"]}):
        return None
    conv, channel = await _channel(db, order)
    note["channel"] = (channel or {}).get("platform") or "none"
    try:
        await db[NOTIFICATIONS].insert_one({"_id": uuid.uuid4().hex, **note})
    except DuplicateKeyError:
        return None
    if not conv:
        return await _finish(db, note, "no_channel")
    try:
        from app.agents.inbox.accounts import account_for
        from app.agents.inbox.meta_transport import meta_transport
        from app.agents.inbox.send import send_reply
        record = await send_reply(db, order["business_id"], channel["conversation_id"], text, idempotency_key=f"order:{note['dedupe']}"[:120],
                                  actor_id=ACTOR, transport=transport or meta_transport, account=await account_for(db, conv) or {})
        return await _finish(db, note, "sent" if record.get("delivery") == "accepted" else f"not_sent:{record.get('failure_reason') or record.get('delivery')}")
    except Exception as e:                      # a message that can't go (closed window, bad token) never breaks the order
        return await _finish(db, note, f"not_sent:{str(e)[:120]}")


async def _finish(db, note: dict, result: str) -> dict:
    await db[NOTIFICATIONS].update_one({"dedupe": note["dedupe"]}, {"$set": {"result": result, "finished_at": utcnow()}})
    return {**note, "result": result}


async def history(db, business_id: str, order_id: str) -> list[dict]:
    return await db[NOTIFICATIONS].find({"business_id": business_id, "order_id": order_id}).sort("created_at", 1).to_list(100)
