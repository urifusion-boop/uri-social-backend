"""
Signal detection (spec §7.2, PRD §8.2-8.3). Deterministic maths only — the AI
never produces a number.

Measure: distinct accounts talking about a topic, per day, counted only over
hours the collection actually covered, expressed as a per-covered-day rate so
partial days compare fairly. Baseline: median and MAD of earlier covered days
(a covered day with no posts is a real zero). Then:
  rising     — well above baseline (z and growth) and elevated on enough days in a row
  spike      — well above baseline today but not yet persistent
  emerging   — active now, but not enough history to call it a trend
  new_entity — a topic first seen in the last few days that is already broad
  recurring_need — a steady question/complaint/request from many people across many days
  objection_shift — same baseline/z maths as rising, but measured only on the topic's
                    complaint-intent authors, so a rise in doubt/complaint volume is caught
                    even when the topic's overall mention volume isn't trending
  cooling    — a topic that had a live rising/spike/new_entity signal has since fallen back
               toward or below its own baseline (PRD §8.3: "momentum fading... stop
               recommending; expire cards") — the prior signal and any live card for it are
               expired immediately rather than left to run out their normal TTL
Breadth guard: one account can't make a signal (top-author share cap).
"""
from __future__ import annotations

import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

from . import permissions

NEED_INTENTS = {"question", "complaint", "recommendation", "purchase"}
COMMERCIAL = {"purchase", "recommendation"}


