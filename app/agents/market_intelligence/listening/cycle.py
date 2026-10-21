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

from . import collector, detect, discovery, markets, ontology, personalize, profile as profile_mod
from .config import get_config

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
        for key in cfg.get("pilot_markets") or []:
            cid, _, country = key.partition(":")
            await markets.ensure_market(db, cid, country or "GLOBAL", pilot=True)

        pq = {"status": "active"} if not brand_ids else {"brand_id": {"$in": brand_ids}}
        profiles = [p async for p in db["mi_listening_profiles"].find(pq)]
        profiles = [await refresh_profile(db, p) for p in profiles]
        if brand_ids and not only_markets:
            only_markets = sorted({k for p in profiles for k in (p.get("markets") or [])})

        if adapter is None:
            from ..scan_runner import ADAPTER_REGISTRY
            adapter = ADAPTER_REGISTRY.get(cfg["source"])
        due = await markets.due_markets(db, cfg, only=only_markets)
        touched = []
        for m in due:
            entry = {"market": m["key"]}
            mlock = f"market:{m['key']}"
            if not await _lock(db, mlock, owner):
                entry["note"] = "market is being collected by another run"
                summary["markets"].append(entry)
                continue
            try:
                run = await collector.collect_market(db, m, cfg, adapter)
                entry.update(run=run["status"], new_items=run.get("new_items", 0), cost=run.get("cost_usd", 0),
                             note=run.get("note"))
                if run.get("to_read"):
                    entry["reading"] = await collector.read_and_count(db, m, run["to_read"], cfg)
                sigs = await detect.detect_market(db, m, cfg)
                entry["signals"] = len(sigs)
                disc = await discovery.update_market(db, m, cfg)
                entry["discovery"] = {k: len(v) for k, v in disc.items()}
                entry["cadence_hours"] = await markets.update_cadence(db, m, cfg)
                eff = await collector.update_query_efficiency(db, m, cfg)
                entry["efficiency"] = {"shrunk": len(eff["shrunk"])}
                touched.append(m["key"])
            finally:
                await _unlock(db, mlock, owner)
            summary["markets"].append(entry)

        for p in profiles:
            if not p or p.get("status") != "active" or not set(p.get("markets") or []) & set(touched or p.get("markets") or []):
                continue
            cards = await personalize.cards_for(db, p, cfg)
            summary["cards"] += len(cards)
            summary["alerts"] += await personalize.notify(db, p, cards, cfg)
    finally:
        await _unlock(db, cycle_lock, owner)
        summary["finished_at"] = datetime.utcnow()
        await db["mi_listen_cycles"].insert_one(dict(summary))
    return summary
