"""
Human review queue (PRD §17): "low-confidence classifications, new
entities and borderline alerts can be reviewed by an internal curator;
reviews feed training data." New entities and borderline topics already
have their own queue (discovery/curation, for observed/candidate topics).
This is the per-POST half: individual readings the AI was unsure about.

Scope: a correction updates the reading itself and is logged to
mi_listen_review_corrections for a future training/evaluation pass — it
deliberately does NOT retroactively rewrite mi_topic_counts. That
aggregate is built with $addToSet on author handles per bucket; reversing
one item's contribution correctly (without silently erasing a *different*
item's legitimate contribution from the same author in the same bucket)
needs more bookkeeping than exists today. A correction changes what the
system does from here on, not a count a card has already cited.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


async def pending_reviews(db, cfg: dict, market: Optional[str] = None, limit: int = 50,
                          now: Optional[datetime] = None) -> list[dict]:
    """Unreviewed readings below the confidence threshold, most uncertain
    first, paired with their post's text/url for context."""
    rc = cfg["review"]
    now = now or datetime.utcnow()
    since = now - timedelta(days=rc["window_days"])
    q: dict = {"confidence": {"$lt": rc["confidence_threshold"]}, "reviewed": {"$ne": True},
              "published_at": {"$gte": since}}
    if market:
        q["market"] = market
    out = []
    async for r in db["mi_listen_readings"].find(q).sort([("confidence", 1)]).limit(min(limit, 200)):
        r.pop("_id", None)
        item = await db["mi_listen_items"].find_one(
            {"source_id": r["source_id"]}, {"text": 1, "url": 1, "author_handle": 1})
        out.append({**r, "text": (item or {}).get("text"), "url": (item or {}).get("url")})
    return out


async def resolve_review(db, source_id: str, market: str, actor: str,
                         corrected_on_topic: Optional[bool] = None, corrected_intent: Optional[str] = None,
                         corrected_topics: Optional[list[str]] = None, note: Optional[str] = None,
                         now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    reading = await db["mi_listen_readings"].find_one({"source_id": source_id, "market": market})
    if not reading:
        return {"found": False}

    corrections: dict = {}
    if corrected_on_topic is not None and corrected_on_topic != reading.get("on_topic"):
        corrections["on_topic"] = corrected_on_topic
    if corrected_intent is not None and corrected_intent != reading.get("intent"):
        corrections["intent"] = corrected_intent
    if corrected_topics is not None and corrected_topics != reading.get("topics"):
        corrections["topics"] = corrected_topics

    await db["mi_listen_readings"].update_one(
        {"source_id": source_id, "market": market},
        {"$set": {"reviewed": True, "reviewed_by": actor, "reviewed_at": now, **corrections}},
    )
    await db["mi_listen_review_corrections"].insert_one({
        "source_id": source_id, "market": market,
        "original": {k: reading.get(k) for k in ("on_topic", "intent", "topics", "confidence")},
        "corrections": corrections, "actor": actor, "note": (note or "")[:300] or None, "at": now,
    })
    return {"found": True, "corrected_fields": sorted(corrections.keys())}
