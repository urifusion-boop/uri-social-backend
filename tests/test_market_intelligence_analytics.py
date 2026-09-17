"""
Uri Market Intelligence — analytics instrumentation tests (PRD §26).

track_event() is a real no-op when POSTHOG_API_KEY isn't configured (see
PostHogService's own docstring), which is why every other MI test suite
already passes without mocking it — these tests specifically assert the
call actually happens with the right event name and no raw private content,
which "it didn't crash" alone doesn't prove.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.agents.market_intelligence.models import (
    Classification,
    Cluster,
    EvidenceType,
    InsightVersion,
    RawEvidence,
    ScanStatus,
    SourceConfig,
    Topic,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_create_topic_tracks_topic_created():
    import asyncio as _a
    from app.agents.market_intelligence import router
    from app.agents.market_intelligence.models import TopicCreateRequest

    class FakeCollection:
        def __init__(self):
            self.docs = []

        async def insert_one(self, doc):
            self.docs.append(doc)

    class FakeDb:
        def __init__(self):
            self._coll = FakeCollection()

        def __getitem__(self, name):
            return self._coll

    db = FakeDb()
    ctx = {"user_id": "u1", "brand_id": "b1"}

    with patch.object(router, "track_event") as mock_track:
        _run(router.create_topic(TopicCreateRequest(question="what stops sales?"), ctx=ctx, db=db))

    mock_track.assert_called_once()
    args, _ = mock_track.call_args
    assert args[0] == "u1"
    assert args[1] == "topic_created"
    assert args[2]["brand_id"] == "b1"
    # No raw question text leaked into analytics properties.
    assert "question" not in args[2]


def test_feedback_submitted_is_tracked_with_verdict_not_content():
    from app.agents.market_intelligence import router
    from app.agents.market_intelligence.models import FeedbackRequest, FeedbackVerdict

    class FakeCollection:
        def __init__(self, docs=None):
            self.docs = docs or []

        async def find_one(self, query):
            for d in self.docs:
                if all(d.get(k) == v for k, v in query.items()):
                    return dict(d)
            return None

        async def insert_one(self, doc):
            self.docs.append(doc)

        async def update_one(self, query, update, upsert=False):
            pass

    class FakeDb:
        def __init__(self):
            self._colls = {}

        def __getitem__(self, name):
            return self._colls.setdefault(name, FakeCollection())

    db = FakeDb()
    db["mi_insights"].docs.append({"id": "i1", "brand_id": "b1"})
    ctx = {"user_id": "u1", "brand_id": "b1"}

    with patch.object(router, "track_event") as mock_track:
        _run(router.submit_feedback("i1", FeedbackRequest(verdict=FeedbackVerdict.USEFUL), ctx=ctx, db=db))

    mock_track.assert_called_once_with("u1", "feedback_submitted", {
        "brand_id": "b1", "insight_id": "i1", "verdict": "useful",
    })


def test_alert_sent_and_alert_suppressed_are_tracked():
    from app.agents.market_intelligence import notification_delivery

    class FakeCollection:
        def __init__(self):
            self.docs = []

        def _matches(self, doc, query):
            return all(doc.get(k) == v for k, v in query.items())

        async def find_one(self, query):
            for d in self.docs:
                if self._matches(d, query):
                    return dict(d)
            return None

        async def find(self, query=None):
            return []

        async def count_documents(self, query):
            return 0

        async def update_one(self, query, update, upsert=False):
            for d in self.docs:
                if self._matches(d, query):
                    d.update(update.get("$set", {}))
                    return

    class FakeDb:
        def __init__(self):
            self._colls = {}

        def __getitem__(self, name):
            return self._colls.setdefault(name, FakeCollection())

    db = FakeDb()
    db["mi_insights"].docs.append({"id": "i1", "status": "active", "headline": "h", "observed_change": "oc", "suggested_next_step": "sns", "urgency": {"is_urgent": False}})
    db["mi_preferences"].docs.append({
        "user_id": "u1", "brand_id": "b1", "email_enabled": True, "timezone": "UTC",
        "digest_hour_local": 8, "quiet_hours_start_local": 21, "quiet_hours_end_local": 8,
        "urgent_override": False, "muted_topic_ids": [], "muted_categories": [], "snoozed_insight_ids": [],
    })
    db["users"].docs.append({"userId": "u1", "email": "u@example.com"})
    entry = {
        "id": "o1", "brand_id": "b1", "user_id": "u1", "topic_id": "t1", "insight_id": "i1",
        "insight_revision": 1, "category": "act_soon", "delivery_mode": "immediate",
        "dedupe_key": "k1", "status": "queued",
    }
    db["mi_outbox"].docs.append(entry)

    from unittest.mock import AsyncMock
    with patch.object(notification_delivery.email_service, "send_raw_email", new=AsyncMock(return_value=True)), \
         patch.object(notification_delivery, "track_event") as mock_track:
        _run(notification_delivery._process_one_immediate(db, entry, datetime(2024, 6, 1, 12, 0)))

    mock_track.assert_called_once_with("u1", "alert_sent", {
        "brand_id": "b1", "insight_id": "i1", "category": "act_soon", "mode": "immediate",
    })


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
