"""
Offline replay (PRD §17): "detectors and scoring changes are replayed
over stored history before release to measure false alerts and missed
trends."

detect.detect_market() already reads nothing but already-stored coverage
and topic counts for a given `now` — it never collects live — so running
it again with a candidate config against a past `now` and diffing the
result against what the live config actually produced and stored at the
time is exactly this replay, with no separate data pipeline needed.
persist=False (added to detect_market for this) means a replay run never
overwrites a real signal.

Needs real collected history to be useful — on a brand-new environment
with no history yet, every replay simply returns zero signals on both
sides, which is correct, not broken: there's nothing to compare against
yet.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import detect


async def replay_market(db, market: dict, candidate_cfg: dict, now: datetime,
                        actual_cfg: Optional[dict] = None) -> dict:
    """What candidate_cfg would have produced for this market at this
    point in history, compared with what was actually produced and stored
    at the time. Pass actual_cfg to compare two configs head-to-head
    (neither persisted); omit it to compare the candidate against the real
    mi_listen_signals the live config already wrote."""
    candidate_signals = await detect.detect_market(db, market, candidate_cfg, now, persist=False)
    candidate_by_key = {(s["topic"], s["type"]) for s in candidate_signals}

    if actual_cfg is not None:
        actual_signals = await detect.detect_market(db, market, actual_cfg, now, persist=False)
    else:
        actual_signals = [s async for s in db["mi_listen_signals"].find(
            {"market": market["key"], "day": now.strftime("%Y-%m-%d")})]
    actual_by_key = {(s["topic"], s["type"]) for s in actual_signals}

    new_signals = sorted(candidate_by_key - actual_by_key)      # candidate would catch, the comparison didn't
    missed_signals = sorted(actual_by_key - candidate_by_key)   # the comparison caught, candidate wouldn't
    agreement = candidate_by_key & actual_by_key

    return {
        "market": market["key"], "day": now.strftime("%Y-%m-%d"),
        "candidate_signal_count": len(candidate_signals), "compared_signal_count": len(actual_signals),
        "new_signals": [{"topic": t, "type": k} for t, k in new_signals],
        "missed_signals": [{"topic": t, "type": k} for t, k in missed_signals],
        "agreement_count": len(agreement),
    }


async def replay_range(db, market: dict, candidate_cfg: dict, days: list[datetime],
                       actual_cfg: Optional[dict] = None) -> list[dict]:
    return [await replay_market(db, market, candidate_cfg, d, actual_cfg) for d in days]
