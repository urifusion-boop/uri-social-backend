"""
Keyword discovery lifecycle (PRD §6.3, Layer C).

A phrase people actually use starts life as a topic with status "observed"
(topics.resolve already does this) — counted, but never searched for. This
module is what moves it through the rest of the lifecycle, purely from its
own evidence:

  observed  -> candidate   enough independent accounts, genuinely on-topic
  candidate -> monitored   clears a higher bar -> added to the market's own
                           searches (market["learned_terms"]), so everyone
                           subscribed to that market benefits
  candidate -> retired     never clears the bar within the timeout window
  monitored -> paused      yield drops for several windows in a row ->
                           dropped from learned_terms, but still counted if
                           still seen (unlike retired)

No curation console exists yet, so promotion/demotion here is fully
automatic against configured thresholds (spec's "approved by curator/owner"
path is simply editing a topic's status by hand until one exists). Paused
topics are intentionally NOT re-evaluated here — reviving a paused term is
a deliberate follow-up, not silent flapping.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

VALID_STATUSES = {"observed", "candidate", "monitored", "paused", "retired"}


async def _record_history(db, market_key: str, topic_key: str, old_status: Optional[str], new_status: str,
                          reason: Optional[str], actor: str, now: datetime) -> None:
    """Appendix A's term_history entity: 'old -> new state, reason, actor,
    timestamp'. Every status transition gets its own row — the topic doc
    itself only ever holds the CURRENT state (overwritten on each change),
    so without this a topic promoted, paused and re-promoted loses every
    earlier decision. Append-only; never read back to decide anything, so
    it can't itself introduce a race with update_market/set_topic_status."""
    await db["mi_term_history"].insert_one({
        "market": market_key, "topic": topic_key, "old_status": old_status, "new_status": new_status,
        "reason": (reason or "")[:300] or None, "actor": actor, "at": now,
    })


async def _window_evidence(db, market: str, topic: str, since: datetime, until: datetime) -> dict:
    accounts: set = set()
    days: set = set()
    on_topic = 0
    total = 0
    async for r in db["mi_listen_readings"].find(
        {"market": market, "topics": topic, "published_at": {"$gte": since, "$lt": until}}
    ):
        total += 1
        if r.get("on_topic"):
            on_topic += 1
            accounts.add(r.get("author") or r["source_id"])
            days.add(r["published_at"].strftime("%Y-%m-%d"))
    return {"accounts": len(accounts), "days": len(days),
            "relevance": (on_topic / total) if total else 0.0}


