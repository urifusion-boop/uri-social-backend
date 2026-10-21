"""
Golden datasets (PRD §17): "Golden datasets per category and language...
labelled for relevance, intent, sentiment and buying intent; refreshed
continuously from curator corrections."

A curator resolving a low-confidence reading (review.py) can promote it
into the golden set: a hand-confirmed label worth keeping as an
evaluation anchor. Snapshotted at the moment it's marked, independent of
mi_listen_readings and mi_listen_review_corrections, so a later edit to
the live reading (or a different correction) never silently changes what
a golden example already represents.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional


async def mark_golden(db, source_id: str, market: str, actor: str, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    reading = await db["mi_listen_readings"].find_one({"source_id": source_id, "market": market})
    if not reading:
        return {"found": False}
    item = await db["mi_listen_items"].find_one({"source_id": source_id}, {"text": 1})
    doc = {
        "source_id": source_id, "market": market, "text": (item or {}).get("text"),
        "on_topic": reading.get("on_topic"), "intent": reading.get("intent"),
        "sentiment": reading.get("sentiment"), "buying_intent": reading.get("buying_intent"),
        "topics": reading.get("topics"), "marked_by": actor, "marked_at": now,
        "prompt_version": reading.get("prompt_version"),
    }
    await db["mi_golden_examples"].update_one({"source_id": source_id, "market": market}, {"$set": doc}, upsert=True)
    return {"found": True, "marked": True}


async def unmark_golden(db, source_id: str, market: str) -> dict:
    result = await db["mi_golden_examples"].delete_one({"source_id": source_id, "market": market})
    return {"removed": result.deleted_count > 0}


async def list_golden(db, market: Optional[str] = None, limit: int = 200) -> list[dict]:
    q: dict = {"market": market} if market else {}
    out = []
    async for g in db["mi_golden_examples"].find(q).limit(min(limit, 500)):
        g.pop("_id", None)
        out.append(g)
    return out
