"""Events other modules can react to, written to an outbox so a failed delivery is retried
and a repeated one is dropped.

The outbox row is written right after the change it describes. Mongo has no multi-document
transaction here, so a crash between the two loses the event but never the change; the
counters stay the source of truth and the next check reads them directly.
"""
from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable, Optional

from pymongo.errors import DuplicateKeyError

from .models import EVENTS, utcnow

MAX_ATTEMPTS = 5
Subscriber = Callable[[dict], Awaitable[None]]
_subscribers: list[Subscriber] = []


def subscribe(fn: Subscriber) -> None:
    if fn not in _subscribers:
        _subscribers.append(fn)


def unsubscribe_all() -> None:
    _subscribers.clear()


async def emit(db, business_id: str, event_type: str, payload: dict[str, Any],
               dedupe_key: Optional[str] = None) -> Optional[str]:
    doc: dict[str, Any] = {"_id": uuid.uuid4().hex, "business_id": business_id, "type": event_type,
                           "payload": payload, "created_at": utcnow(), "delivered": False, "attempts": 0}
    if dedupe_key:
        doc["dedupe_key"] = f"{business_id}:{dedupe_key}"
    try:
        await db[EVENTS].insert_one(doc)
    except DuplicateKeyError:
        return None
    except Exception as e:                      # an event must never fail the sale it describes
        print(f"[Catalog] could not record {event_type}: {e}", flush=True)
        return None
    return doc["_id"]


async def dispatch_pending(db, limit: int = 200) -> dict[str, int]:
    """Hand undelivered events to every subscriber; a failure is counted and retried next run."""
    sent = failed = 0
    rows = await db[EVENTS].find({"delivered": False, "attempts": {"$lt": MAX_ATTEMPTS}}) \
        .sort("created_at", 1).to_list(limit)
    for row in rows:
        try:
            for fn in list(_subscribers):
                await fn(row)
        except Exception as e:
            failed += 1
            await db[EVENTS].update_one({"_id": row["_id"]}, {"$inc": {"attempts": 1},
                                                              "$set": {"last_error": str(e)[:300]}})
            continue
        sent += 1
        await db[EVENTS].update_one({"_id": row["_id"]}, {"$set": {"delivered": True, "delivered_at": utcnow()}})
    return {"delivered": sent, "failed": failed}
