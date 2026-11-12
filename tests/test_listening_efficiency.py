"""
Query value optimisation (PRD §6.6): "spend collection budget where
expected useful evidence per unit cost is highest." Each query's own
on-topic rate (measured from items it found, cross-referenced against AI
readings already done) decides whether its next-run item cap shrinks.
Never below min_cap — a low-yield query keeps being tried at a small
scale instead of being dropped to zero (the bandit's "exploration budget"
in miniature). Uses an in-memory MongoDB; no network, no AI.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import collector
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge

NOW = datetime(2026, 10, 21, 12, 0)
CFG = _merge(DEFAULTS, {})
MARKET = {"key": "hair_wigs:NG"}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["efficiency_test"]


async def _item_and_reading(db, sid, query_ids, on_topic=True, days_ago=1, read=True):
    await db["mi_listen_items"].insert_one({
        "source_id": sid, "markets": [MARKET["key"]], "found_by_queries": list(query_ids),
        "published_at": NOW - timedelta(days=days_ago),
    })
    if read:
        await db["mi_listen_readings"].insert_one({
            "source_id": sid, "market": MARKET["key"], "on_topic": on_topic,
        })


def _update(db, market=None):
    market = market or dict(MARKET)
    return run(collector.update_query_efficiency(db, market, CFG, NOW))


MIN = CFG["efficiency"]["min_items_for_judgement"]


def _seed_query(db, qid, n_items, on_topic_fraction, days_ago=1, read=True):
    on_count = round(n_items * on_topic_fraction)
    for i in range(n_items):
        run(_item_and_reading(db, f"{qid}-{i}", [qid], on_topic=i < on_count, days_ago=days_ago, read=read))


def test_too_few_items_gives_no_verdict():
    db = fresh_db()
    _seed_query(db, "q1", MIN - 1, on_topic_fraction=0.0)  # all off-topic, but not enough samples
    result = _update(db)
    assert result["shrunk"] == []


def test_low_on_topic_rate_shrinks_the_cap():
    db = fresh_db()
    _seed_query(db, "q1", MIN, on_topic_fraction=0.1)  # well below 0.25 threshold
    result = _update(db)
    assert len(result["shrunk"]) == 1
    row = result["shrunk"][0]
    assert row["query_id"] == "q1"
    expected_cap = max(CFG["efficiency"]["min_cap"],
                       int(CFG["items_per_query"] * CFG["efficiency"]["shrink_factor"]))
    assert row["cap"] == expected_cap


def test_good_on_topic_rate_is_left_alone():
    db = fresh_db()
    _seed_query(db, "q1", MIN, on_topic_fraction=0.8)
    result = _update(db)
    assert result["shrunk"] == []


def test_shrink_never_goes_below_the_floor():
    db = fresh_db()
    market = {"key": MARKET["key"], "query_caps": {"q1": CFG["efficiency"]["min_cap"] + 2}}
    _seed_query(db, "q1", MIN, on_topic_fraction=0.0)
    result = _update(db, market)
    assert result["shrunk"][0]["cap"] == CFG["efficiency"]["min_cap"]


def test_cap_shrinks_further_on_sustained_low_yield():
    db = fresh_db()
    market = {"key": MARKET["key"], "query_caps": {"q1": 60}}
    _seed_query(db, "q1", MIN, on_topic_fraction=0.1)
    result = _update(db, market)
    assert result["shrunk"][0]["cap"] == 30  # 60 * 0.5


def test_items_outside_the_window_do_not_count():
    db = fresh_db()
    _seed_query(db, "q1", MIN, on_topic_fraction=0.0, days_ago=30)  # past the 14-day window
    result = _update(db)
    assert result["shrunk"] == []


def test_unread_items_are_not_counted_either_way():
    db = fresh_db()
    _seed_query(db, "q1", MIN, on_topic_fraction=0.0, read=False)
    result = _update(db)
    assert result["shrunk"] == []


def test_an_item_found_by_two_queries_counts_toward_both():
    db = fresh_db()
    for i in range(MIN):
        run(_item_and_reading(db, f"shared-{i}", ["q1", "q2"], on_topic=False))
    result = _update(db)
    shrunk_ids = {r["query_id"] for r in result["shrunk"]}
    assert shrunk_ids == {"q1", "q2"}


def test_persists_the_new_cap_onto_the_market():
    db = fresh_db()
    run(db["mi_markets"].insert_one({"key": MARKET["key"], "query_caps": {}}))
    _seed_query(db, "q1", MIN, on_topic_fraction=0.0)
    _update(db)
    stored = run(db["mi_markets"].find_one({"key": MARKET["key"]}))
    assert stored["query_caps"]["q1"] == max(CFG["efficiency"]["min_cap"],
                                             int(CFG["items_per_query"] * CFG["efficiency"]["shrink_factor"]))


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
