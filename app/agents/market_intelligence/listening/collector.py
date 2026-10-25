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

from . import enrich, markets, ontology, permissions, topics

# Bump when collection/counting rules change: markets then re-backfill.
# v2: measuring vs discovery searches; coverage and counts from measuring only.
COLLECTOR_VERSION = 2


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def spent_this_month(db, now: Optional[datetime] = None, source: Optional[str] = None) -> float:
    """Total listening spend this month — across every source by default, or
    scoped to one source when given (Phase A Step 6: a per-source budget
    check alongside the existing global ceiling, so one newly-approved
    source can't quietly eat the whole market's budget on its own)."""
    now = now or datetime.utcnow()
    q: dict = {"started_at": {"$gte": month_start(now)}}
    if source:
        q["source"] = source
    total = 0.0
    async for r in db["mi_listen_runs"].find(q, {"cost_usd": 1}):
        total += float(r.get("cost_usd") or 0)
    return total


async def _one_query(adapter, q: dict, excluded: list[str], since: datetime, until: datetime, per: int,
                     db=None, run_id: Optional[str] = None) -> dict:
    """Run one search to completion; returns its posts and coverage.

    db/run_id are optional and purely for live progress — when given, this
    writes its own completion into the run doc's per_query array the moment
    IT finishes, independent of the other queries running alongside it in
    the same gather(). No polling, no fabricated percentage: the UI shows
    exactly which real searches have finished and what they found, as they
    actually finish (spec §16: "show progress per source; do not invent a
    completion percentage")."""
    out = {"query": q["q"], "kind": q["kind"], "role": q.get("role", "sample"), "id": q.get("id"),
           "cap": per, "items": [], "cost": 0.0, "error": None, "capped": False}
    try:
        run_id_provider = await adapter.start_collection([q["q"]], excluded, since, until, items_per_query=per)
        out["run_id"] = run_id_provider
        cursor = None
        for _ in range(20):
            page = await adapter.fetch_page(run_id_provider, cursor)
            out["items"] += page.evidence
            if page.gaps:
                out["error"] = "; ".join(page.gaps)
            if not page.has_more or not page.next_cursor:
                break
            cursor = page.next_cursor
        out["cost"] = float(await adapter.actual_cost(run_id_provider) or 0)
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
    if db is not None and run_id is not None:
        await db["mi_listen_runs"].update_one(
            {"id": run_id, "per_query.id": out["id"]},
            {"$set": {
                "per_query.$.status": "done", "per_query.$.items": returned, "per_query.$.capped": out["capped"],
                "per_query.$.cost": out["cost"], "per_query.$.error": out["error"],
            }},
        )
    return out