async def update_market(db, market: dict, cfg: dict, now: Optional[datetime] = None) -> dict:
    d = cfg["discovery"]
    now = now or datetime.utcnow()
    since = now - timedelta(days=d["window_days"])
    summary: dict = {"candidates": [], "monitored": [], "retired": [], "paused": []}
    learned = set(market.get("learned_terms") or [])
    before = set(learned)

    async for t in db["mi_market_topics"].find(
        {"market": market["key"], "status": {"$in": ["observed", "candidate", "monitored"]}}
    ):
        ev = await _window_evidence(db, market["key"], t["key"], since, now)
        status = t["status"]

        if status == "observed":
            if ev["accounts"] >= d["candidate_min_accounts"] and ev["relevance"] >= d["candidate_min_relevance"]:
                await db["mi_market_topics"].update_one(
                    {"_id": t["_id"]}, {"$set": {"status": "candidate", "candidate_at": now}})
                await _record_history(db, market["key"], t["key"], "observed", "candidate",
                                      f"{ev['accounts']} accounts, {ev['relevance']:.2f} relevance", "system", now)
                summary["candidates"].append(t["key"])

        elif status == "candidate":
            if (ev["accounts"] >= d["monitor_min_accounts"] and ev["days"] >= d["monitor_min_days"]
                    and ev["relevance"] >= d["monitor_min_relevance"]):
                learned.add(t["key"])
                await db["mi_market_topics"].update_one(
                    {"_id": t["_id"]}, {"$set": {"status": "monitored", "monitored_at": now, "low_yield_windows": 0}})
                await _record_history(db, market["key"], t["key"], "candidate", "monitored",
                                      f"{ev['accounts']} accounts, {ev['days']} days, {ev['relevance']:.2f} relevance",
                                      "system", now)
                summary["monitored"].append(t["key"])
            elif t.get("candidate_at") and now - t["candidate_at"] > timedelta(days=d["candidate_timeout_days"]):
                await db["mi_market_topics"].update_one(
                    {"_id": t["_id"]},
                    {"$set": {"status": "retired", "retired_at": now,
                             "retired_reason": "candidate timed out without enough evidence"}})
                await _record_history(db, market["key"], t["key"], "candidate", "retired",
                                      "candidate timed out without enough evidence", "system", now)
                summary["retired"].append(t["key"])

        elif status == "monitored":
            low = ev["accounts"] < d["monitor_min_accounts"] or ev["relevance"] < d["pause_min_relevance"]
            streak = int(t.get("low_yield_windows", 0)) + 1 if low else 0
            if low and streak >= d["pause_after_windows"]:
                learned.discard(t["key"])
                await db["mi_market_topics"].update_one(
                    {"_id": t["_id"]}, {"$set": {"status": "paused", "paused_at": now, "low_yield_windows": streak}})
                await _record_history(db, market["key"], t["key"], "monitored", "paused",
                                      f"low yield for {streak} consecutive windows", "system", now)
                summary["paused"].append(t["key"])
            else:
                await db["mi_market_topics"].update_one({"_id": t["_id"]}, {"$set": {"low_yield_windows": streak}})

    if learned != before:
        await db["mi_markets"].update_one({"key": market["key"]}, {"$set": {"learned_terms": sorted(learned)}})
    return summary


async def set_topic_status(db, market_key: str, topic_key: str, status: str, reason: Optional[str] = None,
                           actor: str = "curator", now: Optional[datetime] = None) -> dict:
    """A curator or brand owner's manual override of a topic's lifecycle
    state — PRD §6.3's "approved by curator/owner" path for promoting a
    candidate straight to monitored, or demoting/retiring one directly,
    without waiting for (or instead of) the automatic evidence-based
    thresholds in update_market(). Carries the same learned_terms side
    effect the automatic path has: "monitored" adds the topic to the
    market's own searches, every other status removes it."""
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    now = now or datetime.utcnow()
    topic = await db["mi_market_topics"].find_one({"market": market_key, "key": topic_key})
    if not topic:
        return {"found": False}

    await db["mi_market_topics"].update_one(
        {"_id": topic["_id"]},
        {"$set": {"status": status, f"{status}_at": now, "decided_by": actor,
                 "decision_reason": (reason or "")[:300] or None}},
    )
    await _record_history(db, market_key, topic_key, topic.get("status"), status, reason, actor, now)
    market = await db["mi_markets"].find_one({"key": market_key}) or {}
    before = set(market.get("learned_terms") or [])
    learned = set(before)
    if status == "monitored":
        learned.add(topic_key)
    else:
        learned.discard(topic_key)
    if learned != before:
        await db["mi_markets"].update_one({"key": market_key}, {"$set": {"learned_terms": sorted(learned)}})
    return {"found": True, "status": status, "market": market_key, "topic": topic_key}


async def term_history(db, market_key: str, topic_key: str, limit: int = 100) -> list[dict]:
    """Every lifecycle transition a term has ever made, most recent first —
    the curation console's audit trail for a topic (Appendix A's
    term_history). The topic doc itself only ever holds current state."""
    out = []
    async for h in db["mi_term_history"].find(
        {"market": market_key, "topic": topic_key}
    ).sort([("at", -1)]).limit(min(limit, 500)):
        h.pop("_id", None)
        out.append(h)
    return out
