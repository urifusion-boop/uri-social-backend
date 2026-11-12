"""
Uri Market Intelligence — topic deletion cascade tests.

No DELETE endpoint for a topic existed anywhere in this product before
delete_topic_cascade — confirmed live, two real exact-duplicate topics
(the same standing question, created minutes apart) had no way to be
removed. Mirrors test_market_intelligence_deletion.py's fake-db pattern
for the evidence cascade, extended with $in support (needed here for the
evidence_id/insight_id joins) and a delete_many that returns deleted_count.
"""
import asyncio

import pytest

from app.agents.market_intelligence.deletion import delete_topic_cascade


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _DeleteResult:
    def __init__(self, deleted_count: int):
        self.deleted_count = deleted_count


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
            if isinstance(val, dict) and "$in" in val:
                if doc.get(key) not in val["$in"]:
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
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._matches(d, query)]
        return _DeleteResult(before - len(self.docs))


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]]):
        self._colls = {name: FakeCollection(docs) for name, docs in collections.items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def _db(**overrides):
    base = {
        "mi_topics": [], "mi_evidence": [], "mi_classifications": [], "mi_clusters": [],
        "mi_insights": [], "mi_developments": [], "mi_scans": [], "mi_term_stats": [], "mi_outbox": [],
        "mi_briefs": [],
    }
    base.update(overrides)
    return FakeDb(base)


def test_returns_none_for_missing_topic():
    db = _db()
    assert _run(delete_topic_cascade("t404", "b1", db)) is None


def test_returns_none_for_cross_tenant_topic():
    db = _db(mi_topics=[{"id": "t1", "brand_id": "OTHER_BRAND"}])
    result = _run(delete_topic_cascade("t1", "b1", db))
    assert result is None
    # Not actually deleted — a 404-shaped failure must not have side effects.
    assert db["mi_topics"].docs == [{"id": "t1", "brand_id": "OTHER_BRAND"}]


def test_deletes_the_topic_itself():
    db = _db(mi_topics=[{"id": "t1", "brand_id": "b1", "question": "q"}])
    result = _run(delete_topic_cascade("t1", "b1", db))
    assert result is not None
    assert db["mi_topics"].docs == []


def test_cascades_into_evidence_classifications_clusters_insights_developments():
    db = _db(
        mi_topics=[{"id": "t1", "brand_id": "b1", "question": "q"}],
        mi_evidence=[{"id": "e1", "topic_id": "t1"}, {"id": "e2", "topic_id": "t1"}, {"id": "e3", "topic_id": "OTHER"}],
        mi_classifications=[{"evidence_id": "e1"}, {"evidence_id": "e2"}, {"evidence_id": "e3"}],
        mi_clusters=[{"id": "c1", "topic_id": "t1"}, {"id": "c2", "topic_id": "OTHER"}],
        mi_insights=[{"id": "i1", "topic_id": "t1"}, {"id": "i2", "topic_id": "OTHER"}],
        mi_developments=[{"id": "d1", "topic_id": "t1"}],
        mi_briefs=[{"id": "br1", "insight_id": "i1"}, {"id": "br2", "insight_id": "i2"}],
    )
    result = _run(delete_topic_cascade("t1", "b1", db))

    assert result["evidence_deleted"] == 2
    assert result["clusters_deleted"] == 1
    assert result["insights_deleted"] == 1
    assert result["developments_deleted"] == 1

    assert [e["id"] for e in db["mi_evidence"].docs] == ["e3"]
    assert [c["evidence_id"] for c in db["mi_classifications"].docs] == ["e3"]
    assert [c["id"] for c in db["mi_clusters"].docs] == ["c2"]
    assert [i["id"] for i in db["mi_insights"].docs] == ["i2"]
    assert db["mi_developments"].docs == []
    # The brief for the deleted topic's own insight goes with it; the
    # other brand/topic's brief (i2, untouched) stays.
    assert [b["id"] for b in db["mi_briefs"].docs] == ["br2"]


def test_other_topics_and_brands_are_left_alone():
    db = _db(
        mi_topics=[{"id": "t1", "brand_id": "b1", "question": "q"}, {"id": "t2", "brand_id": "b1", "question": "other"}],
        mi_evidence=[{"id": "e1", "topic_id": "t2"}],
    )
    _run(delete_topic_cascade("t1", "b1", db))
    assert [t["id"] for t in db["mi_topics"].docs] == ["t2"]
    assert [e["id"] for e in db["mi_evidence"].docs] == ["e1"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
