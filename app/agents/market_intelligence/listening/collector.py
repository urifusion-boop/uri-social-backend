"""
Collecting a market (spec §6) and reading what was collected.

Each of the market's searches runs as its own provider run, in parallel. X
returns newest first, so when a search hits its item cap, everything newer
than its oldest returned post was read completely; when it doesn't, the whole
requested window was. That boundary is stored per run as `covered_from`, so
counting and trend maths only ever use hours that were genuinely covered —
"no posts" is never confused with "not checked" (spec §6, §15).

Posts are stored once (mi_listen_items) and shared by every market and
business they belong to; each is read by AI once per market.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Optional

from . import enrich, markets, ontology, topics


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def spent_this_month(db, now: Optional[datetime] = None) -> float:
    now = now or datetime.utcnow()
    total = 0.0
    async for r in db["mi_listen_runs"].find({"started_at": {"$gte": month_start(now)}}, {"cost_usd": 1}):
        total += float(r.get("cost_usd") or 0)
    return total


async def _one_query(adapter, q: dict, excluded: list[str], since: datetime, until: datetime, per: int) -> dict:
    """Run one search to completion; returns its posts and coverage."""
    out = {"query": q["q"], "kind": q["kind"], "items": [], "cost": 0.0, "error": None, "capped": False}
    try:
        run_id = await adapter.start_collection([q["q"]], excluded, since, until, items_per_query=per)
        out["run_id"] = run_id
        cursor = None
        for _ in range(20):
            page = await adapter.fetch_page(run_id, cursor)
            out["items"] += page.evidence
            if page.gaps:
                out["error"] = "; ".join(page.gaps)
            if not page.has_more or not page.next_cursor:
                break
            cursor = page.next_cursor
        out["cost"] = float(await adapter.actual_cost(run_id) or 0)
    except Exception as e:
        out["error"] = str(e)[:300]
    returned = len(out["items"])
    out["capped"] = returned >= int(per * 0.9)
    pubs = [e.published_at for e in out["items"] if e.published_at]
    if out["error"] and not returned:
        out["covered_from"] = None          # not checked
    elif out["capped"] and pubs:
        out["covered_from"] = min(pubs)     # read completely from its oldest post onward
    else:
        out["covered_from"] = since         # read the whole window
    return out


async def collect_market(db, market: dict, cfg: dict, adapter, now: Optional[datetime] = None) -> dict:
    """Collect one market once. Returns the run record."""
    now = now or datetime.utcnow()
    run = {"id": str(uuid.uuid4()), "market": market["key"], "source": cfg["source"], "started_at": now,
           "status": "running", "cost_usd": 0.0}

    spent = await spent_this_month(db, now)
    if spent >= cfg["monthly_budget_usd"]:
        run.update(status="skipped_budget", note=f"monthly listening budget reached (${spent:.2f} of "
                                                  f"${cfg['monthly_budget_usd']:.2f})", finished_at=now)
        await db["mi_listen_runs"].insert_one(dict(run))
        return run

    category = await ontology.category(db, market["category_id"])
    if not category:
        run.update(status="failed", note=f"unknown category {market['category_id']}", finished_at=now)
        await db["mi_listen_runs"].insert_one(dict(run))
        return run
    patterns = await ontology.intent_patterns(db)
    plan = markets.compile_queries(category, market["country"], patterns, market.get("learned_terms") or [],
                                   cfg["max_queries_per_market"])

    last = market.get("last_collected_at")
    since = (last - timedelta(hours=cfg["overlap_hours"])) if last else now - timedelta(days=cfg["backfill_days"])
    run.update(since=since, until=now, queries=[q["q"] for q in plan["queries"]], dropped_queries=plan["dropped"])

    results = await asyncio.gather(*[
        _one_query(adapter, q, plan["excluded"], since, now, cfg["items_per_query"]) for q in plan["queries"]])

    new_ids, total = [], 0
    for res in results:
        for ev in res["items"]:
            total += 1
            doc = ev.dict()
            r = await db["mi_listen_items"].update_one(
                {"source_id": ev.source_id},
                {"$setOnInsert": {**doc, "first_run": run["id"]}, "$addToSet": {"markets": market["key"]}},
                upsert=True,
            )
            if r.upserted_id is not None:
                new_ids.append(ev.source_id)
            else:
                # already stored (another market or an overlap window): still needs reading for this market
                seen = await db["mi_listen_readings"].find_one({"source_id": ev.source_id, "market": market["key"]})
                if not seen:
                    new_ids.append(ev.source_id)

    covered = [r["covered_from"] for r in results if r.get("covered_from")]
    ok = [r for r in results if not r["error"] or r["items"]]
    run.update(
        status="completed" if len(ok) == len(results) else ("partial" if ok else "failed"),
        finished_at=datetime.utcnow(),
        items_returned=total, new_items=len(set(new_ids)),
        cost_usd=round(sum(r["cost"] for r in results), 5),
        # Every search covered at least from here: the market's fully-read range is [covered_from, until].
        covered_from=max(covered) if covered and len(covered) == len(results) else None,
        per_query=[{"q": r["query"], "kind": r["kind"], "items": len(r["items"]), "capped": r["capped"],
                    "covered_from": r.get("covered_from"), "cost": r["cost"], "error": r["error"]} for r in results],
    )
    await db["mi_listen_runs"].insert_one(dict(run))
    if run["status"] != "failed":
        await db["mi_markets"].update_one({"key": market["key"]}, {"$set": {"last_collected_at": now}})
    run["to_read"] = list(dict.fromkeys(new_ids))
    return run


async def read_and_count(db, market: dict, source_ids: list[str], cfg: dict) -> dict:
    """AI-read new posts for this market in batches, then count relevant ones
    into topic buckets."""
    stats = {"read": 0, "on_topic": 0, "topics": 0, "unread": 0}
    size = cfg["enrich_batch_size"]
    for i in range(0, len(source_ids), size):
        ids = source_ids[i:i + size]
        items = [d async for d in db["mi_listen_items"].find({"source_id": {"$in": ids}})]
        readings = await enrich.read_batch(market["label"], items, cfg["enrich_model"])
        for idx, item in enumerate(items):
            rd = readings.get(idx)
            if rd is None:
                stats["unread"] += 1
                continue
            stats["read"] += 1
            topic_keys = []
            if rd.on_topic and rd.intent.value != "seller_promo":
                stats["on_topic"] += 1
                for phrase in rd.topics[:3]:
                    key = await topics.resolve(db, market["key"], phrase)
                    if key and key not in topic_keys:
                        topic_keys.append(key)
            reading = {
                "source_id": item["source_id"], "market": market["key"], "on_topic": rd.on_topic,
                "intent": rd.intent.value, "need": rd.need, "sentiment": rd.sentiment.value,
                "buying_intent": rd.buying_intent.value, "topics": topic_keys, "confidence": rd.confidence,
                "published_at": item.get("published_at"), "author": item.get("author_handle"),
                "prompt_version": enrich.PROMPT_VERSION, "read_at": datetime.utcnow(),
            }
            await db["mi_listen_readings"].update_one({"source_id": item["source_id"], "market": market["key"]},
                                                      {"$set": reading}, upsert=True)
            for key in topic_keys:
                await topics.count(db, market["key"], key, item, reading)
                stats["topics"] += 1
    return stats
