"""
Adaptive cadence (PRD §7.3): "Hot topics are automatically promoted to
faster cadence and cooled down when momentum fades." A market with an
active rising/spike signal gets collected on the faster hot_hours interval;
once nothing is hot, it falls back to the default cadence_hours. Uses an
in-memory MongoDB; no network, no AI.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import markets
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
    return mongomock_motor.AsyncMongoMockClient()["cadence_test"]


async def _signal(db, kind, status="active", expires_in_hours=24):
    await db["mi_listen_signals"].insert_one({
        "market": MARKET["key"], "topic": "latasha wig", "type": kind, "status": status,
        "expires_at": NOW + timedelta(hours=expires_in_hours),
    })


async def _seed_market(db, **extra):
    doc = {"key": MARKET["key"], **extra}
    await db["mi_markets"].insert_one(doc)
    return doc


def _update(db):
    return run(markets.update_cadence(db, MARKET, CFG, NOW))


def _stored_cadence(db):
    return run(db["mi_markets"].find_one({"key": MARKET["key"]}))["cadence_hours"]


# ── update_cadence ───────────────────────────────────────────────────────────

def test_no_signals_at_all_uses_the_default_cadence():
    db = fresh_db()
    run(_seed_market(db))
    assert _update(db) == CFG["cadence_hours"]
    assert _stored_cadence(db) == CFG["cadence_hours"]


def test_an_active_rising_signal_promotes_to_hot_cadence():
    db = fresh_db()
    run(_seed_market(db))
    run(_signal(db, "rising"))
    assert _update(db) == CFG["cadence"]["hot_hours"]
    assert _stored_cadence(db) == CFG["cadence"]["hot_hours"]


def test_an_active_spike_signal_also_promotes():
    db = fresh_db()
    run(_seed_market(db))
    run(_signal(db, "spike"))
    assert _update(db) == CFG["cadence"]["hot_hours"]


def test_recurring_need_does_not_count_as_hot():
    db = fresh_db()
    run(_seed_market(db))
    run(_signal(db, "recurring_need"))
    assert _update(db) == CFG["cadence_hours"]


def test_expired_signal_does_not_count_as_hot():
    db = fresh_db()
    run(_seed_market(db))
    run(_signal(db, "rising", expires_in_hours=-1))
    assert _update(db) == CFG["cadence_hours"]


def test_dismissed_or_cooled_signal_does_not_count():
    db = fresh_db()
    run(_seed_market(db))
    run(_signal(db, "rising", status="cooled"))
    assert _update(db) == CFG["cadence_hours"]


def test_cools_back_down_once_the_hot_signal_is_gone():
    db = fresh_db()
    run(_seed_market(db))
    run(_signal(db, "rising"))
    assert _update(db) == CFG["cadence"]["hot_hours"]

    run(db["mi_listen_signals"].update_many({}, {"$set": {"status": "cooled"}}))
    assert _update(db) == CFG["cadence_hours"]


# ── due_markets respects the per-market override ─────────────────────────────

def test_due_markets_uses_the_markets_own_cadence_not_the_flat_default():
    db = fresh_db()
    # Collected 3 hours ago: due under the hot cadence (2h), not due under
    # the default (12h) — proves due_markets reads the per-market override.
    run(_seed_market(db, status="active", subscribers=["b1"], cadence_hours=2,
                     last_collected_at=NOW - timedelta(hours=3)))

    due = run(markets.due_markets(db, CFG, NOW))

    assert [m["key"] for m in due] == [MARKET["key"]]


def test_due_markets_falls_back_to_default_with_no_override():
    db = fresh_db()
    run(_seed_market(db, status="active", subscribers=["b1"],
                     last_collected_at=NOW - timedelta(hours=3)))  # no cadence_hours field

    due = run(markets.due_markets(db, CFG, NOW))

    assert due == []  # 3h < default 12h cadence, not due yet


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
