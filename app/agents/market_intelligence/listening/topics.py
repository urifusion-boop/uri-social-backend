"""
Topics and counts (spec §7 stages 5-6).

Many wordings of one thing become one topic: "Latasha wig", "latasha wigs",
"#latashawig" and "latasha-wig" all resolve to the topic key "latasha wig".
Every relevant post is counted into an hourly bucket per market × topic:
distinct accounts (the headline measure — one account posting ten times is
one voice), posts, engagement and the intent mix.

Also builds the two PRD §8.1 topic-graph edges this module can produce
without new infrastructure: an embedding per topic (resolve(), computed
once when the topic is first created — PRD §8 stage 4), and co-occurrence
counts between topics mentioned in the same post (count(), since the
reading it's called with already carries every topic that post resolved
to, not just this one).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from . import embeddings

GENERIC = {"hair", "skin", "product", "products", "price", "business", "food", "clothes", "fashion", "beauty",
           "love", "buy", "sell", "today", "people", "thing", "things", "shop", "store", "online"}


def normalize(phrase: str) -> Optional[str]:
    t = (phrase or "").lower().replace("#", " ").replace("_", " ").replace("-", " ")
    t = re.sub(r"[^\w\s']", " ", t)
    words = [w for w in t.split() if w]
    if not words or len(words) > 5:
        return None
    # light singularisation of the last word ("wigs" -> "wig"), not "ss" ("dress")
    last = words[-1]
    if len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
        words[-1] = last[:-1]
    key = " ".join(words)
    if key in GENERIC or len(key) < 3:
        return None
    return key


def compact(key: str) -> str:
    return key.replace(" ", "")


async def resolve(db, market: str, phrase: str, now: Optional[datetime] = None) -> Optional[str]:
    """The canonical topic key for a phrase in a market, creating the topic
    (with its alias) when new. A hashtag-style run-together form
    ("latashawig") resolves to the spaced topic ("latasha wig")."""
    key = normalize(phrase)
    if not key:
        return None
    now = now or datetime.utcnow()
    coll = db["mi_market_topics"]
    hit = await coll.find_one({"market": market, "$or": [{"key": key}, {"aliases": key}, {"compact": compact(key)}]})
    if hit:
        updates = {"$set": {"last_seen": now}}
        if key != hit["key"]:
            updates["$addToSet"] = {"aliases": key}
        await coll.update_one({"_id": hit["_id"]}, updates)
        return hit["key"]
    result = await coll.update_one(
        {"market": market, "key": key},
        {"$setOnInsert": {"market": market, "key": key, "name": phrase.strip().lower().lstrip("#"),
                          "compact": compact(key), "aliases": [], "first_seen": now, "status": "observed"},
         "$set": {"last_seen": now}},
        upsert=True,
    )
    if result.upserted_id is not None:
        # Once per topic, not per post: every business subscribed to this
        # market reuses the same vector (personalize.py's semantic
        # relevance component). A failed embedding call just leaves this
        # topic without one — resolve() has already done its real job.
        vec = await embeddings.embed_text(key)
        if vec:
            await coll.update_one({"_id": result.upserted_id}, {"$set": {"embedding": vec}})
    return key


def bucket(ts: Optional[datetime]) -> Optional[datetime]:
    if not ts:
        return None
    return ts.replace(minute=0, second=0, microsecond=0)


async def count(db, market: str, topic_key: str, item: dict, reading: dict) -> None:
    b = bucket(item.get("published_at"))
    if not b:
        return
    m = item.get("raw_metrics") or {}
    engagement = int(m.get("likes", 0)) + 2 * int(m.get("replies", 0)) + 2 * int(m.get("reposts", 0))
    author = item.get("author_handle") or item["source_id"]
    add_to_set = {"authors": author}
    # Tracked separately from the overall author set so detect.py can baseline
    # complaint volume on its own (objection_shift, PRD §8.3) — a rise in
    # doubt/complaint can matter even when the topic's total mention volume
    # isn't trending at all.
    if reading["intent"] == "complaint":
        add_to_set["complaint_authors"] = author
    await db["mi_topic_counts"].update_one(
        {"market": market, "topic": topic_key, "bucket": b},
        {"$addToSet": add_to_set,
         "$inc": {"items": 1, "engagement": engagement, f"intents.{reading['intent']}": 1,
                  f"buying.{reading['buying_intent']}": 1},
         "$push": {"sample": {"$each": [item["source_id"]], "$slice": -20}}},
        upsert=True,
    )
    # PRD §8.1 topic graph: "edges capture... co-occurrence" — this post
    # resolved to more than one topic, so each is evidence the two are
    # related. Counted on this topic's own doc (an edge recorded from both
    # sides, independently, as each topic gets counted in turn).
    others = [t for t in (reading.get("topics") or []) if t and t != topic_key]
    if others:
        await db["mi_market_topics"].update_one(
            {"market": market, "key": topic_key},
            {"$inc": {f"co_occurs.{t}": 1 for t in others}},
        )
