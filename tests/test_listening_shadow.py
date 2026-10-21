"""
Shadow mode (PRD §17): "new models, prompts, weights and thresholds run
alongside production and are compared before switching." Off by default;
when enabled with candidate_overrides, run_shadow() runs that candidate
config unpersisted alongside the real, already-persisted live signals for
the same market and moment, and logs the comparison without ever
affecting what a business actually sees. Uses an in-memory MongoDB; no
network, no AI.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import detect, shadow
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge

NOW = datetime(2026, 10, 21, 12, 0)
CFG = _merge(DEFAULTS, {})
MARKET = {"key": "hair_wigs:NG", "category_id": "hair_wigs", "country": "NG", "label": "Hair & wigs · Nigeria",
          "created_at": NOW - timedelta(days=30), "learned_terms": []}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["shadow_test"]


async def _seed(db, per_day_authors: dict[int, int], topic="latasha wig", covered_days=12, intent="purchase"):
    from app.agents.market_intelligence.listening import topics
    await db["mi_listen_runs"].insert_one({"market": MARKET["key"], "status": "completed",
                                           "covered_from": NOW - timedelta(days=covered_days), "until": NOW,
                                           "started_at": NOW})
    await topics.resolve(db, MARKET["key"], topic, NOW - timedelta(days=20))
    for ago, count in per_day_authors.items():
        for i in range(count):
            ts = NOW - timedelta(days=ago, hours=1 + (i % 20))
            sid = f"x:{ago}-{i}"
            author = f"@a{ago}_{i}"
            item = {"source_id": sid, "author_handle": author, "published_at": ts, "raw_metrics": {"likes": i},
                    "text": f"who sells {topic} in lagos {i}", "url": f"https://x.com/s/{sid}"}
            reading = {"source_id": sid, "market": MARKET["key"], "on_topic": True, "measured": True, "intent": intent,
                       "need": "find a trusted vendor", "sentiment": "neutral", "buying_intent": "high",
                       "topics": [topic], "confidence": 0.9, "published_at": ts, "author": author}
            await db["mi_listen_items"].insert_one(dict(item))
            await db["mi_listen_readings"].insert_one(dict(reading))
            await topics.count(db, MARKET["key"], topic, item, reading)


def _rising_history():
    return {0: 12, 1: 11, **{d: 1 for d in range(2, 11)}}


def test_disabled_by_default_does_nothing():
    db = fresh_db()
    run(_seed(db, _rising_history()))
    run(detect.detect_market(db, MARKET, CFG, NOW))
    result = run(shadow.run_shadow(db, MARKET, CFG, NOW))
    assert result is None
    assert run(db["mi_shadow_comparisons"].count_documents({})) == 0


def test_enabled_with_no_overrides_still_does_nothing():
    db = fresh_db()
    cfg = _merge(CFG, {"shadow": {"enabled": True, "candidate_overrides": {}}})
    result = run(shadow.run_shadow(db, MARKET, cfg, NOW))
    assert result is None


def test_enabled_runs_the_candidate_unpersisted_and_logs_the_comparison():
    db = fresh_db()
    run(_seed(db, _rising_history()))
    run(detect.detect_market(db, MARKET, CFG, NOW))  # the real, live, persisted pass

    cfg = _merge(CFG, {"shadow": {"enabled": True, "candidate_overrides": {"detect": {"rising_z": 999}}}})
    result = run(shadow.run_shadow(db, MARKET, cfg, NOW))

    assert result["live_count"] > 0
    assert result["missing_in_candidate"] >= 1  # the stricter candidate misses the live "rising"
    # Only the ONE run (the real, live one) ever persisted signals — the
    # shadow pass used persist=False internally.
    live_signal_count = run(db["mi_listen_signals"].count_documents({}))
    assert live_signal_count == result["live_count"]
    log = run(db["mi_shadow_comparisons"].find_one({"market": MARKET["key"]}))
    assert log["candidate_count"] == result["candidate_count"]


def test_a_matching_candidate_shows_full_agreement():
    db = fresh_db()
    run(_seed(db, _rising_history()))
    run(detect.detect_market(db, MARKET, CFG, NOW))

    cfg = _merge(CFG, {"shadow": {"enabled": True, "candidate_overrides": {"detect": {}}}})
    result = run(shadow.run_shadow(db, MARKET, cfg, NOW))

    assert result["new_in_candidate"] == 0 and result["missing_in_candidate"] == 0
    assert result["agreement"] == result["live_count"] == result["candidate_count"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