async def collect_market(db, market: dict, cfg: dict, adapter, source: str, now: Optional[datetime] = None) -> dict:
    """Collect one market from one source once. Returns the run record.

    `source` is explicit (Phase A Step 5) rather than read from cfg["source"]
    — the caller (cycle.py) now loops over every approved source for this
    market, calling this once per (market, source) pair, each producing its
    own run doc. Everything below that used to read cfg["source"] now uses
    this parameter instead; cfg itself stays source-agnostic."""
    now = now or datetime.utcnow()
    run = {"id": str(uuid.uuid4()), "market": market["key"], "source": source, "started_at": now,
           "status": "running", "cost_usd": 0.0}

    # §15: "each connector has a permission record... approved before
    # production use." Not a policy statement someone has to remember to
    # check — a source with no approved record simply never collects.
    if not await permissions.is_approved(db, source):
        run.update(status="blocked_unapproved_source",
                   note=f"source '{source}' has no approved permission record (PRD §15)", finished_at=now)
        await db["mi_listen_runs"].update_one({"id": run["id"]}, {"$set": dict(run)}, upsert=True)
        return run

    spent = await spent_this_month(db, now)
    if spent >= cfg["monthly_budget_usd"]:
        run.update(status="skipped_budget", note=f"monthly listening budget reached (${spent:.2f} of "
                                                  f"${cfg['monthly_budget_usd']:.2f})", finished_at=now)
        await db["mi_listen_runs"].update_one({"id": run["id"]}, {"$set": dict(run)}, upsert=True)
        return run

    # A per-source sub-budget (optional, cfg["source_budgets"]) stops one
    # newly-approved source from quietly spending the whole market's global
    # budget and starving the others — the global ceiling above still
    # applies regardless, this is an additional, tighter cap when set.
    source_cap = (cfg.get("source_budgets") or {}).get(source)
    if source_cap is not None:
        source_spent = await spent_this_month(db, now, source=source)
        if source_spent >= source_cap:
            run.update(status="skipped_budget",
                       note=f"'{source}' monthly budget reached (${source_spent:.2f} of ${source_cap:.2f})",
                       finished_at=now)
            await db["mi_listen_runs"].update_one({"id": run["id"]}, {"$set": dict(run)}, upsert=True)
            return run

    category = await ontology.category(db, market["category_id"])
    if not category:
        run.update(status="failed", note=f"unknown category {market['category_id']}", finished_at=now)
        await db["mi_listen_runs"].update_one({"id": run["id"]}, {"$set": dict(run)}, upsert=True)
        return run
    patterns = await ontology.intent_patterns(db)
    plan = markets.compile_queries(category, market["country"], patterns, market.get("learned_terms") or [],
                                   cfg["max_queries_per_market"], source=source)

    # A market collected under older collection rules is backfilled again, so
    # its history is consistent with how it is counted now.
    last = market.get("last_collected_at") if market.get("collector_version") == COLLECTOR_VERSION else None
    since = (last - timedelta(hours=cfg["overlap_hours"])) if last else now - timedelta(days=cfg["backfill_days"])
    run.update(since=since, until=now, queries=[q["q"] for q in plan["queries"]], dropped_queries=plan["dropped"])

    # Written now, before a single query has finished, so a client polling
    # for "what's happening" sees the real query plan immediately — every
    # entry "pending" — rather than nothing at all until the whole run ends.
    # _one_query flips its own entry to "done" the moment IT personally
    # finishes (see its docstring); this doc is the one place live progress
    # actually lives, not a client-side simulation of one.
    run["per_query"] = [{"id": q.get("id"), "q": q["q"], "kind": q["kind"], "role": q.get("role", "sample"),
                         "status": "pending", "items": None, "capped": None, "cost": None, "error": None}
                        for q in plan["queries"]]
    await db["mi_listen_runs"].update_one({"id": run["id"]}, {"$set": dict(run)}, upsert=True)

    caps = market.get("query_caps") or {}
    results = await asyncio.gather(*[
        _one_query(adapter, q, plan["excluded"], since, now, int(caps.get(q["id"], cfg["items_per_query"])),
                  db=db, run_id=run["id"])
        for q in plan["queries"]])

    new_ids, total = [], 0
    for res in results:
        measured = res["role"] == "measure"
        for ev in res["items"]:
            total += 1
            doc = ev.dict()
            add = {"markets": market["key"], "found_by_queries": res.get("id")}
            if measured:
                add["measured"] = market["key"]  # found by a fully-read search: may be counted
            r = await db["mi_listen_items"].update_one(
                {"source_id": ev.source_id},
                {"$setOnInsert": {**doc, "first_run": run["id"]}, "$addToSet": add},
                upsert=True,
            )
            if r.upserted_id is not None:
                new_ids.append(ev.source_id)
            else:
                # already stored (another market or an overlap window): still needs reading for this market
                seen = await db["mi_listen_readings"].find_one({"source_id": ev.source_id, "market": market["key"]})
                # unread here, or read before only as a sample and now found by a measuring search
                if not seen or (measured and not seen.get("measured")):
                    new_ids.append(ev.source_id)

    # Coverage comes from the measuring searches only: the market counts as
    # fully read from the latest point every one of them reached.
    measure = [r for r in results if r["role"] == "measure"]
    covered = [r["covered_from"] for r in measure if r.get("covered_from")]
    ok = [r for r in results if not r["error"] or r["items"]]
    run.update(
        status="completed" if len(ok) == len(results) else ("partial" if ok else "failed"),
        finished_at=datetime.utcnow(),
        items_returned=total, new_items=len(set(new_ids)),
        cost_usd=round(sum(r["cost"] for r in results), 5),
        # Every search covered at least from here: the market's fully-read range is [covered_from, until].
        covered_from=max(covered) if covered and len(covered) == len(measure) else None,
        per_query=[{"q": r["query"], "kind": r["kind"], "role": r["role"], "cap": r["cap"], "items": len(r["items"]),
                    "capped": r["capped"], "covered_from": r.get("covered_from"), "cost": r["cost"],
                    "error": r["error"]} for r in results],
    )
    # A measuring search that filled its cap gets a bigger one next time (up
    # to the ceiling), so it can be read completely; still full at the ceiling
    # means it's too broad — flagged for splitting.
    new_caps, too_broad = dict(caps), []
    for r in measure:
        if r["capped"] and r.get("id"):
            nxt = min(int(r["cap"] * 2), cfg["max_items_per_query"])
            if nxt > r["cap"]:
                new_caps[r["id"]] = nxt
            else:
                too_broad.append(r["query"])
    await db["mi_markets"].update_one({"key": market["key"]},
                                      {"$set": {"query_caps": new_caps, "too_broad_queries": too_broad}})
    await db["mi_listen_runs"].update_one({"id": run["id"]}, {"$set": dict(run)}, upsert=True)
    if run["status"] != "failed":
        await db["mi_markets"].update_one({"key": market["key"]},
                                          {"$set": {"last_collected_at": now, "collector_version": COLLECTOR_VERSION}})
    run["to_read"] = list(dict.fromkeys(new_ids))
    return run


