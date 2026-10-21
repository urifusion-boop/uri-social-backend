"""
Human review queue (PRD §17): "low-confidence classifications... can be
reviewed by an internal curator; reviews feed training data." A reading
the AI was unsure about shows up until a curator resolves it; a
correction updates the reading and is logged for training, but doesn't
retroactively rewrite counts already computed from it (see review.py's
own docstring). Uses an in-memory MongoDB; no network, no AI.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from fastapi import HTTPException

from app.agents.market_intelligence.listening import review
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge
from app.agents.market_intelligence.listening.router import list_pending_reviews, resolve_pending_review

NOW = datetime(2026, 10, 21, 12, 0)
CFG = _merge(DEFAULTS, {})
MARKET_KEY = "hair_wigs:NG"
THRESHOLD = CFG["review"]["confidence_threshold"]


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["review_test"]


async def _reading(db, sid, confidence, market=MARKET_KEY, days_ago=1, reviewed=False, on_topic=True,
                   intent="question", topics=None):
    doc = {"source_id": sid, "market": market, "confidence": confidence, "published_at": NOW - timedelta(days=days_ago),
           "reviewed": reviewed, "on_topic": on_topic, "intent": intent, "topics": topics or ["latasha wig"]}
    await db["mi_listen_readings"].insert_one(dict(doc))
    await db["mi_listen_items"].insert_one({"source_id": sid, "text": f"post {sid}", "url": f"https://x.com/{sid}",
                                            "author_handle": "@a"})


ADMIN = {"claims": {"email": "curator@urisocial.com"}}


# ── pending_reviews ───────────────────────────────────────────────────────────

class TestPendingReviews:
    def test_low_confidence_unreviewed_readings_are_listed(self):
        db = fresh_db()
        run(_reading(db, "s1", confidence=THRESHOLD - 0.1))
        items = run(review.pending_reviews(db, CFG, now=NOW))
        assert len(items) == 1 and items[0]["source_id"] == "s1"
        assert items[0]["text"] == "post s1"

    def test_high_confidence_readings_are_excluded(self):
        db = fresh_db()
        run(_reading(db, "s1", confidence=THRESHOLD + 0.1))
        assert run(review.pending_reviews(db, CFG, now=NOW)) == []

    def test_already_reviewed_readings_are_excluded(self):
        db = fresh_db()
        run(_reading(db, "s1", confidence=THRESHOLD - 0.1, reviewed=True))
        assert run(review.pending_reviews(db, CFG, now=NOW)) == []

    def test_outside_the_window_is_excluded(self):
        db = fresh_db()
        run(_reading(db, "s1", confidence=THRESHOLD - 0.1, days_ago=CFG["review"]["window_days"] + 1))
        assert run(review.pending_reviews(db, CFG, now=NOW)) == []

    def test_can_filter_by_market(self):
        db = fresh_db()
        run(_reading(db, "s1", confidence=0.1, market=MARKET_KEY))
        run(_reading(db, "s2", confidence=0.1, market="beauty_skincare:NG"))
        items = run(review.pending_reviews(db, CFG, market=MARKET_KEY, now=NOW))
        assert [i["source_id"] for i in items] == ["s1"]

    def test_most_uncertain_first(self):
        db = fresh_db()
        run(_reading(db, "mid", confidence=0.3))
        run(_reading(db, "worst", confidence=0.05))
        run(_reading(db, "best-of-the-low", confidence=0.45))
        items = run(review.pending_reviews(db, CFG, now=NOW))
        assert [i["source_id"] for i in items] == ["worst", "mid", "best-of-the-low"]


# ── resolve_review ────────────────────────────────────────────────────────────

class TestResolveReview:
    def test_correcting_on_topic_updates_the_reading_and_logs_it(self):
        db = fresh_db()
        run(_reading(db, "s1", confidence=0.2, on_topic=True))

        result = run(review.resolve_review(db, "s1", MARKET_KEY, "curator@x.com",
                                           corrected_on_topic=False, note="Actually a different wig brand"))

        assert result == {"found": True, "corrected_fields": ["on_topic"]}
        reading = run(db["mi_listen_readings"].find_one({"source_id": "s1"}))
        assert reading["on_topic"] is False and reading["reviewed"] is True and reading["reviewed_by"] == "curator@x.com"
        log = run(db["mi_listen_review_corrections"].find_one({"source_id": "s1"}))
        assert log["corrections"] == {"on_topic": False}
        assert log["original"]["on_topic"] is True
        assert log["note"] == "Actually a different wig brand"

    def test_resolving_without_changing_anything_still_marks_it_reviewed(self):
        db = fresh_db()
        run(_reading(db, "s1", confidence=0.2, on_topic=True))

        result = run(review.resolve_review(db, "s1", MARKET_KEY, "curator@x.com", corrected_on_topic=True))

        assert result == {"found": True, "corrected_fields": []}
        reading = run(db["mi_listen_readings"].find_one({"source_id": "s1"}))
        assert reading["reviewed"] is True

    def test_resolving_a_reading_removes_it_from_the_queue(self):
        db = fresh_db()
        run(_reading(db, "s1", confidence=0.2))
        run(review.resolve_review(db, "s1", MARKET_KEY, "curator@x.com", corrected_on_topic=False))
        assert run(review.pending_reviews(db, CFG, now=NOW)) == []

    def test_unknown_reading_returns_not_found(self):
        db = fresh_db()
        result = run(review.resolve_review(db, "ghost", MARKET_KEY, "curator@x.com"))
        assert result == {"found": False}

    def test_does_not_touch_topic_counts(self):
        db = fresh_db()
        run(_reading(db, "s1", confidence=0.2, on_topic=True))
        run(db["mi_topic_counts"].insert_one({"market": MARKET_KEY, "topic": "latasha wig",
                                              "bucket": NOW, "items": 5}))
        run(review.resolve_review(db, "s1", MARKET_KEY, "curator@x.com", corrected_on_topic=False))
        counts = run(db["mi_topic_counts"].find_one({"market": MARKET_KEY, "topic": "latasha wig"}))
        assert counts["items"] == 5  # unchanged — corrections don't rewrite history


# ── router endpoints ──────────────────────────────────────────────────────────

class TestReviewRouter:
    @pytest.mark.asyncio
    async def test_list_pending_reviews_endpoint(self):
        db = fresh_db()
        await _reading(db, "s1", confidence=0.1)
        result = await list_pending_reviews(market=None, limit=50, admin=ADMIN, db=db)
        assert [r["source_id"] for r in result["responseData"]] == ["s1"]

    @pytest.mark.asyncio
    async def test_resolve_endpoint_records_the_admins_email(self):
        db = fresh_db()
        await _reading(db, "s1", confidence=0.1)

        class _Body:
            on_topic, intent, topics, note = False, None, None, "Not relevant"
        result = await resolve_pending_review(MARKET_KEY, "s1", _Body(), admin=ADMIN, db=db)

        assert result["responseData"]["corrected_fields"] == ["on_topic"]
        log = await db["mi_listen_review_corrections"].find_one({"source_id": "s1"})
        assert log["actor"] == "curator@urisocial.com"

    @pytest.mark.asyncio
    async def test_resolve_endpoint_404s_on_missing_reading(self):
        db = fresh_db()

        class _Body:
            on_topic, intent, topics, note = None, None, None, None
        with pytest.raises(HTTPException) as exc:
            await resolve_pending_review(MARKET_KEY, "ghost", _Body(), admin=ADMIN, db=db)
        assert exc.value.status_code == 404


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
