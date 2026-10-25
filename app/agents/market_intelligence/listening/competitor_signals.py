"""
Competitor-move signal (PRD §8.3): "Observable change in a competitor's
offer or messaging" -> "Differentiation idea (never copying)".

Deliberately evidence-based, not statistical, unlike detect.py's
rising/spike/recurring_need signals. Those are built for a SHARED market
with many businesses' worth of volume behind them, so a 28-day median/MAD
baseline is meaningful. A single business's own competitor-mention stream
has no such volume — a real competitor launch might be two or three posts,
total, ever. Forcing that through a trend apparatus sized for shared data
would mean it could never qualify. Instead this follows the PRD's own
"evidence or silence" principle directly: a handful of genuine, on-topic
posts naming a competitor within a recent window IS the signal.

Depends on private_collect.py having already collected and classified
posts mentioning this business's named competitors (PRD §6.4's "private
terms", confirmed missing before this and private_collect.py together).
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

from . import permissions
from .detect import commercial_score
from .private_collect import private_market_key


async def detect_competitor_moves(db, profile: dict, cfg: dict, now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.utcnow()
    d = cfg.get("competitor_signal") or {}
    min_authors = d.get("min_authors", 2)
    window_days = d.get("window_days", 7)
    brand_id = profile["brand_id"]
    market = private_market_key(brand_id)
    since = now - timedelta(days=window_days)

    by_term: dict[str, list[dict]] = {}
    async for r in db["mi_listen_private_readings"].find({
        "brand_id": brand_id, "term_kind": "competitor", "on_topic": True,
        "published_at": {"$gte": since},
    }):
        by_term.setdefault(r["term"], []).append(r)

    signals: list[dict] = []
    for term, rows in by_term.items():
        author_counts = Counter(r.get("author") or r["source_id"] for r in rows)
        authors = set(author_counts)
        if len(authors) < min_authors:
            continue
        top_author_share = author_counts.most_common(1)[0][1] / len(rows)
        if top_author_share > cfg["detect"]["max_top_author_share"]:
            continue

        source_ids = [r["source_id"] for r in rows]
        items = {i["source_id"]: i async for i in db["mi_listen_items"].find(
            {"source_id": {"$in": source_ids}},
            {"source_id": 1, "url": 1, "text": 1, "published_at": 1, "author_handle": 1, "provider": 1})}
        ranked = sorted(items.values(), key=lambda i: i.get("published_at") or now, reverse=True)

        dr_cache: dict[str, dict] = {}

        async def dr_for(provider: Optional[str]) -> dict:
            key = provider or cfg.get("source", "x")
            if key not in dr_cache:
                dr_cache[key] = await permissions.display_rules(db, key)
            return dr_cache[key]

        samples = []
        for i in ranked:
            dr = await dr_for(i.get("provider"))
            if dr.get("require_link", True) and not i.get("url"):
                continue
            max_chars = int(dr.get("max_excerpt_chars") or 280)
            samples.append({"url": i.get("url"), "text": (i.get("text") or "")[:max_chars],
                            "author": i.get("author_handle"), "published_at": i.get("published_at"),
                            "platform": i.get("provider")})
            if len(samples) >= 5:
                break
        if not samples:
            continue  # nothing we're allowed to show as evidence: no card without it

        intents = Counter(r["intent"] for r in rows)
        buying = Counter(r["buying_intent"] for r in rows)
        needs = Counter((r.get("need") or "").strip().lower() for r in rows if r.get("need"))
        reading_conf = sum(r.get("confidence", 0) for r in rows) / len(rows)
        # Simple, bounded confidence: more independent authors and surer
        # readings raise it; no history-length term (there is no baseline
        # history for this kind of signal to be long or short).
        confidence = round(min(1.0, len(authors) / (2 * min_authors)) * 0.6 + reading_conf * 0.4, 3)

        sig = {
            "market": market, "market_label": "competitor activity", "topic": f"competitor:{term.lower()}",
            "topic_name": term, "aliases": [], "type": "competitor_move",
            "day": now.strftime("%Y-%m-%d"), "window_start": since, "window_end": now,
            "posts": len(rows), "intent_mix": dict(intents), "buying_mix": dict(buying),
            "needs": [n for n, _ in needs.most_common(3)],
            "top_author_share": round(top_author_share, 2), "reading_confidence": round(reading_conf, 2),
            "samples": samples, "status": "active", "computed_at": now,
            "expires_at": now + timedelta(days=cfg["score"]["card_ttl_days"]),
            "current": {"authors": len(authors), "posts": len(rows), "window_days": window_days},
            "baseline": None, "z": 0.0, "growth": 0.0, "persistence_days": 0,
            "scores": {"M": 55.0, "C": commercial_score(dict(intents), dict(buying))},
            "confidence": confidence,
        }
        signals.append(sig)

    for s in signals:
        await db["mi_listen_signals"].update_one(
            {"market": s["market"], "topic": s["topic"], "type": s["type"], "day": s["day"]},
            {"$set": s, "$setOnInsert": {"id": str(uuid.uuid4()), "created_at": now}}, upsert=True)
    return signals