async def update_query_efficiency(db, market: dict, cfg: dict, now: Optional[datetime] = None) -> dict:
    """Query value optimisation (PRD §6.6): "spend collection budget where
    expected useful evidence per unit cost is highest."

    A full multi-armed bandit (ranking every candidate query by expected
    value, with a reserved exploration budget) needs richer per-query cost
    tracking than exists yet. This is the load-bearing first half of the
    same idea: every stored item now remembers which query/queries found
    it (collect_market's $addToSet on found_by_queries), so each query's
    own on-topic rate — the fraction of what it returns that genuinely
    belongs to this market, from AI readings already done for other
    reasons — can be measured directly. A query that's consistently
    low-yield gets its next-run item cap shrunk, freeing exactly the
    budget collect_market's existing cap-growth step would otherwise keep
    spending on it. Never shrunk to zero — min_cap keeps exploring it at a
    small scale rather than dropping it outright, the bandit's
    "exploration budget" in miniature.
    """
    e = cfg["efficiency"]
    now = now or datetime.utcnow()
    since = now - timedelta(days=e["window_days"])

    stats: dict[str, dict] = {}
    async for item in db["mi_listen_items"].find(
        {"markets": market["key"], "found_by_queries": {"$exists": True, "$ne": []},
         "published_at": {"$gte": since}},
        {"source_id": 1, "found_by_queries": 1},
    ):
        reading = await db["mi_listen_readings"].find_one(
            {"source_id": item["source_id"], "market": market["key"]}, {"on_topic": 1})
        if not reading:
            continue  # not read yet; judge it once it has been
        for qid in item.get("found_by_queries") or []:
            s = stats.setdefault(qid, {"items": 0, "on_topic": 0})
            s["items"] += 1
            if reading.get("on_topic"):
                s["on_topic"] += 1

    caps = dict(market.get("query_caps") or {})
    changed = False
    shrunk = []
    for qid, s in stats.items():
        if s["items"] < e["min_items_for_judgement"]:
            continue
        rate = s["on_topic"] / s["items"]
        if rate < e["min_on_topic_rate"]:
            cur = int(caps.get(qid) or cfg["items_per_query"])
            nxt = max(e["min_cap"], int(cur * e["shrink_factor"]))
            if nxt < cur:
                caps[qid] = nxt
                changed = True
                shrunk.append({"query_id": qid, "on_topic_rate": round(rate, 2), "items": s["items"], "cap": nxt})

    if changed:
        await db["mi_markets"].update_one({"key": market["key"]}, {"$set": {"query_caps": caps}})
    return {"shrunk": shrunk}


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
            measured = market["key"] in (item.get("measured") or [])
            reading = {
                "source_id": item["source_id"], "market": market["key"], "on_topic": rd.on_topic, "measured": measured,
                "intent": rd.intent.value, "need": rd.need, "sentiment": rd.sentiment.value,
                "buying_intent": rd.buying_intent.value, "topics": topic_keys, "confidence": rd.confidence,
                "event_date": rd.event_date,
                "published_at": item.get("published_at"), "author": item.get("author_handle"),
                "prompt_version": enrich.PROMPT_VERSION, "read_at": datetime.utcnow(),
            }
            await db["mi_listen_readings"].update_one({"source_id": item["source_id"], "market": market["key"]},
                                                      {"$set": reading}, upsert=True)
            if measured:  # only fully-read searches feed trend counts
                for key in topic_keys:
                    await topics.count(db, market["key"], key, item, reading)
                    stats["topics"] += 1
    return stats
