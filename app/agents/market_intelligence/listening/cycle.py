"""
The listening loop: Listen → Understand → Recommend (spec §16 loop).

Runs from the shared scheduler (hourly tick; each market is only collected
when its cadence is due) and can be triggered for one business from the API.
A lock document keeps two cycles from overlapping across workers.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from . import collector, detect, discovery, markets, ontology, permissions, personalize, profile as profile_mod, shadow
from .config import _merge, get_config

LOCK_MINUTES = 50


async def _lock(db, lock_id: str, owner: str) -> bool:
    """Take a named lock (expires on its own if a worker dies mid-run)."""
    now = datetime.utcnow()
    try:
        await db["mi_listen_lock"].create_index("id", unique=True)
        r = await db["mi_listen_lock"].find_one_and_update(
            {"id": lock_id, "$or": [{"until": {"$lt": now}}, {"until": None}]},
            {"$set": {"owner": owner, "until": now + timedelta(minutes=LOCK_MINUTES)}},
        )
        if r:
            return True
        await db["mi_listen_lock"].insert_one({"id": lock_id, "owner": owner,
                                               "until": now + timedelta(minutes=LOCK_MINUTES)})
        return True
    except Exception:
        return False  # duplicate key: someone else holds it


async def _unlock(db, lock_id: str, owner: str) -> None:
    await db["mi_listen_lock"].update_one({"id": lock_id, "owner": owner}, {"$set": {"until": None}})


async def _tier_cadence_hours(db, cfg: dict, user_id: str, brand_id: str, cache: dict[str, int]) -> int:
    """One brand's plan-tier cadence (PRD §5, §21 open decision #8), cached
    per cycle run so N markets sharing the same subscriber only resolve its
    tier once."""
    if brand_id not in cache:
        tier = await profile_mod.effective_plan_tier(db, user_id, brand_id)
        tiers = cfg["plan_tiers"]
        cache[brand_id] = (tiers.get(tier) or tiers["default"])["cadence_hours"]
    return cache[brand_id]


async def _market_tier_floor_hours(db, cfg: dict, market: dict, profiles_by_brand: dict[str, dict],
                                   cache: dict[str, int]) -> Optional[int]:
    """PRD §6.5: "collected once, on the schedule its most demanding
    subscriber's plan allows" — the fastest cadence any of this market's
    *current* subscribers' plan tiers calls for. None (no floor) when no
    subscriber resolves — due_markets() already treats a pilot-only market's
    plain cfg["cadence_hours"] as the floor in that case, same as before
    this existed."""
    fastest: Optional[int] = None
    for brand_id in market.get("subscribers") or []:
        p = profiles_by_brand.get(brand_id)
        if not p or not p.get("user_id"):
            continue
        hours = await _tier_cadence_hours(db, cfg, p["user_id"], brand_id, cache)
        if fastest is None or hours < fastest:
            fastest = hours
    return fastest


async def refresh_profile(db, prof: dict) -> Optional[dict]:
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    try:
        res = await BrandProfileService.get(prof["user_id"], db, brand_id=prof["brand_id"])
        playbook = (res.get("responseData") or {}) if res.get("status") else {}
    except Exception as e:
        print(f"[MI][listen] playbook read failed for {prof['brand_id']}: {e}")
        return prof
    return await profile_mod.build(db, prof["user_id"], prof["brand_id"], playbook) or prof


async def run_cycle(db, only_markets: Optional[list[str]] = None, brand_ids: Optional[list[str]] = None,
                    adapter=None) -> dict:
    cfg = await get_config(db)
    summary = {"id": str(uuid.uuid4()), "started_at": datetime.utcnow(), "markets": [], "cards": 0, "alerts": 0}
    if not cfg["enabled"]:
        summary["note"] = "listening disabled"
        return summary
    owner = summary["id"]
    # The scheduled cycle and each business's "check now" have their own lock,
    # so one business's run never blocks another's; markets have their own
    # lock below so the same market is never collected twice at once.
    cycle_lock = "cycle" if not brand_ids else "brands:" + ",".join(sorted(brand_ids))
    summary["lock"] = cycle_lock
    if not await _lock(db, cycle_lock, owner):
        summary["note"] = "this check is already running"
        return summary
    try:
        await ontology.ensure_seeded(db)
        await permissions.ensure_seeded(db)
        for key in cfg.get("pilot_markets") or []:
            cid, _, country = key.partition(":")
            await markets.ensure_market(db, cid, country or "GLOBAL", pilot=True)

        pq = {"status": "active"} if not brand_ids else {"brand_id": {"$in": brand_ids}}
        profiles = [p async for p in db["mi_listening_profiles"].find(pq)]
        profiles = [await refresh_profile(db, p) for p in profiles]
        if brand_ids and not only_markets:
            only_markets = sorted({k for p in profiles for k in (p.get("markets") or [])})
        profiles_by_brand = {p["brand_id"]: p for p in profiles if p and p.get("brand_id")}
        tier_cadence_cache: dict[str, int] = {}

        # Phase A Step 5: every market now collects from every *active*
        # source configured for this environment (cfg["sources"], default
        # ["x"] — unset/old configs fall back to the single cfg["source"]
        # for compatibility), not just one global adapter. A source that
        # isn't actually approved yet still shows up as one more per-source
        # run entry with status "blocked_unapproved_source" — collect_market
        # itself is the single source of truth for that gate (PRD §15), so
        # this loop doesn't duplicate the check, it just calls it per source.
        if adapter is not None:
            active_sources = [cfg.get("source", "x")]  # caller passed one adapter explicitly (e.g. a test)
            adapters = {active_sources[0]: adapter}
        else:
            from ..scan_runner import ADAPTER_REGISTRY
            active_sources = cfg.get("sources") or [cfg.get("source", "x")]
            adapters = {s: ADAPTER_REGISTRY.get(s) for s in active_sources}
        due = await markets.due_markets(db, cfg, only=only_markets)
        touched = []
        for m in due:
            entry = {"market": m["key"], "sources": []}
            mlock = f"market:{m['key']}"
            if not await _lock(db, mlock, owner):
                entry["note"] = "market is being collected by another run"
                summary["markets"].append(entry)
                continue
            try:
                to_read: list[str] = []
                new_items_total = cost_total = 0
                for source in active_sources:
                    src_adapter = adapters.get(source)
                    if src_adapter is None:
                        entry["sources"].append({"source": source, "run": "unknown_adapter"})
                        continue
                    run = await collector.collect_market(db, m, cfg, src_adapter, source)
                    entry["sources"].append({"source": source, "run": run["status"],
                                             "new_items": run.get("new_items", 0), "cost": run.get("cost_usd", 0),
                                             "note": run.get("note")})
                    new_items_total += run.get("new_items", 0)
                    cost_total += run.get("cost_usd", 0) or 0
                    to_read.extend(run.get("to_read") or [])
                entry.update(new_items=new_items_total, cost=round(cost_total, 5))
                if to_read:
                    entry["reading"] = await collector.read_and_count(db, m, list(dict.fromkeys(to_read)), cfg)
                detect_now = datetime.utcnow()
                sigs = await detect.detect_market(db, m, cfg, detect_now)
                entry["signals"] = len(sigs)
                disc = await discovery.update_market(db, m, cfg)
                entry["discovery"] = {k: len(v) for k, v in disc.items()}
                tier_floor = await _market_tier_floor_hours(db, cfg, m, profiles_by_brand, tier_cadence_cache)
                entry["cadence_hours"] = await markets.update_cadence(db, m, cfg, tier_floor_hours=tier_floor)
                eff = await collector.update_query_efficiency(db, m, cfg)
                entry["efficiency"] = {"shrunk": len(eff["shrunk"])}
                shadow_result = await shadow.run_shadow(db, m, cfg, detect_now)
                if shadow_result:
                    entry["shadow"] = {k: shadow_result[k] for k in
                                       ("candidate_count", "live_count", "new_in_candidate", "missing_in_candidate")}
                touched.append(m["key"])
            finally:
                await _unlock(db, mlock, owner)
            summary["markets"].append(entry)

        for p in profiles:
            if not p or p.get("status") != "active" or not set(p.get("markets") or []) & set(touched or p.get("markets") or []):
                continue
            cards = await personalize.cards_for(db, p, cfg)
            summary["cards"] += len(cards)
            # PRD §5's "Operating policy: Plan tier... delivery policy" —
            # this brand's own alert budget, not the flat global default,
            # for this one notify() call only (cards_for above is
            # unaffected: card generation itself isn't plan-tier-gated,
            # only how many alerts a week can interrupt the owner).
            tier = await profile_mod.effective_plan_tier(db, p["user_id"], p["brand_id"])
            tiers = cfg["plan_tiers"]
            max_alerts = (tiers.get(tier) or tiers["default"])["max_alerts_per_week"]
            brand_cfg = _merge(cfg, {"delivery": {"max_alerts_per_week": max_alerts}})
            summary["alerts"] += await personalize.notify(db, p, cards, brand_cfg)
    finally:
        await _unlock(db, cycle_lock, owner)
        summary["finished_at"] = datetime.utcnow()
        await db["mi_listen_cycles"].insert_one(dict(summary))
    return summary
