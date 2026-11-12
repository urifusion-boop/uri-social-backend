"""
Private-term collection (PRD §6.4-6.5).

A business's own brand name and its named competitors are "private terms":
unlike the category/intent/learned terms in markets.compile_queries() —
which build the SHARED queries every business subscribed to a market
benefits from — a private term belongs to exactly one tenant and must
never be searched for, or exposed, on behalf of any other business
(PRD §15: "private terms... are never exposed to another tenant").

Confirmed missing in a full module audit (Oct 2026): profile.build()
already computed and stored private_terms on every listening profile, but
nothing ever read them — Listening could not hear about a business's own
brand or its named competitors at all, and "competitor move" (PRD §8.3)
had no evidence to ever be detected from. This module closes that gap by
giving each business its own small, tenant-scoped collection pass,
separate from the shared per-market one in collector.py:

  * Queries are compiled per business (one per private term), never merged
    into a market's shared query plan.
  * Raw items are stored in the same mi_listen_items collection (just
    normalised post text/metadata — not tenant-sensitive on its own, and
    sharing storage lets an item already collected for a shared market
    avoid being fetched twice), but the AI reading of it is stored in
    mi_listen_private_readings, keyed by brand_id, and nothing outside this
    module ever queries that collection — so even though the raw post can
    be shared storage, what URI concluded about it for this business stays
    private to that business, exactly as the PRD requires.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from . import enrich, permissions
from .collector import _one_query, spent_this_month
from .markets import _q, query_id


def private_market_key(brand_id: str) -> str:
    """A synthetic, single-tenant 'market' key for competitor-move signals
    to live under — never written to mi_markets (no shared document, no
    subscribers list: the shared-collection machinery in markets.py never
    needs to know this key exists), just a stable identity so the existing
    signal/card shape (keyed by market+topic) and cards_for()'s query can
    both be reused as-is instead of forking a parallel schema."""
    return f"private:{brand_id}"


def compile_private_queries(private_terms_by_kind: dict[str, list[str]]) -> list[dict]:
    """One query per private term, tagged with which term it is and whether
    it's the business's own brand or a named competitor. Deliberately no
    category/intent expansion (there's no ontology entry for a brand name)
    — just the literal term, quoted if it contains a space."""
    queries = []
    seen = set()
    for kind, terms in private_terms_by_kind.items():
        for term in terms or []:
            t = (term or "").strip()
            if not t or t.lower() in seen:
                continue
            seen.add(t.lower())
            queries.append({"kind": kind, "term": t, "q": _q(t), "role": "measure",
                            "id": query_id(f"{kind}:{t.lower()}")})
    return queries


async def collect_private(db, profile: dict, cfg: dict, adapter, source: str,
                          now: Optional[datetime] = None) -> dict:
    """Collect this one business's private terms. Mirrors collector.
    collect_market()'s budget/approval gates (same permissions.is_approved
    and monthly-budget checks — private collection spends from the same
    ledger, it doesn't get a free pass) but is scoped to a single tenant
    and never touches mi_markets."""
    now = now or datetime.utcnow()
    brand_id = profile["brand_id"]
    run = {"id": str(uuid.uuid4()), "market": private_market_key(brand_id), "brand_id": brand_id,
           "source": source, "started_at": now, "status": "running", "cost_usd": 0.0, "private": True}

    if not await permissions.is_approved(db, source):
        run.update(status="blocked_unapproved_source",
                   note=f"source '{source}' has no approved permission record (PRD §15)", finished_at=now)
        await db["mi_listen_runs"].update_one({"id": run["id"]}, {"$set": dict(run)}, upsert=True)
        return run

    spent = await spent_this_month(db, now)
    if spent >= cfg["monthly_budget_usd"]:
        run.update(status="skipped_budget",
                   note=f"monthly listening budget reached (${spent:.2f} of ${cfg['monthly_budget_usd']:.2f})",
                   finished_at=now)
        await db["mi_listen_runs"].update_one({"id": run["id"]}, {"$set": dict(run)}, upsert=True)
        return run

    facts = profile.get("facts") or {}
    own = [facts["brand_name"]] if facts.get("brand_name") else []
    competitors = list(facts.get("competitors") or [])
    queries = compile_private_queries({"own_brand": own, "competitor": competitors})
    if not queries:
        run.update(status="completed", finished_at=now, items_returned=0, new_items=0)
        await db["mi_listen_runs"].update_one({"id": run["id"]}, {"$set": dict(run)}, upsert=True)
        run["to_read"] = []
        return run

    pc = cfg.get("private") or {}
    since = now - timedelta(days=pc.get("backfill_days", 3))
    cap = pc.get("items_per_query", 25)
    results = [await _one_query(adapter, q, [], since, now, cap) for q in queries]

    to_read: list[tuple[str, str, str]] = []
    for q, res in zip(queries, results):
        for ev in res["items"]:
            doc = ev.dict()
            await db["mi_listen_items"].update_one(
                {"source_id": ev.source_id},
                {"$setOnInsert": {**doc, "first_run": run["id"]}},
                upsert=True,
            )
            seen = await db["mi_listen_private_readings"].find_one(
                {"source_id": ev.source_id, "brand_id": brand_id}, {"_id": 1})
            if not seen:
                to_read.append((ev.source_id, q["term"], q["kind"]))

    ok = [r for r in results if not r["error"] or r["items"]]
    run.update(
        status="completed" if len(ok) == len(results) else ("partial" if ok else "failed"),
        finished_at=datetime.utcnow(),
        items_returned=sum(len(r["items"]) for r in results),
        new_items=len(to_read),
        cost_usd=round(sum(r["cost"] for r in results), 5),
        queries=[q["q"] for q in queries],
    )
    await db["mi_listen_runs"].update_one({"id": run["id"]}, {"$set": dict(run)}, upsert=True)
    run["to_read"] = list(dict.fromkeys(to_read))
    return run


async def read_private(db, profile: dict, to_read: list[tuple[str, str, str]], cfg: dict) -> dict:
    """AI-read and classify newly-collected private items. Stored in
    mi_listen_private_readings, scoped to this brand_id only — never joined
    into the shared mi_listen_readings a market's other subscribers read
    from, and never queried by anything outside this module and
    competitor_signals.py."""
    brand_id = profile["brand_id"]
    stats = {"read": 0, "on_topic": 0}
    if not to_read:
        return stats
    size = cfg["enrich_batch_size"]
    brand_name = (profile.get("facts") or {}).get("brand_name") or "this business"
    label = f"mentions of {brand_name} or its named competitors (not just the category in general)"
    for i in range(0, len(to_read), size):
        chunk = to_read[i:i + size]
        ids = [c[0] for c in chunk]
        term_by_id = {c[0]: (c[1], c[2]) for c in chunk}
        items = [d async for d in db["mi_listen_items"].find({"source_id": {"$in": ids}})]
        readings = await enrich.read_batch(label, items, cfg["enrich_model"])
        for idx, item in enumerate(items):
            rd = readings.get(idx)
            if rd is None:
                continue
            stats["read"] += 1
            term, kind = term_by_id.get(item["source_id"], ("", ""))
            if rd.on_topic:
                stats["on_topic"] += 1
            reading = {
                "source_id": item["source_id"], "brand_id": brand_id, "term": term, "term_kind": kind,
                "on_topic": rd.on_topic, "intent": rd.intent.value, "need": rd.need,
                "sentiment": rd.sentiment.value, "buying_intent": rd.buying_intent.value,
                "confidence": rd.confidence, "published_at": item.get("published_at"),
                "author": item.get("author_handle"), "prompt_version": enrich.PROMPT_VERSION,
                "read_at": datetime.utcnow(),
            }
            await db["mi_listen_private_readings"].update_one(
                {"source_id": item["source_id"], "brand_id": brand_id},
                {"$set": reading}, upsert=True)
    return stats
