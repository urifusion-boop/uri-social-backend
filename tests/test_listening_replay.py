"""
Offline replay (PRD §17): detectors are replayed over already-stored
history with a candidate config, compared against what actually shipped,
before a threshold/weight change goes live. Confirms persist=False on
detect_market never writes to mi_listen_signals, and that replay_market
correctly diffs a candidate run against either the real stored signals or
a second, equally-unpersisted config. Uses an in-memory MongoDB; no
network, no AI.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from fastapi import HTTPException

from app.agents.market_intelligence.listening import detect, replay
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge
from app.agents.market_intelligence.listening.router import replay_detection

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
    return mongomock_motor.AsyncMongoMockClient()["replay_test"]


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


class TestPersistFlag:
    def test_persist_false_never_writes_signals(self):
        db = fresh_db()
        run(_seed(db, _rising_history()))
        signals = run(detect.detect_market(db, MARKET, CFG, NOW, persist=False))
        assert signals  # the detection itself still ran and found something
        assert run(db["mi_listen_signals"].count_documents({})) == 0

    def test_persist_true_is_still_the_default(self):
        db = fresh_db()
        run(_seed(db, _rising_history()))
        signals = run(detect.detect_market(db, MARKET, CFG, NOW))
        assert signals
        assert run(db["mi_listen_signals"].count_documents({})) == len(signals)


class TestReplayMarket:
    def test_candidate_agrees_with_what_actually_shipped(self):
        db = fresh_db()
        run(_seed(db, _rising_history()))
        run(detect.detect_market(db, MARKET, CFG, NOW))  # the "live" run, persisted

        result = run(replay.replay_market(db, MARKET, CFG, NOW))

        assert result["new_signals"] == [] and result["missed_signals"] == []
        assert result["agreement_count"] > 0
        assert result["candidate_signal_count"] == result["compared_signal_count"]

    def test_a_stricter_candidate_misses_what_actually_shipped(self):
        db = fresh_db()
        run(_seed(db, _rising_history()))
        run(detect.detect_market(db, MARKET, CFG, NOW))  # live run finds "rising"

        strict_cfg = _merge(CFG, {"detect": {"rising_z": 999}})  # impossible to clear
        result = run(replay.replay_market(db, MARKET, strict_cfg, NOW))

        assert result["new_signals"] == []
        assert any(m["type"] == "rising" for m in result["missed_signals"])

    def test_a_looser_candidate_catches_more_than_what_shipped(self):
        db = fresh_db()
        run(_seed(db, _rising_history()))
        # Live run persisted with a strict config that found no "rising"
        # signal (its recurring_need detector is unaffected by rising_z,
        # so it still shipped that one on its own).
        strict_cfg = _merge(CFG, {"detect": {"rising_z": 999}})
        run(detect.detect_market(db, MARKET, strict_cfg, NOW))

        result = run(replay.replay_market(db, MARKET, CFG, NOW))

        assert any(n["type"] == "rising" for n in result["new_signals"])
        assert result["missed_signals"] == []
        assert result["compared_signal_count"] == 1

    def test_comparing_two_unpersisted_configs_head_to_head(self):
        db = fresh_db()
        run(_seed(db, _rising_history()))
        strict_cfg = _merge(CFG, {"detect": {"rising_z": 999}})

        result = run(replay.replay_market(db, MARKET, CFG, NOW, actual_cfg=strict_cfg))

        assert run(db["mi_listen_signals"].count_documents({})) == 0  # neither side persisted
        assert any(n["type"] == "rising" for n in result["new_signals"])

    def test_no_history_at_all_is_a_clean_zero_comparison(self):
        db = fresh_db()
        run(db["mi_listen_runs"].insert_one({"market": MARKET["key"], "status": "completed",
                                             "covered_from": NOW - timedelta(days=12), "until": NOW,
                                             "started_at": NOW}))
        result = run(replay.replay_market(db, MARKET, CFG, NOW))
        assert result == {
            "market": MARKET["key"], "day": NOW.strftime("%Y-%m-%d"),
            "candidate_signal_count": 0, "compared_signal_count": 0,
            "new_signals": [], "missed_signals": [], "agreement_count": 0,
        }


class TestReplayRange:
    def test_replays_every_day_given(self):
        db = fresh_db()
        run(_seed(db, _rising_history()))
        days = [NOW, NOW - timedelta(days=1)]
        results = run(replay.replay_range(db, MARKET, CFG, days))
        assert len(results) == 2
        assert {r["day"] for r in results} == {NOW.strftime("%Y-%m-%d"), (NOW - timedelta(days=1)).strftime("%Y-%m-%d")}


class TestReplayRouter:
    @pytest.mark.asyncio
    async def test_market_not_found_is_404(self):
        db = fresh_db()

        class _Body:
            market, at, candidate_overrides = "ghost:NG", ["2026-10-21T12:00:00"], {}
        with pytest.raises(HTTPException) as exc:
            await replay_detection(_Body(), admin={"claims": {}}, db=db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_at_list_is_400(self):
        db = fresh_db()
        await db["mi_markets"].insert_one(dict(MARKET))

        class _Body:
            market, at, candidate_overrides = MARKET["key"], [], {}
        with pytest.raises(HTTPException) as exc:
            await replay_detection(_Body(), admin={"claims": {}}, db=db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_bad_timestamp_is_400(self):
        db = fresh_db()
        await db["mi_markets"].insert_one(dict(MARKET))

        class _Body:
            market, at, candidate_overrides = MARKET["key"], ["not-a-timestamp"], {}
        with pytest.raises(HTTPException) as exc:
            await replay_detection(_Body(), admin={"claims": {}}, db=db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_happy_path_merges_overrides_and_delegates(self):
        db = fresh_db()
        await db["mi_markets"].insert_one(dict(MARKET))

        class _Body:
            market = MARKET["key"]
            at = ["2026-10-21T12:00:00"]
            candidate_overrides = {"detect": {"rising_z": 999}}

        with patch(
            "app.agents.market_intelligence.listening.replay.replay_range", new=AsyncMock(return_value=["ok"])
        ) as mock_range:
            result = await replay_detection(_Body(), admin={"claims": {}}, db=db)

        assert result["responseData"] == ["ok"]
        passed_cfg = mock_range.call_args.args[2]
        assert passed_cfg["detect"]["rising_z"] == 999
        assert passed_cfg["detect"]["min_authors"] == DEFAULTS["detect"]["min_authors"]  # untouched defaults kept


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
