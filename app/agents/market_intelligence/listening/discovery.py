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
                summary["candidates"].append(t["key"])

        elif status == "candidate":
            if (ev["accounts"] >= d["monitor_min_accounts"] and ev["days"] >= d["monitor_min_days"]
                    and ev["relevance"] >= d["monitor_min_relevance"]):
                learned.add(t["key"])
                await db["mi_market_topics"].update_one(
                    {"_id": t["_id"]}, {"$set": {"status": "monitored", "monitored_at": now, "low_yield_windows": 0}})
                summary["monitored"].append(t["key"])
            elif t.get("candidate_at") and now - t["candidate_at"] > timedelta(days=d["candidate_timeout_days"]):
                await db["mi_market_topics"].update_one(
                    {"_id": t["_id"]},
                    {"$set": {"status": "retired", "retired_at": now,
                             "retired_reason": "candidate timed out without enough evidence"}})
                summary["retired"].append(t["key"])

        elif status == "monitored":
            low = ev["accounts"] < d["monitor_min_accounts"] or ev["relevance"] < d["pause_min_relevance"]
            streak = int(t.get("low_yield_windows", 0)) + 1 if low else 0
            if low and streak >= d["pause_after_windows"]:
                learned.discard(t["key"])
                await db["mi_market_topics"].update_one(
                    {"_id": t["_id"]}, {"$set": {"status": "paused", "paused_at": now, "low_yield_windows": streak}})
                summary["paused"].append(t["key"])
            else:
                await db["mi_market_topics"].update_one({"_id": t["_id"]}, {"$set": {"low_yield_windows": streak}})

    if learned != before:
        await db["mi_markets"].update_one({"key": market["key"]}, {"$set": {"learned_terms": sorted(learned)}})
    return summary
