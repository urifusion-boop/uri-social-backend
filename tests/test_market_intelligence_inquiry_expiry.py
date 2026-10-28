"""
Uri Market Intelligence — purchase-inquiry expiry/re-scoring
(insight_expiry.py). Confirmed live: 48 active insights, all
purchase_inquiry, all stuck with the exact pre-fix urgency reason string
forever, because nothing ever revisits an inquiry insight after it's
composed the way a cluster-backed insight gets revised on new evidence.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.agents.market_intelligence.insight_expiry import expire_stale_inquiries
from app.agents.market_intelligence.models import InsightStatus


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for d in list(self._docs):
            yield d


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    def _matches(self, doc, query):
        for key, val in query.items():
            if doc.get(key) != val:
                return False
        return True

    def find(self, query=None, projection=None):
        query = query or {}
        return FakeCursor([dict(d) for d in self.docs if self._matches(d, query)])

    async def find_one(self, query):
        for d in self.docs:
            if self._matches(d, query):
                return dict(d)
        return None

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]]):
        self._colls = {name: FakeCollection(docs) for name, docs in collections.items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


NOW = datetime(2026, 10, 28, 12, 0, 0)


def _evidence(evidence_id, hours_ago):
    return {
        "id": evidence_id, "brand_id": "b1", "user_id": "u1", "topic_id": "t1",
        "provider": "fake", "platform": "fake", "source_id": evidence_id,
        "text": "need this urgently", "author_handle": "@a1",
        "published_at": NOW - timedelta(hours=hours_ago), "collected_at": NOW,
    }


def _inquiry_insight(insight_id, evidence_id, is_urgent, reason):
    return {
        "id": insight_id, "brand_id": "b1", "topic_id": "t1", "type": "purchase_inquiry",
        "status": "active", "evidence_ids": [evidence_id],
        "urgency": {"is_urgent": is_urgent, "reason": reason, "deadline": None},
        "headline": "h", "observed_change": "oc", "business_implication": "bi", "suggested_next_step": "sns",
        "confidence": {"components": [], "total": 0, "band": "low"},
        "relevance": {"components": [], "total": 0, "band": "low"},
        "first_seen": NOW, "last_updated": NOW,
    }


def test_a_fresh_still_urgent_inquiry_is_left_unchanged():
    db = FakeDb({
        "mi_insights": [_inquiry_insight("i1", "e1", True, "within 12h urgent cutoff")],
        "mi_evidence": [_evidence("e1", 2)],
    })
    result = _run(expire_stale_inquiries(db, now=NOW))
    assert result == {"rescored": 0, "expired": 0, "unchanged": 1}
    assert db["mi_insights"].docs[0]["status"] == "active"
    assert db["mi_insights"].docs[0]["urgency"]["is_urgent"] is True


def test_the_literal_live_bug_a_stale_urgent_flag_gets_rescored_to_not_urgent():
    """Exactly the confirmed live case: an insight stored with the old,
    now-incorrect "within 48h freshness window" / is_urgent=True, whose
    real evidence is well past the urgent cutoff but still inside the
    eligibility window."""
    db = FakeDb({
        "mi_insights": [_inquiry_insight("i1", "e1", True, "within 48h freshness window")],
        "mi_evidence": [_evidence("e1", 20)],
    })
    result = _run(expire_stale_inquiries(db, now=NOW))
    assert result == {"rescored": 1, "expired": 0, "unchanged": 0}
    updated = db["mi_insights"].docs[0]
    assert updated["status"] == "active"
    assert updated["urgency"]["is_urgent"] is False


def test_an_inquiry_past_the_freshness_window_is_expired():
    db = FakeDb({
        "mi_insights": [_inquiry_insight("i1", "e1", True, "within 48h freshness window")],
        "mi_evidence": [_evidence("e1", 72)],
    })
    result = _run(expire_stale_inquiries(db, now=NOW))
    assert result == {"rescored": 0, "expired": 1, "unchanged": 0}
    assert db["mi_insights"].docs[0]["status"] == InsightStatus.EXPIRED.value


def test_non_inquiry_insights_are_left_alone():
    db = FakeDb({
        "mi_insights": [
            {"id": "c1", "brand_id": "b1", "type": "customer_concern", "status": "active", "evidence_ids": ["e1"],
             "urgency": {"is_urgent": False, "reason": "n/a", "deadline": None}},
        ],
        "mi_evidence": [_evidence("e1", 200)],
    })
    result = _run(expire_stale_inquiries(db, now=NOW))
    assert result == {"rescored": 0, "expired": 0, "unchanged": 0}
    assert db["mi_insights"].docs[0]["status"] == "active"


def test_already_inactive_inquiries_are_not_touched():
    db = FakeDb({
        "mi_insights": [{**_inquiry_insight("i1", "e1", True, "x"), "status": "dismissed"}],
        "mi_evidence": [_evidence("e1", 200)],
    })
    result = _run(expire_stale_inquiries(db, now=NOW))
    assert result == {"rescored": 0, "expired": 0, "unchanged": 0}
    assert db["mi_insights"].docs[0]["status"] == "dismissed"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
