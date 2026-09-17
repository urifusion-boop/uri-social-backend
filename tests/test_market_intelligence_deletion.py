"""
Uri Market Intelligence — evidence deletion cascade tests (PRD §16, §21,
P0-14: "removed evidence disappears from all serving paths").
"""
import asyncio

import pytest

from app.agents.market_intelligence.deletion import delete_evidence_cascade
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
            if isinstance(val, dict):
                # only $in used in this module's real code path elsewhere; not needed here
                return False
            if key == "evidence_ids":
                # Mongo array-contains-scalar semantics.
                if val not in doc.get("evidence_ids", []):
                    return False
            elif doc.get(key) != val:
                return False
        return True

    async def find_one(self, query):
        for d in self.docs:
            if self._matches(d, query):
                return dict(d)
        return None

    def find(self, query, projection=None):
        return FakeCursor([dict(d) for d in self.docs if self._matches(d, query)])

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if self._matches(d, query):
                del self.docs[i]
                return

    async def delete_many(self, query):
        self.docs = [d for d in self.docs if not self._matches(d, query)]

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


def test_returns_none_for_missing_evidence():
    db = FakeDb({"mi_evidence": []})
    result = _run(delete_evidence_cascade("e404", "b1", db))
    assert result is None


def test_returns_none_for_cross_tenant_evidence():
    db = FakeDb({"mi_evidence": [{"id": "e1", "brand_id": "OTHER_BRAND"}]})
    result = _run(delete_evidence_cascade("e1", "b1", db))
    assert result is None
    # Not actually deleted — a 404-shaped failure must not have side effects.
    assert db["mi_evidence"].docs == [{"id": "e1", "brand_id": "OTHER_BRAND"}]


def test_deletes_evidence_and_its_classification():
    db = FakeDb({
        "mi_evidence": [{"id": "e1", "brand_id": "b1", "text": "hello"}],
        "mi_classifications": [{"evidence_id": "e1", "primary_type": "customer_concern"}],
        "mi_clusters": [],
        "mi_insights": [],
    })
    result = _run(delete_evidence_cascade("e1", "b1", db))
    assert result is not None
    assert db["mi_evidence"].docs == []
    assert db["mi_classifications"].docs == []


def test_shrinks_cluster_that_has_other_evidence():
    db = FakeDb({
        "mi_evidence": [{"id": "e1", "brand_id": "b1"}],
        "mi_classifications": [],
        "mi_clusters": [{"id": "c1", "brand_id": "b1", "evidence_ids": ["e1", "e2", "e3"]}],
        "mi_insights": [],
    })
    result = _run(delete_evidence_cascade("e1", "b1", db))
    assert result["clusters_updated"] == ["c1"]
    assert result["clusters_deleted"] == []
    assert db["mi_clusters"].docs[0]["evidence_ids"] == ["e2", "e3"]


def test_deletes_cluster_left_with_no_evidence():
    db = FakeDb({
        "mi_evidence": [{"id": "e1", "brand_id": "b1"}],
        "mi_classifications": [],
        "mi_clusters": [{"id": "c1", "brand_id": "b1", "evidence_ids": ["e1"]}],
        "mi_insights": [],
    })
    result = _run(delete_evidence_cascade("e1", "b1", db))
    assert result["clusters_deleted"] == ["c1"]
    assert db["mi_clusters"].docs == []


def test_insight_with_remaining_evidence_gets_coverage_note_not_retracted():
    db = FakeDb({
        "mi_evidence": [{"id": "e1", "brand_id": "b1"}],
        "mi_classifications": [],
        "mi_clusters": [],
        "mi_insights": [{"id": "i1", "brand_id": "b1", "status": "active", "evidence_ids": ["e1", "e2"]}],
    })
    result = _run(delete_evidence_cascade("e1", "b1", db))
    assert result["insights_updated"] == ["i1"]
    assert result["insights_retracted"] == []
    updated = db["mi_insights"].docs[0]
    assert updated["evidence_ids"] == ["e2"]
    assert updated["status"] == "active"
    assert "removed" in updated["coverage_note"]


def test_insight_left_with_no_evidence_is_retracted():
    db = FakeDb({
        "mi_evidence": [{"id": "e1", "brand_id": "b1"}],
        "mi_classifications": [],
        "mi_clusters": [],
        "mi_insights": [{"id": "i1", "brand_id": "b1", "status": "active", "evidence_ids": ["e1"]}],
    })
    result = _run(delete_evidence_cascade("e1", "b1", db))
    assert result["insights_retracted"] == ["i1"]
    assert db["mi_insights"].docs[0]["status"] == InsightStatus.RETRACTED.value


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
