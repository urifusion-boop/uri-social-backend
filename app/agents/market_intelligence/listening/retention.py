"""
Retention and deletion (PRD §12, §15).

"Raw provider payloads... 30-90 days"; "Normalised items + evidence
text... 90 days hot; then per terms" — this codebase stores items in
already-normalised form with no separate raw/hot-archive tiering, so both
tiers collapse into one: mi_listen_items (the post itself: text, url,
author, metrics) and mi_listen_readings (its AI reading) age out together.

"Deletion requests (from a user, a platform takedown or a retention
expiry) cascade through items, enrichment, embeddings and evidence
references; aggregates remain because they contain no personal data."
delete_items() is that cascade's one primitive — enforce_retention() is
the retention-expiry trigger that calls it on a schedule. The other two
triggers (a user's own deletion request, a platform takedown notice) have
no API surface anywhere in this codebase yet, so they're not wired up,
but reuse the exact same primitive once something originates them.

Never touched by any of this: mi_topic_counts (aggregate counts),
mi_market_topics (the topic graph), mi_listen_signals, mi_listen_cards,
mi_listen_card_actions — derived knowledge and AI-written text, not the
original post, and explicitly meant to be kept long-term (§8.1, §12).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


async def delete_items(db, source_ids: list[str]) -> dict:
    if not source_ids:
        return {"items": 0, "readings": 0}
    items = await db["mi_listen_items"].delete_many({"source_id": {"$in": source_ids}})
    readings = await db["mi_listen_readings"].delete_many({"source_id": {"$in": source_ids}})
    return {"items": items.deleted_count, "readings": readings.deleted_count}


async def enforce_retention(db, cfg: dict, now: Optional[datetime] = None) -> dict:
    """Items published before the retention cutoff (and their readings) are
    deleted. Runs globally, not per market, since an item can belong to
    more than one market's collection."""
    r = cfg["retention"]
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=r["item_days"])
    source_ids = [doc["source_id"] async for doc in db["mi_listen_items"].find(
        {"published_at": {"$lt": cutoff}}, {"source_id": 1})]
    result = await delete_items(db, source_ids)
    result["cutoff"] = cutoff
    return result
