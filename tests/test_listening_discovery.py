"""
Keyword discovery lifecycle (PRD §6.3): a topic moves
observed -> candidate -> monitored (added to the market's own searches, so
everyone subscribed benefits) purely from its own evidence, with no
curation console yet. candidate -> retired on timeout; monitored -> paused
after sustained low yield. Uses an in-memory MongoDB; no network, no AI.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import discovery
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge

NOW = datetime(2026, 10, 21, 12, 0)
CFG = _merge(DEFAULTS, {})
MARKET_KEY = "hair_wigs:NG"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["discovery_test"]


async def _seed_market(db, learned_terms=None):
    await db["mi_markets"].insert_one({"key": MARKET_KEY, "learned_terms": learned_terms or []})
    return await db["mi_markets"].find_one({"key": MARKET_KEY})


async def _seed_topic(db, key, status, **extra):
    doc = {"market": MARKET_KEY, "key": key, "status": status, "first_seen": NOW - timedelta(days=15)}
    doc.update(extra)
    await db["mi_market_topics"].insert_one(dict(doc))


async def _seed_readings(db, topic, n_accounts, on_topic_fraction=1.0, days_ago=1):
    """n_accounts distinct accounts each posting once about `topic`, inside
    the default 10-day discovery window."""
    on_count = round(n_accounts * on_topic_fraction)
    for i in range(n_accounts):
        ts = NOW - timedelta(days=days_ago, hours=i % 20)
        await db["mi_listen_readings"].insert_one({
            "source_id": f"{topic}-{days_ago}-{i}", "market": MARKET_KEY, "topics": [topic],
            "on_topic": i < on_count, "published_at": ts, "author": f"@a{i}",
        })


def _market(db):
    return run(db["mi_markets"].find_one({"key": MARKET_KEY}))


def _topic(db, key):
    return run(db["mi_market_topics"].find_one({"market": MARKET_KEY, "key": key}))


def _update(db, market=None):
    market = market or _market(db)
    return run(discovery.update_market(db, market, CFG, NOW))


# ── observed -> candidate ───────────────────────────────────────────────────

def test_enough_independent_accounts_promotes_to_candidate():
    db = fresh_db()
    run(_seed_market(db))
    run(_seed_topic(db, "latasha wig", "observed"))
    run(_seed_readings(db, "latasha wig", n_accounts=4))  # default threshold is 3

    summary = _update(db)

    assert "latasha wig" in summary["candidates"]
    assert _topic(db, "latasha wig")["status"] == "candidate"


def test_too_few_accounts_stays_observed():
    db = fresh_db()
    run(_seed_market(db))
    run(_seed_topic(db, "latasha wig", "observed"))
    run(_seed_readings(db, "latasha wig", n_accounts=2))

    summary = _update(db)

    assert summary["candidates"] == []
    assert _topic(db, "latasha wig")["status"] == "observed"


def test_mostly_off_topic_mentions_do_not_promote():
    db = fresh_db()
    run(_seed_market(db))
    run(_seed_topic(db, "fake wig scandal", "observed"))
    run(_seed_readings(db, "fake wig scandal", n_accounts=6, on_topic_fraction=0.2))  # below relevance bar

    summary = _update(db)

    assert summary["candidates"] == []
    assert _topic(db, "fake wig scandal")["status"] == "observed"


# ── candidate -> monitored / retired ────────────────────────────────────────

def test_candidate_clearing_the_bar_is_added_to_learned_terms():
    db = fresh_db()
    run(_seed_market(db))
    run(_seed_topic(db, "latasha wig", "candidate", candidate_at=NOW - timedelta(days=2)))
    run(_seed_readings(db, "latasha wig", n_accounts=6, days_ago=1))
    run(_seed_readings(db, "latasha wig", n_accounts=2, days_ago=4))  # a second distinct day
    run(_seed_readings(db, "latasha wig", n_accounts=2, days_ago=7))  # a third distinct day

    summary = _update(db)

    assert "latasha wig" in summary["monitored"]
    assert _topic(db, "latasha wig")["status"] == "monitored"
    assert "latasha wig" in _market(db)["learned_terms"]


def test_candidate_short_of_the_bar_is_left_alone_before_timeout():
    db = fresh_db()
    run(_seed_market(db))
    run(_seed_topic(db, "latasha wig", "candidate", candidate_at=NOW - timedelta(days=2)))
    run(_seed_readings(db, "latasha wig", n_accounts=3))  # below monitor_min_accounts=5

    summary = _update(db)

    assert summary == {"candidates": [], "monitored": [], "retired": [], "paused": []}
    assert _topic(db, "latasha wig")["status"] == "candidate"
    assert _market(db)["learned_terms"] == []


def test_candidate_that_never_clears_the_bar_is_retired_after_timeout():
    db = fresh_db()
    run(_seed_market(db))
    run(_seed_topic(db, "stale term", "candidate", candidate_at=NOW - timedelta(days=22)))  # past 21-day timeout
    run(_seed_readings(db, "stale term", n_accounts=1))

    summary = _update(db)

    assert "stale term" in summary["retired"]
    assert _topic(db, "stale term")["status"] == "retired"
    assert "stale term" not in _market(db)["learned_terms"]


# ── monitored -> paused ──────────────────────────────────────────────────────

def test_monitored_topic_with_good_yield_is_left_alone():
    db = fresh_db()
    run(_seed_market(db, learned_terms=["latasha wig"]))
    run(_seed_topic(db, "latasha wig", "monitored", low_yield_windows=0))
    run(_seed_readings(db, "latasha wig", n_accounts=6))

    summary = _update(db)

    assert summary == {"candidates": [], "monitored": [], "retired": [], "paused": []}
    assert _topic(db, "latasha wig")["status"] == "monitored"
    assert "latasha wig" in _market(db)["learned_terms"]


def test_monitored_topic_pauses_after_consecutive_low_yield_windows():
    db = fresh_db()
    run(_seed_market(db, learned_terms=["dying term"]))
    run(_seed_topic(db, "dying term", "monitored", low_yield_windows=0))
    # No readings at all this window -> zero accounts, zero relevance: low yield.

    s1 = _update(db)
    assert s1["paused"] == [] and _topic(db, "dying term")["low_yield_windows"] == 1

    s2 = _update(db)
    assert s2["paused"] == [] and _topic(db, "dying term")["low_yield_windows"] == 2

    s3 = _update(db)  # pause_after_windows default is 3
    assert "dying term" in s3["paused"]
    assert _topic(db, "dying term")["status"] == "paused"
    assert "dying term" not in _market(db)["learned_terms"]


def test_a_good_window_in_between_resets_the_low_yield_streak():
    db = fresh_db()
    run(_seed_market(db, learned_terms=["flaky term"]))
    run(_seed_topic(db, "flaky term", "monitored", low_yield_windows=0))

    _update(db)  # low yield window 1, streak -> 1
    assert _topic(db, "flaky term")["low_yield_windows"] == 1

    run(_seed_readings(db, "flaky term", n_accounts=6))  # recovers
    _update(db)
    assert _topic(db, "flaky term")["low_yield_windows"] == 0
    assert _topic(db, "flaky term")["status"] == "monitored"


# ── paused topics are not re-evaluated ──────────────────────────────────────

def test_paused_topics_are_never_touched():
    db = fresh_db()
    run(_seed_market(db))
    run(_seed_topic(db, "retired-ish", "paused", low_yield_windows=5))
    run(_seed_readings(db, "retired-ish", n_accounts=20))  # plenty of evidence now

    summary = _update(db)

    assert summary == {"candidates": [], "monitored": [], "retired": [], "paused": []}
    assert _topic(db, "retired-ish")["status"] == "paused"  # left alone, no silent revival


# ── term_history audit trail (PRD §6.3 "full history", Appendix A) ─────────

def _history(db, topic="latasha wig"):
    return run(discovery.term_history(db, MARKET_KEY, topic))


class TestTermHistory:
    def test_automatic_promotion_is_recorded(self):
        db = fresh_db()
        run(_seed_market(db))
        run(_seed_topic(db, "latasha wig", "observed"))
        run(_seed_readings(db, "latasha wig", n_accounts=4))

        _update(db)

        hist = _history(db)
        assert len(hist) == 1
        assert hist[0]["old_status"] == "observed"
        assert hist[0]["new_status"] == "candidate"
        assert hist[0]["actor"] == "system"
        assert hist[0]["market"] == MARKET_KEY and hist[0]["topic"] == "latasha wig"

    def test_full_lifecycle_keeps_every_transition_not_just_the_latest(self):
        # The topic doc itself only ever holds current state — a term that's
        # promoted, then paused, then manually promoted again must not lose
        # the earlier decisions the way an overwrite-in-place would.
        db = fresh_db()
        run(_seed_market(db))
        run(_seed_topic(db, "latasha wig", "candidate", candidate_at=NOW - timedelta(days=2)))
        run(_seed_readings(db, "latasha wig", n_accounts=6, days_ago=1))
        run(_seed_readings(db, "latasha wig", n_accounts=2, days_ago=4))
        run(_seed_readings(db, "latasha wig", n_accounts=2, days_ago=7))
        _update(db)  # candidate -> monitored

        run(discovery.set_topic_status(db, MARKET_KEY, "latasha wig", "paused", "manual pause", "ops@uri",
                                       NOW + timedelta(minutes=1)))
        run(discovery.set_topic_status(db, MARKET_KEY, "latasha wig", "monitored", "revived", "ops@uri",
                                       NOW + timedelta(minutes=2)))

        hist = _history(db)
        assert len(hist) == 3
        # most recent first
        assert [h["new_status"] for h in hist] == ["monitored", "paused", "monitored"]
        assert hist[0]["actor"] == "ops@uri" and hist[0]["reason"] == "revived"

    def test_manual_override_records_actor_and_reason(self):
        db = fresh_db()
        run(_seed_market(db))
        run(_seed_topic(db, "latasha wig", "candidate"))

        run(discovery.set_topic_status(db, MARKET_KEY, "latasha wig", "monitored", "clearly on-topic", "curator@uri", NOW))

        hist = _history(db)
        assert len(hist) == 1
        assert hist[0] == {
            "market": MARKET_KEY, "topic": "latasha wig", "old_status": "candidate", "new_status": "monitored",
            "reason": "clearly on-topic", "actor": "curator@uri", "at": NOW,
        }

    def test_unrelated_topic_or_market_never_appears(self):
        db = fresh_db()
        run(_seed_market(db))
        run(_seed_topic(db, "latasha wig", "observed"))
        run(_seed_topic(db, "another wig", "observed"))
        run(_seed_readings(db, "latasha wig", n_accounts=4))
        run(_seed_readings(db, "another wig", n_accounts=4))

        _update(db)

        assert len(_history(db, "latasha wig")) == 1
        assert len(_history(db, "another wig")) == 1

    def test_no_history_yet_is_an_empty_list(self):
        db = fresh_db()
        assert _history(db) == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
