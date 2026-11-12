"""
Golden datasets (PRD §17): "Golden datasets per category and language...
labelled for relevance, intent, sentiment and buying intent; refreshed
continuously from curator corrections." A curator marks a hand-confirmed
reading as golden; it's snapshotted at that moment so a later edit to the
live reading never silently changes what the golden example represents.
Uses an in-memory MongoDB; no network, no AI.
"""
import asyncio
from datetime import datetime

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from fastapi import HTTPException

from app.agents.market_intelligence.listening import golden
from app.agents.market_intelligence.listening.router import (
    list_golden_examples, mark_golden_example, unmark_golden_example,
)

NOW = datetime(2026, 10, 21, 12, 0)
MARKET_KEY = "hair_wigs:NG"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["golden_test"]


async def _reading(db, sid, market=MARKET_KEY, on_topic=True, intent="purchase", confidence=0.95):
    await db["mi_listen_readings"].insert_one({
        "source_id": sid, "market": market, "on_topic": on_topic, "intent": intent, "sentiment": "neutral",
        "buying_intent": "high", "topics": ["latasha wig"], "confidence": confidence, "prompt_version": "v2",
    })
    await db["mi_listen_items"].insert_one({"source_id": sid, "text": f"post {sid}"})


ADMIN = {"claims": {"email": "curator@urisocial.com"}}


class TestMarkGolden:
    def test_marking_a_reading_snapshots_its_current_labels(self):
        db = fresh_db()
        run(_reading(db, "s1"))

        result = run(golden.mark_golden(db, "s1", MARKET_KEY, "curator@x.com", now=NOW))

        assert result == {"found": True, "marked": True}
        doc = run(db["mi_golden_examples"].find_one({"source_id": "s1"}))
        assert doc["on_topic"] is True and doc["intent"] == "purchase"
        assert doc["text"] == "post s1" and doc["marked_by"] == "curator@x.com" and doc["marked_at"] == NOW

    def test_marking_an_unknown_reading_returns_not_found(self):
        db = fresh_db()
        assert run(golden.mark_golden(db, "ghost", MARKET_KEY, "curator@x.com")) == {"found": False}

    def test_a_later_reading_correction_does_not_change_an_already_marked_example(self):
        db = fresh_db()
        run(_reading(db, "s1", intent="purchase"))
        run(golden.mark_golden(db, "s1", MARKET_KEY, "curator@x.com", now=NOW))

        run(db["mi_listen_readings"].update_one({"source_id": "s1"}, {"$set": {"intent": "complaint"}}))

        snapshot = run(db["mi_golden_examples"].find_one({"source_id": "s1"}))
        assert snapshot["intent"] == "purchase"  # frozen at mark time

    def test_marking_twice_overwrites_with_the_latest_snapshot(self):
        db = fresh_db()
        run(_reading(db, "s1", intent="purchase"))
        run(golden.mark_golden(db, "s1", MARKET_KEY, "curator@x.com"))
        run(db["mi_listen_readings"].update_one({"source_id": "s1"}, {"$set": {"intent": "complaint"}}))
        run(golden.mark_golden(db, "s1", MARKET_KEY, "curator@y.com"))

        assert run(db["mi_golden_examples"].count_documents({"source_id": "s1"})) == 1
        doc = run(db["mi_golden_examples"].find_one({"source_id": "s1"}))
        assert doc["intent"] == "complaint" and doc["marked_by"] == "curator@y.com"


class TestUnmarkAndList:
    def test_unmarking_removes_it(self):
        db = fresh_db()
        run(_reading(db, "s1"))
        run(golden.mark_golden(db, "s1", MARKET_KEY, "curator@x.com"))
        assert run(golden.unmark_golden(db, "s1", MARKET_KEY)) == {"removed": True}
        assert run(db["mi_golden_examples"].count_documents({})) == 0

    def test_unmarking_something_not_marked_is_a_no_op(self):
        db = fresh_db()
        assert run(golden.unmark_golden(db, "ghost", MARKET_KEY)) == {"removed": False}

    def test_list_filters_by_market(self):
        db = fresh_db()
        run(_reading(db, "s1", market=MARKET_KEY))
        run(_reading(db, "s2", market="beauty_skincare:NG"))
        run(golden.mark_golden(db, "s1", MARKET_KEY, "x"))
        run(golden.mark_golden(db, "s2", "beauty_skincare:NG", "x"))

        assert [g["source_id"] for g in run(golden.list_golden(db, market=MARKET_KEY))] == ["s1"]
        assert len(run(golden.list_golden(db))) == 2


class TestGoldenRouter:
    @pytest.mark.asyncio
    async def test_mark_endpoint(self):
        db = fresh_db()
        await _reading(db, "s1")
        result = await mark_golden_example(MARKET_KEY, "s1", admin=ADMIN, db=db)
        assert result["responseData"]["marked"] is True

    @pytest.mark.asyncio
    async def test_mark_endpoint_404s_on_missing_reading(self):
        db = fresh_db()
        with pytest.raises(HTTPException) as exc:
            await mark_golden_example(MARKET_KEY, "ghost", admin=ADMIN, db=db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_unmark_endpoint_404s_when_not_marked(self):
        db = fresh_db()
        with pytest.raises(HTTPException) as exc:
            await unmark_golden_example(MARKET_KEY, "ghost", admin=ADMIN, db=db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_list_endpoint(self):
        db = fresh_db()
        await _reading(db, "s1")
        await golden.mark_golden(db, "s1", MARKET_KEY, "x")
        result = await list_golden_examples(market=None, limit=200, admin=ADMIN, db=db)
        assert [g["source_id"] for g in result["responseData"]] == ["s1"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
