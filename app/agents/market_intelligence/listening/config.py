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
    "source": "x",                    # adapter key in scan_runner.ADAPTER_REGISTRY — kept as the single-source
                                       # fallback (tests, and any caller that still passes its own adapter)
    "sources": ["x"],                  # Phase A: every active source the scheduled cycle collects from per
                                       # market. Each one still has to clear permissions.is_approved on its own
                                       # (PRD §15) before it actually collects anything — adding a key here only
                                       # makes a source *eligible*, same as registering it in ADAPTER_REGISTRY.
                                       # New sources (tiktok/instagram/youtube/reddit/news, each test+pro) ship
                                       # registered but unapproved; add a key here (and approve it) when ready.
    "source_budgets": {},              # optional per-source monthly USD cap, e.g. {"tiktok": 5.0} — on top of
                                       # (never instead of) the global monthly_budget_usd ceiling below.
    "cadence_hours": 12,               # how often a market is collected, when nothing is hot
    "cadence": {
        "hot_hours": 2,                 # collection interval while a market has an active hot signal
        "hot_signal_types": ["rising", "spike"],
    },
    "items_per_query": 50,             # starting cap per search per run (test actor minimum is 20)
    "max_items_per_query": 150,        # a measuring search that keeps filling up is raised to at most this
    "max_queries_per_market": 6,
    "backfill_days": 7,                # first run of a new market
    "overlap_hours": 2,                # re-read this much before the last run (late-indexed posts)
    "monthly_budget_usd": 12.0,        # all listening collection, this environment
    "pilot_markets": ["beauty_skincare:NG", "hair_wigs:NG"],  # collected even with no subscriber
    # Private-term collection (PRD §6.4-6.5): a business's own brand name and
    # named competitors, searched once per business (never shared across
    # tenants, unlike everything else above). Small and cheap on purpose —
    # a handful of literal-name queries per business, not category search.
    "private": {
        "backfill_days": 3,
        "items_per_query": 25,
    },
    # Competitor-move signal (PRD §8.3): evidence-based, not a statistical
    # trend — see competitor_signals.py's docstring for why a 28-day
    # baseline doesn't fit one business's own low-volume mention stream.
    "competitor_signal": {
        "min_authors": 2,
        "window_days": 7,
    },
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
    # Learning (PRD §9.1's "learned adjustment from past feedback", §11)
    "learning": {
        "feedback_window_days": 60,     # how far back a brand's own dismiss/save history counts
        "min_feedback_samples": 4,      # fewer than this for a market+type combo: no adjustment yet
        "dismiss_rate_threshold": 0.7,  # dismiss rate above this starts lowering relevance
        "max_penalty": 25,              # relevance points subtracted at a 100% dismiss rate
        "category_prior_min_samples": 5,  # cross-tenant content-performance groups smaller than this are
                                           # withheld entirely (PRD §15's minimum-group threshold) — reporting
                                           # only for now, see metrics.category_content_performance
    },
    # Shadow mode (PRD §17) — off by default; set enabled + candidate_overrides
    # to compare a not-yet-shipped config against live production in real time.
    "shadow": {
        "enabled": False,
        "candidate_overrides": {},
    },
    # Human review queue (PRD §17)
    "review": {
        "window_days": 14,
        "confidence_threshold": 0.5,  # readings below this, unreviewed, show up in the queue
    },
    # Retention (PRD §12, §15)
    "retention": {
        "item_days": 90,  # normalised items + evidence text: 90 days hot, then deleted
    },
    # Query value optimisation (PRD §6.6)
    "efficiency": {
        "window_days": 14,
        "min_items_for_judgement": 15,  # fewer than this for a query: no verdict yet
        "min_on_topic_rate": 0.25,      # below this on-topic share: the query is low-yield
        "shrink_factor": 0.5,           # cap multiplier applied to a low-yield query
        "min_cap": 10,                  # never shrink a query to zero — keep exploring it at a small scale
    },
    # Plan-tier limits (PRD §5's "Operating policy: Plan tier... Scheduler
    # priority, delivery policy"; §21 open decision #8). Every tier below
    # starts equal to today's single global values (cadence_hours=12,
    # delivery.max_alerts_per_week=3, retention.item_days=90) on purpose —
    # adding this lookup changes nothing about current behaviour by itself;
    # differentiating a tier is a deliberate later edit to these numbers,
    # not a side effect of this key existing. "default" is used for any
    # resolved tier string that isn't one of the named keys below (e.g. an
    # agency plan_tier or subscription_tier this list hasn't been told
    # about yet) — never a guess, the same global values either way.
    "plan_tiers": {
        "default":        {"cadence_hours": 12, "max_alerts_per_week": 3, "history_days": 90},
        "starter":        {"cadence_hours": 12, "max_alerts_per_week": 3, "history_days": 90},
        "growth":         {"cadence_hours": 12, "max_alerts_per_week": 3, "history_days": 90},
        "pro":            {"cadence_hours": 12, "max_alerts_per_week": 3, "history_days": 90},
        "agency_starter": {"cadence_hours": 12, "max_alerts_per_week": 3, "history_days": 90},
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