def hour_floor(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


async def covered_hours(db, market: str, start: datetime, end: datetime) -> set[datetime]:
    """Hours in [start, end) that a successful run read completely."""
    hours: set[datetime] = set()
    async for r in db["mi_listen_runs"].find({"market": market, "status": {"$in": ["completed", "partial"]},
                                              "covered_from": {"$ne": None}, "until": {"$gte": start}}):
        a = max(hour_floor(r["covered_from"]), hour_floor(start))
        b = min(r["until"], end)
        h = a
        while h < b:
            hours.add(h)
            h += timedelta(hours=1)
    return hours


def robust_baseline(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    return med, mad


def momentum_score(kind: str, z: float, growth: float) -> float:
    if kind == "rising":
        return min(100.0, 55 + 9 * z)
    if kind == "objection_shift":
        return min(90.0, 50 + 8 * z)
    if kind == "spike":
        return min(85.0, 45 + 7 * z)
    if kind == "new_entity":
        return 65.0
    if kind == "emerging":
        return min(60.0, 35 + 10 * max(growth, 0))
    if kind == "cooling":
        return 10.0  # momentum is, by definition, fading — never worth promoting
    return 30.0  # recurring_need: steady, not trending


def commercial_score(intents: dict, buying: dict) -> float:
    total = sum(intents.values()) or 1
    share = sum(v for k, v in intents.items() if k in COMMERCIAL) / total
    high = (buying.get("high", 0) + 0.5 * buying.get("medium", 0)) / total
    return round(min(100.0, 100 * (0.7 * share + 0.3 * high) + 10), 1)


def confidence(authors: int, min_authors: int, coverage: float, history_days: int, min_history: int,
               reading_conf: float) -> float:
    return round(min(1.0, authors / (3 * min_authors)) * 0.4 + coverage * 0.25
                 + min(1.0, history_days / max(min_history, 1)) * 0.2 + reading_conf * 0.15, 3)


async def detect_market(db, market: dict, cfg: dict, now: Optional[datetime] = None, persist: bool = True) -> list[dict]:
    d = cfg["detect"]
    now = hour_floor(now or datetime.utcnow())
    days = d["baseline_days"]
    start = now - timedelta(days=days + 1)
    cov = await covered_hours(db, market["key"], start, now)

    # day windows: k=0 is the last 24h
    def window(k: int) -> tuple[datetime, datetime]:
        return now - timedelta(hours=24 * (k + 1)), now - timedelta(hours=24 * k)

    win_cov = {}
    for k in range(days + 1):
        a, b = window(k)
        win_cov[k] = sum(1 for h in cov if a <= h < b)

    per_topic: dict[str, dict[int, set]] = defaultdict(lambda: defaultdict(set))
    per_topic_complaint: dict[str, dict[int, set]] = defaultdict(lambda: defaultdict(set))
    async for c in db["mi_topic_counts"].find({"market": market["key"], "bucket": {"$gte": start, "$lt": now}}):
        if c["bucket"] not in cov:
            continue
        k = int((now - c["bucket"]).total_seconds() // (24 * 3600))
        if 0 <= k <= days:
            per_topic[c["topic"]][k].update(c.get("authors") or [])
            per_topic_complaint[c["topic"]][k].update(c.get("complaint_authors") or [])

    market_age_days = (now - (market.get("created_at") or now)).days
    signals: list[dict] = []
    for topic, by_day in per_topic.items():
        cur_cov = win_cov.get(0, 0)
        if cur_cov == 0:
            continue
        cur_authors = len(by_day.get(0, set()))
        rate = lambda k: len(by_day.get(k, set())) * 24.0 / max(win_cov.get(k, 0), 1)
        history = [k for k in range(1, days + 1) if win_cov.get(k, 0) >= 24 * d["min_coverage"]]
        base_vals = [rate(k) for k in history]
        med, mad = robust_baseline(base_vals)
        cur_rate = rate(0)
        z = (cur_rate - med) / max(1.4826 * mad, 1.0)
        growth = (cur_rate + 1) / (med + 1) - 1
        persistence = 0
        for k in range(0, days + 1):
            if win_cov.get(k, 0) and rate(k) >= max(med * (1 + d["rising_min_growth"] / 2), d["min_authors"] / 2):
                persistence += 1
            else:
                break

        kind = None
        if cur_authors >= d["min_authors"]:
            if len(history) < d["min_history_days"]:
                kind = "emerging" if cur_rate > med or not history else None
            elif z >= d["rising_z"] and growth >= d["rising_min_growth"]:
                kind = "rising" if persistence >= d["persistence_days"] else "spike"
            topic_doc = await db["mi_market_topics"].find_one({"market": market["key"], "key": topic})
            first_seen = (topic_doc or {}).get("first_seen")
            if (not kind or kind == "emerging") and first_seen and market_age_days >= cfg["backfill_days"] + d["new_entity_days"] \
                    and now - first_seen <= timedelta(days=d["new_entity_days"]):
                kind = "new_entity"
        if kind:
            sig = await _evidence(db, market, topic, kind, now - timedelta(hours=24), now, cfg)
            if sig and sig["top_author_share"] <= d["max_top_author_share"]:
                sig.update(current={"authors": cur_authors, "rate_per_day": round(cur_rate, 1),
                                    "coverage": round(cur_cov / 24, 2)},
                           baseline={"median_per_day": round(med, 1), "mad": round(mad, 1), "days": len(history)},
                           z=round(z, 2), growth=round(growth, 2), persistence_days=persistence)
                sig["scores"] = {"M": momentum_score(kind, z, growth),
                                 "C": commercial_score(sig["intent_mix"], sig["buying_mix"])}
                sig["confidence"] = confidence(cur_authors, d["min_authors"], cur_cov / 24, len(history),
                                               d["min_history_days"], sig["reading_confidence"])
                signals.append(sig)

        # objection_shift: same baseline maths, but on the topic's complaint-
        # intent authors only — a rise in doubt/complaint can matter even
        # when the topic's overall mention volume isn't trending at all.
        by_day_c = per_topic_complaint.get(topic, {})
        cur_c_authors = len(by_day_c.get(0, set()))
        rate_c = lambda k: len(by_day_c.get(k, set())) * 24.0 / max(win_cov.get(k, 0), 1)
        base_c_vals = [rate_c(k) for k in history]
        med_c, mad_c = robust_baseline(base_c_vals)
        cur_rate_c = rate_c(0)
        z_c = (cur_rate_c - med_c) / max(1.4826 * mad_c, 1.0)
        growth_c = (cur_rate_c + 1) / (med_c + 1) - 1
        if cur_c_authors >= d["min_authors"] and len(history) >= d["min_history_days"] \
                and z_c >= d["objection_z"] and growth_c >= d["objection_min_growth"]:
            obj_sig = await _evidence(db, market, topic, "objection_shift", now - timedelta(hours=24), now, cfg,
                                      intents={"complaint"})
            if obj_sig and obj_sig["top_author_share"] <= d["max_top_author_share"]:
                obj_sig.update(current={"authors": cur_c_authors, "rate_per_day": round(cur_rate_c, 1),
                                        "coverage": round(cur_cov / 24, 2)},
                              baseline={"median_per_day": round(med_c, 1), "mad": round(mad_c, 1), "days": len(history)},
                              z=round(z_c, 2), growth=round(growth_c, 2), persistence_days=0)
                obj_sig["scores"] = {"M": momentum_score("objection_shift", z_c, growth_c),
                                     "C": commercial_score(obj_sig["intent_mix"], obj_sig["buying_mix"])}
                obj_sig["confidence"] = confidence(cur_c_authors, d["min_authors"], cur_cov / 24, len(history),
                                                   d["min_history_days"], obj_sig["reading_confidence"])
                signals.append(obj_sig)

        # cooling (PRD §8.3: "momentum fading... stop recommending; expire
        # cards"): a topic that still has a live rising/spike/new_entity
        # signal, but no longer qualifies this run and has fallen back
        # toward or below its own baseline. The prior signal and any live
        # card for it are expired right away rather than left to run out
        # their normal TTL — a stale "this is trending" card shouldn't
        # outlive the trend it was about.
        if kind not in ("rising", "spike") and z < d["rising_z"] and growth <= 0:
            prior = await db["mi_listen_signals"].find_one(
                {"market": market["key"], "topic": topic, "type": {"$in": ["rising", "spike", "new_entity"]},
                 "status": "active", "expires_at": {"$gt": now}})
            if prior:
                await db["mi_listen_signals"].update_one({"_id": prior["_id"]}, {"$set": {"expires_at": now}})
                await db["mi_listen_cards"].update_many(
                    {"market": market["key"], "topic": topic, "status": {"$in": ["new", "shown", "saved"]}},
                    {"$set": {"expires_at": now}})
                cool_sig = await _evidence(db, market, topic, "cooling", now - timedelta(hours=24), now, cfg)
                if cool_sig:
                    cool_sig.update(current={"authors": cur_authors, "rate_per_day": round(cur_rate, 1),
                                             "coverage": round(cur_cov / 24, 2),
                                             "peak_authors": (prior.get("current") or {}).get("authors")},
                                   baseline={"median_per_day": round(med, 1), "mad": round(mad, 1), "days": len(history)},
                                   z=round(z, 2), growth=round(growth, 2), persistence_days=0)
                    cool_sig["scores"] = {"M": momentum_score("cooling", z, growth),
                                         "C": commercial_score(cool_sig["intent_mix"], cool_sig["buying_mix"])}
                    cool_sig["confidence"] = prior.get("confidence", 0.5)
                    signals.append(cool_sig)

    signals += await _recurring_needs(db, market, cfg, now, cov)
    if persist:
        for s in signals:
            await db["mi_listen_signals"].update_one(
                {"market": s["market"], "topic": s["topic"], "type": s["type"], "day": s["day"]},
                {"$set": s, "$setOnInsert": {"id": str(uuid.uuid4()), "created_at": now}}, upsert=True)
    return signals


async def _evidence(db, market: dict, topic: str, kind: str, a: datetime, b: datetime, cfg: dict,
                    intents: Optional[set] = None) -> Optional[dict]:
    """intents narrows the evidence to specific reading intents (e.g.
    {"complaint"} for objection_shift) — the signal exists because of that
    subset, so its evidence and samples should come from that subset too,
    not from every on-topic post about the topic."""
    query = {"market": market["key"], "topics": topic, "on_topic": True, "measured": True,
             "published_at": {"$gte": a, "$lt": b}}
    if intents:
        query["intent"] = {"$in": list(intents)}
    rows = [r async for r in db["mi_listen_readings"].find(query)]
    if not rows:
        return None
    authors = Counter(r.get("author") or r["source_id"] for r in rows)
    intents = Counter(r["intent"] for r in rows)
    buying = Counter(r["buying_intent"] for r in rows)
    needs = Counter((r.get("need") or "").strip().lower() for r in rows if r.get("need"))
    items = {i["source_id"]: i async for i in db["mi_listen_items"].find(
        {"source_id": {"$in": [r["source_id"] for r in rows]}},
        {"source_id": 1, "url": 1, "text": 1, "raw_metrics": 1, "published_at": 1, "author_handle": 1, "provider": 1})}
    ranked = sorted(items.values(), key=lambda i: -sum((i.get("raw_metrics") or {}).values()))
    tdoc = await db["mi_market_topics"].find_one({"market": market["key"], "key": topic}) or {}

    # §15 display rules, enforced here rather than left as policy text: how
    # much of a post's own words a sample may quote, and whether a sample
    # with no link back to the original post may be shown at all. Evidence
    # for one topic can now come from several sources at once (Phase A), so
    # this is a per-item lookup keyed by each item's own `provider` — never
    # one global cfg["source"] rule applied to posts it may not even govern.
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

    return {
        "market": market["key"], "market_label": market["label"], "topic": topic,
        "topic_name": tdoc.get("name") or topic, "aliases": tdoc.get("aliases") or [],
        "type": kind, "day": b.strftime("%Y-%m-%d"), "window_start": a, "window_end": b,
        "posts": len(rows), "intent_mix": dict(intents), "buying_mix": dict(buying),
        "needs": [n for n, _ in needs.most_common(3)],
        "top_author_share": round(max(authors.values()) / len(rows), 2),
        "reading_confidence": round(sum(r.get("confidence", 0) for r in rows) / len(rows), 2),
        "samples": samples,
        "status": "active", "computed_at": datetime.utcnow(),
        "expires_at": b + timedelta(days=cfg["score"]["card_ttl_days"]),
    }


async def _recurring_needs(db, market: dict, cfg: dict, now: datetime, cov: set) -> list[dict]:
    """Steady questions/complaints/requests: valuable content even when not trending."""
    d = cfg["detect"]
    a = now - timedelta(days=d["recurring_window_days"])
    by_topic: dict[str, dict] = defaultdict(lambda: {"authors": set(), "days": set()})
    async for r in db["mi_listen_readings"].find({"market": market["key"], "on_topic": True, "measured": True,
                                                  "intent": {"$in": list(NEED_INTENTS)},
                                                  "published_at": {"$gte": a, "$lt": now}}):
        if hour_floor(r["published_at"]) not in cov:
            continue  # only hours that were genuinely checked count
        for t in r.get("topics") or []:
            by_topic[t]["authors"].add(r.get("author") or r["source_id"])
            by_topic[t]["days"].add(r["published_at"].strftime("%Y-%m-%d"))
    covered_days = len({h.strftime("%Y-%m-%d") for h in cov if h >= a})
    out = []
    for topic, v in by_topic.items():
        if len(v["authors"]) >= d["recurring_min_authors"] and len(v["days"]) >= d["recurring_min_days"]:
            sig = await _evidence(db, market, topic, "recurring_need", a, now, cfg)
            if not sig or sig["top_author_share"] > d["max_top_author_share"]:
                continue
            sig.update(current={"authors": len(v["authors"]), "days_present": len(v["days"]),
                                "window_days": d["recurring_window_days"], "covered_days": covered_days},
                       baseline=None, z=0.0, growth=0.0, persistence_days=len(v["days"]))
            sig["scores"] = {"M": momentum_score("recurring_need", 0, 0),
                             "C": commercial_score(sig["intent_mix"], sig["buying_mix"])}
            sig["confidence"] = confidence(len(v["authors"]), d["recurring_min_authors"],
                                           min(1.0, covered_days / max(d["recurring_window_days"], 1)),
                                           covered_days, d["min_history_days"], sig["reading_confidence"])
            out.append(sig)
    return out
