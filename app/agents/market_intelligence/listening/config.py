"""
Listening settings. The values below are only the first-run defaults: the
live values are the `mi_listening_config` document (id "global"), merged
over these, so cadence, budgets, thresholds and score weights are changed in
data — per environment, without a deploy — and every change is visible in
one place.
"""
from __future__ import annotations

import copy
from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Collection
    "source": "x",                    # adapter key in scan_runner.ADAPTER_REGISTRY
    "cadence_hours": 12,               # how often a market is collected
    "items_per_query": 50,             # starting cap per search per run (test actor minimum is 20)
    "max_items_per_query": 150,        # a measuring search that keeps filling up is raised to at most this
    "max_queries_per_market": 6,
    "backfill_days": 7,                # first run of a new market
    "overlap_hours": 2,                # re-read this much before the last run (late-indexed posts)
    "monthly_budget_usd": 12.0,        # all listening collection, this environment
    "pilot_markets": ["beauty_skincare:NG", "hair_wigs:NG"],  # collected even with no subscriber
    # Enrichment
    "enrich_batch_size": 15,
    "enrich_model": "gpt-4o-mini",
    # Detection
    "detect": {
        "baseline_days": 28,
        "min_history_days": 7,         # fewer -> "emerging signal", never "rising"
        "min_coverage": 0.5,           # a day counts in the baseline only if this share of it was covered
        "rising_z": 2.0,
        "rising_min_growth": 0.5,      # +50% over the usual daily rate
        "persistence_days": 2,         # elevated this many days in a row -> rising (else spike)
        "min_authors": 5,              # distinct accounts in the window
        "max_top_author_share": 0.4,   # one account can't make a trend
        "recurring_window_days": 14,
        "recurring_min_authors": 6,
        "recurring_min_days": 4,
        "new_entity_days": 3,
    },
    # Personalisation
    "score": {
        "weights": {"R": 0.30, "M": 0.25, "C": 0.20, "E": 0.15, "B": 0.10},
        "min_confidence": 0.5,
        "min_score": 55,
        "min_relevance": 50,
        "max_signals_judged": 12,
        "max_cards_per_run": 4,
        "card_ttl_days": 7,
        "model": "gpt-4o-mini",
    },
    # Delivery
    "delivery": {
        "alert_min_score": 80,
        "max_alerts_per_week": 3,
        "digest_max_items": 5,
    },
    # Discovery (keyword lifecycle, PRD §6.3)
    "discovery": {
        "window_days": 10,              # evidence lookback for every status check
        "candidate_min_accounts": 3,    # observed -> candidate
        "candidate_min_relevance": 0.5,
        "candidate_timeout_days": 21,   # candidate -> retired if never promoted
        "monitor_min_accounts": 5,      # candidate -> monitored (added to learned_terms)
        "monitor_min_days": 3,
        "monitor_min_relevance": 0.6,
        "pause_min_relevance": 0.3,     # monitored -> paused (dropped from learned_terms)
        "pause_after_windows": 3,       # consecutive low-yield runs before pausing
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


async def get_config(db) -> dict:
    doc = await db["mi_listening_config"].find_one({"id": "global"}) or {}
    doc.pop("_id", None)
    doc.pop("id", None)
    return _merge(DEFAULTS, doc)
