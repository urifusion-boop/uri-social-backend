"""
Shadow mode (PRD §17): "new models, prompts, weights and thresholds run
alongside production and are compared before switching."

Distinct from replay.py, which re-runs stored *history* after the fact:
this runs a candidate config in parallel with the real, live detection
pass on every cycle tick, for a market just collected, and logs the
comparison. The candidate's result is never what a business actually
sees — nothing here ever reaches mi_listen_cards — until someone decides
the comparison looks good and changes the live config to match it.

Off by default (cfg["shadow"]["enabled"]). When on, the one extra
detect_market(persist=False) call per market is pure arithmetic over data
the real pass already read — no extra AI calls, no extra collection cost,
just one more pass of the same deterministic maths.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import detect
from .config import _merge


async def run_shadow(db, market: dict, cfg: dict, now: Optional[datetime] = None) -> Optional[dict]:
    sh = cfg.get("shadow") or {}
    if not sh.get("enabled") or not sh.get("candidate_overrides"):
        return None
    now = now or datetime.utcnow()

    candidate_cfg = _merge(cfg, sh["candidate_overrides"])
    candidate_signals = await detect.detect_market(db, market, candidate_cfg, now, persist=False)
    live_signals = [s async for s in db["mi_listen_signals"].find(
        {"market": market["key"], "day": now.strftime("%Y-%m-%d")})]

    candidate_keys = {(s["topic"], s["type"]) for s in candidate_signals}
    live_keys = {(s["topic"], s["type"]) for s in live_signals}

    comparison = {
        "market": market["key"], "at": now,
        "candidate_count": len(candidate_signals), "live_count": len(live_signals),
        "new_in_candidate": len(candidate_keys - live_keys),
        "missing_in_candidate": len(live_keys - candidate_keys),
        "agreement": len(candidate_keys & live_keys),
    }
    await db["mi_shadow_comparisons"].insert_one(dict(comparison))
    return comparison
