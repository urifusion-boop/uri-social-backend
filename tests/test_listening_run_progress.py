"""
GET /listening/run/active — real, in-progress collection state for a
business's markets, for a UI that wants to show the user something is
genuinely happening (not a simulated percentage) while "Check now" runs.
Uses an in-memory MongoDB; no network, no AI.
"""
import asyncio
from datetime import datetime

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening.router import active_runs

BRAND_ID, USER_ID = "brand-1", "user-1"
NOW = datetime(2026, 10, 22, 12, 0)
CTX = {"brand_id": BRAND_ID, "user_id": USER_ID}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["run_progress_test"]


async def _profile(db, markets=("hair_wigs:NG",)):
    await db["mi_listening_profiles"].insert_one({
        "brand_id": BRAND_ID, "user_id": USER_ID, "status": "active", "markets": list(markets),
    })


async def _run_doc(db, market, status="running", **extra):
    doc = {"id": f"run-{market}-{status}", "market": market, "status": status, "started_at": NOW,
          "per_query": [{"id": "q1", "q": "(wig)", "kind": "core", "role": "sample", "status": "pending",
                        "items": None, "capped": None, "cost": None, "error": None}]}
    doc.update(extra)
    await db["mi_listen_runs"].insert_one(doc)


class TestActiveRuns:
    @pytest.mark.asyncio
    async def test_returns_running_runs_for_this_brands_subscribed_markets(self):
        db = fresh_db()
        await _profile(db)
        await _run_doc(db, "hair_wigs:NG", status="running")

        result = await active_runs(ctx=CTX, db=db)

        assert result["status"] is True
        assert len(result["responseData"]) == 1
        assert result["responseData"][0]["market"] == "hair_wigs:NG"
        assert result["responseData"][0]["per_query"][0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_completed_runs_are_not_returned(self):
        # Same "falsy data -> status False, 404" convention every other
        # listing endpoint in this API uses (e.g. GET /listening/cards) —
        # not a crash, nothing run-progress-specific to special-case here.
        db = fresh_db()
        await _profile(db)
        await _run_doc(db, "hair_wigs:NG", status="completed")

        result = await active_runs(ctx=CTX, db=db)

        assert result["status"] is False
        assert result["responseCode"] == 404

    @pytest.mark.asyncio
    async def test_another_markets_run_is_not_returned(self):
        db = fresh_db()
        await _profile(db, markets=("hair_wigs:NG",))
        await _run_doc(db, "beauty_skincare:NG", status="running")  # not a market this brand subscribes to

        result = await active_runs(ctx=CTX, db=db)

        assert result["status"] is False

    @pytest.mark.asyncio
    async def test_no_listening_profile_yet_is_an_empty_list_not_an_error(self):
        db = fresh_db()
        result = await active_runs(ctx=CTX, db=db)
        assert result["status"] is False


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
