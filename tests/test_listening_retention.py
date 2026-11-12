"""
Retention and deletion cascade (PRD §12, §15): items and their readings
past the retention window are deleted; everything derived — topic counts,
the topic graph, cards, feedback — is kept, since it "contains no
personal data" and is the long-term asset (§8.1). Uses an in-memory
MongoDB; no network, no AI.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import retention
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge

NOW = datetime(2026, 10, 21, 12, 0)
CFG = _merge(DEFAULTS, {})
RETENTION_DAYS = CFG["retention"]["item_days"]


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["retention_test"]


async def _item(db, sid, days_old, with_reading=True):
    await db["mi_listen_items"].insert_one({
        "source_id": sid, "markets": ["hair_wigs:NG"], "text": "some post",
        "published_at": NOW - timedelta(days=days_old),
    })
    if with_reading:
        await db["mi_listen_readings"].insert_one({"source_id": sid, "market": "hair_wigs:NG", "on_topic": True})


def _enforce(db):
    return run(retention.enforce_retention(db, CFG, NOW))


# ── delete_items (the cascade primitive) ────────────────────────────────────

def test_delete_items_removes_both_item_and_reading():
    db = fresh_db()
    run(_item(db, "s1", days_old=1))

    result = run(retention.delete_items(db, ["s1"]))

    assert result == {"items": 1, "readings": 1}
    assert run(db["mi_listen_items"].find_one({"source_id": "s1"})) is None
    assert run(db["mi_listen_readings"].find_one({"source_id": "s1"})) is None


def test_delete_items_with_empty_list_does_nothing():
    db = fresh_db()
    run(_item(db, "s1", days_old=1))

    result = run(retention.delete_items(db, []))

    assert result == {"items": 0, "readings": 0}
    assert run(db["mi_listen_items"].find_one({"source_id": "s1"})) is not None


def test_delete_items_is_safe_when_no_reading_exists_yet():
    db = fresh_db()
    run(_item(db, "s1", days_old=1, with_reading=False))

    result = run(retention.delete_items(db, ["s1"]))

    assert result == {"items": 1, "readings": 0}


# ── enforce_retention (the scheduled sweep) ──────────────────────────────────

def test_items_past_the_window_are_deleted():
    db = fresh_db()
    run(_item(db, "old", days_old=RETENTION_DAYS + 1))

    result = _enforce(db)

    assert result["items"] == 1 and result["readings"] == 1
    assert run(db["mi_listen_items"].find_one({"source_id": "old"})) is None


def test_items_within_the_window_are_kept():
    db = fresh_db()
    run(_item(db, "recent", days_old=RETENTION_DAYS - 1))

    result = _enforce(db)

    assert result["items"] == 0
    assert run(db["mi_listen_items"].find_one({"source_id": "recent"})) is not None


def test_only_the_expired_subset_is_removed():
    db = fresh_db()
    run(_item(db, "old", days_old=RETENTION_DAYS + 5))
    run(_item(db, "recent", days_old=1))

    _enforce(db)

    assert run(db["mi_listen_items"].find_one({"source_id": "old"})) is None
    assert run(db["mi_listen_items"].find_one({"source_id": "recent"})) is not None


def test_derived_aggregates_are_never_touched_by_retention():
    db = fresh_db()
    run(_item(db, "old", days_old=RETENTION_DAYS + 1))
    aggregate_docs = {
        "mi_topic_counts": {"market": "hair_wigs:NG", "topic": "latasha wig", "bucket": NOW - timedelta(days=200)},
        "mi_market_topics": {"market": "hair_wigs:NG", "key": "latasha wig", "status": "monitored"},
        "mi_listen_signals": {"market": "hair_wigs:NG", "topic": "latasha wig", "type": "rising",
                              "computed_at": NOW - timedelta(days=200)},
        "mi_listen_cards": {"id": "c1", "brand_id": "b1", "topic": "latasha wig",
                            "created_at": NOW - timedelta(days=200)},
    }
    for coll, doc in aggregate_docs.items():
        run(db[coll].insert_one(dict(doc)))

    _enforce(db)

    for coll in aggregate_docs:
        assert run(db[coll].count_documents({})) == 1, f"{coll} was touched by retention"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
