"""
Ontology curation (PRD §6.3's curation console, §17's human review queue):
a curator can promote a candidate straight to monitored, or demote/retire
a topic directly, instead of waiting on (or in addition to) the automatic
evidence-based thresholds in discovery.update_market(). Admin-only — a
market's topics are shared across every subscribed business, not any one
tenant's own data. Uses an in-memory MongoDB; no network, no AI.
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from fastapi import HTTPException

from app.agents.market_intelligence.listening import discovery
from app.agents.market_intelligence.listening.router import curation_decide, curation_topics

NOW = datetime(2026, 10, 21, 12, 0)
MARKET_KEY = "hair_wigs:NG"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["curation_test"]


async def _seed_topic(db, key, status, **extra):
    doc = {"market": MARKET_KEY, "key": key, "status": status, "last_seen": NOW}
    doc.update(extra)
    await db["mi_market_topics"].insert_one(dict(doc))


async def _seed_market(db, learned_terms=None):
    await db["mi_markets"].insert_one({"key": MARKET_KEY, "learned_terms": learned_terms or []})


def _topic(db, key):
    return run(db["mi_market_topics"].find_one({"market": MARKET_KEY, "key": key}))


def _market(db):
    return run(db["mi_markets"].find_one({"key": MARKET_KEY}))


ADMIN = {"claims": {"email": "curator@urisocial.com"}}


# ── discovery.set_topic_status ───────────────────────────────────────────────

class TestSetTopicStatus:
    def test_promoting_a_candidate_straight_to_monitored_adds_learned_term(self):
        db = fresh_db()
        run(_seed_market(db))
        run(_seed_topic(db, "latasha wig", "candidate"))

        result = run(discovery.set_topic_status(db, MARKET_KEY, "latasha wig", "monitored",
                                                reason="Clearly a real product name", actor="curator@x.com"))

        assert result == {"found": True, "status": "monitored", "market": MARKET_KEY, "topic": "latasha wig"}
        assert _topic(db, "latasha wig")["status"] == "monitored"
        assert _topic(db, "latasha wig")["decided_by"] == "curator@x.com"
        assert _topic(db, "latasha wig")["decision_reason"] == "Clearly a real product name"
        assert "latasha wig" in _market(db)["learned_terms"]

    def test_retiring_a_monitored_topic_removes_it_from_learned_terms(self):
        db = fresh_db()
        run(_seed_market(db, learned_terms=["bad term"]))
        run(_seed_topic(db, "bad term", "monitored"))

        result = run(discovery.set_topic_status(db, MARKET_KEY, "bad term", "retired", reason="Not actually a product"))

        assert result["status"] == "retired"
        assert _topic(db, "bad term")["status"] == "retired"
        assert "bad term" not in _market(db)["learned_terms"]

    def test_unknown_topic_returns_not_found(self):
        db = fresh_db()
        run(_seed_market(db))
        result = run(discovery.set_topic_status(db, MARKET_KEY, "ghost", "monitored"))
        assert result == {"found": False}

    def test_invalid_status_raises(self):
        db = fresh_db()
        with pytest.raises(ValueError):
            run(discovery.set_topic_status(db, MARKET_KEY, "x", "not-a-real-status"))


# ── router endpoints ──────────────────────────────────────────────────────────

class TestCurationRouter:
    @pytest.mark.asyncio
    async def test_lists_observed_and_candidate_topics_by_default(self):
        db = fresh_db()
        await db["mi_market_topics"].insert_many([
            {"market": MARKET_KEY, "key": "a", "status": "observed", "last_seen": NOW},
            {"market": MARKET_KEY, "key": "b", "status": "candidate", "last_seen": NOW},
            {"market": MARKET_KEY, "key": "c", "status": "monitored", "last_seen": NOW},
        ])
        result = await curation_topics(status=None, market=None, limit=50, admin=ADMIN, db=db)
        keys = {t["key"] for t in result["responseData"]}
        assert keys == {"a", "b"}

    @pytest.mark.asyncio
    async def test_filters_by_explicit_status_and_market(self):
        db = fresh_db()
        await db["mi_market_topics"].insert_many([
            {"market": MARKET_KEY, "key": "a", "status": "monitored", "last_seen": NOW},
            {"market": "beauty_skincare:NG", "key": "b", "status": "monitored", "last_seen": NOW},
        ])
        result = await curation_topics(status="monitored", market=MARKET_KEY, limit=50, admin=ADMIN, db=db)
        assert [t["key"] for t in result["responseData"]] == ["a"]

    @pytest.mark.asyncio
    async def test_invalid_status_filter_is_400(self):
        db = fresh_db()
        with pytest.raises(HTTPException) as exc:
            await curation_topics(status="not-a-status", market=None, limit=50, admin=ADMIN, db=db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_decide_promotes_and_records_the_admins_email_as_actor(self):
        db = fresh_db()
        await _seed_market(db)
        await _seed_topic(db, "latasha wig", "candidate")

        class _Body:
            status, reason = "monitored", "Looks real"
        result = await curation_decide(MARKET_KEY, "latasha wig", _Body(), admin=ADMIN, db=db)

        assert result["responseData"]["status"] == "monitored"
        stored = await db["mi_market_topics"].find_one({"market": MARKET_KEY, "key": "latasha wig"})
        assert stored["decided_by"] == "curator@urisocial.com"

    @pytest.mark.asyncio
    async def test_decide_on_missing_topic_is_404(self):
        db = fresh_db()

        class _Body:
            status, reason = "monitored", None
        with pytest.raises(HTTPException) as exc:
            await curation_decide(MARKET_KEY, "ghost", _Body(), admin=ADMIN, db=db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_decide_with_invalid_status_is_400(self):
        db = fresh_db()
        await _seed_topic(db, "latasha wig", "candidate")

        class _Body:
            status, reason = "not-a-status", None
        with pytest.raises(HTTPException) as exc:
            await curation_decide(MARKET_KEY, "latasha wig", _Body(), admin=ADMIN, db=db)
        assert exc.value.status_code == 400


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
