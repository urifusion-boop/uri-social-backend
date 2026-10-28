"""
Uri Market Intelligence — GET /scans/{id} now reports what a finished scan
actually produced (router.py's get_scan). Confirmed live UX gap: "Run
scan" -> "Scanning..." -> "Scan complete" told the owner nothing about
what was found or where to look — the real payoff (Insights, Announced
Events) lives on two entirely different tabs with no link pointing there.
"""
import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.agents.market_intelligence.router import get_scan


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
            if isinstance(val, dict) and "$in" in val:
                if doc.get(key) not in val["$in"]:
                    return False
            elif doc.get(key) != val:
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

    async def count_documents(self, query):
        return len([d for d in self.docs if self._matches_count(d, query)])

    def _matches_count(self, doc, query):
        for key, val in query.items():
            if isinstance(val, dict) and "$in" in val:
                doc_val = doc.get(key)
                if isinstance(doc_val, list):
                    if not any(v in val["$in"] for v in doc_val):
                        return False
                elif doc_val not in val["$in"]:
                    return False
            elif doc.get(key) != val:
                return False
        return True


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]]):
        self._colls = {name: FakeCollection(docs) for name, docs in collections.items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def _ctx():
    return {"user_id": "u1", "brand_id": "b1"}


def test_completed_scan_reports_the_insights_and_developments_it_produced():
    db = FakeDb({
        "mi_scans": [{"id": "s1", "brand_id": "b1", "topic_id": "t1", "status": "completed"}],
        "mi_evidence": [
            {"id": "e1", "collection_run_id": "s1"},
            {"id": "e2", "collection_run_id": "s1"},
            {"id": "e3", "collection_run_id": "OTHER_SCAN"},
        ],
        "mi_insights": [
            {"id": "i1", "topic_id": "t1", "status": "active", "evidence_ids": ["e1"]},
            {"id": "i2", "topic_id": "t1", "status": "active", "evidence_ids": ["e3"]},  # not from this scan
        ],
        "mi_developments": [
            {"id": "d1", "topic_id": "t1", "evidence_id": "e2"},
        ],
    })
    res = _run(get_scan("s1", ctx=_ctx(), db=db))
    assert res["responseData"]["new_insight_count"] == 1
    assert res["responseData"]["new_development_count"] == 1


def test_scan_with_no_evidence_reports_zero_not_an_error():
    db = FakeDb({
        "mi_scans": [{"id": "s1", "brand_id": "b1", "topic_id": "t1", "status": "completed"}],
    })
    res = _run(get_scan("s1", ctx=_ctx(), db=db))
    assert res["responseData"]["new_insight_count"] == 0
    assert res["responseData"]["new_development_count"] == 0


def test_a_still_running_scan_has_no_result_counts_yet():
    db = FakeDb({
        "mi_scans": [{"id": "s1", "brand_id": "b1", "topic_id": "t1", "status": "collecting", "started_at": None}],
    })
    res = _run(get_scan("s1", ctx=_ctx(), db=db))
    assert "new_insight_count" not in res["responseData"]


def test_404s_for_a_scan_that_does_not_exist():
    db = FakeDb({})
    with pytest.raises(HTTPException) as exc_info:
        _run(get_scan("missing", ctx=_ctx(), db=db))
    assert exc_info.value.status_code == 404


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
