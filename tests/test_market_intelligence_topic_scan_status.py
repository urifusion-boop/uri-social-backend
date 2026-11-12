"""
Uri Market Intelligence — GET /topics attaches each topic's latest scan
(router.py's list_topics). Confirmed live UX bug: "Run scan" had no
server-visible state at all — a new tab, a closed/reopened browser, or
just outlasting the frontend's own give-up timeout showed nothing, no
matter what the scan was actually doing. This is what makes real server
state ("still collecting," "finished," "failed") visible on any page
load, not just the one browser tab that happened to start the scan.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.agents.market_intelligence.models import TopicCreateRequest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    def _matches(self, doc, query):
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

    async def count_documents(self, query):
        return len([d for d in self.docs if self._matches(d, query)])

    class _Cursor:
        def __init__(self, docs):
            self._docs = docs

        def sort(self, *_args, **_kwargs):
            return self

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for d in list(self._docs):
                yield d

    def find(self, query=None, projection=None):
        query = query or {}
        return self._Cursor([dict(d) for d in self.docs if self._matches(d, query)])

    async def find_one(self, query, sort=None):
        matches = [d for d in self.docs if self._matches(d, query)]
        if sort:
            field, direction = sort[0]
            matches.sort(key=lambda d: d.get(field) or datetime.min, reverse=(direction == -1))
        return dict(matches[0]) if matches else None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeDb:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def _ctx():
    return {"user_id": "u1", "brand_id": "b1"}


def test_topic_with_no_scans_yet_has_a_null_latest_scan():
    from app.agents.market_intelligence.router import create_topic, list_topics

    db = FakeDb()
    from unittest.mock import patch
    with patch("app.agents.market_intelligence.router.track_event"):
        _run(create_topic(TopicCreateRequest(question="q"), ctx=_ctx(), db=db))

    res = _run(list_topics(ctx=_ctx(), db=db))
    assert res["responseData"][0]["latest_scan"] is None


def test_topic_shows_its_most_recent_scan():
    from app.agents.market_intelligence.router import list_topics

    db = FakeDb()
    db["mi_topics"].docs.append({"id": "t1", "brand_id": "b1", "question": "q", "created_at": datetime(2026, 1, 1)})
    db["mi_scans"].docs.append({
        "id": "s1", "topic_id": "t1", "brand_id": "b1", "status": "completed",
        "started_at": datetime(2026, 1, 1, 10, 0),
    })
    db["mi_scans"].docs.append({
        "id": "s2", "topic_id": "t1", "brand_id": "b1", "status": "collecting",
        "started_at": datetime(2026, 1, 1, 11, 0),
    })

    res = _run(list_topics(ctx=_ctx(), db=db))
    latest = res["responseData"][0]["latest_scan"]
    assert latest["id"] == "s2"
    assert latest["status"] == "collecting"


def test_each_topics_scan_history_is_kept_separate():
    from app.agents.market_intelligence.router import list_topics

    db = FakeDb()
    db["mi_topics"].docs.append({"id": "t1", "brand_id": "b1", "question": "q1", "created_at": datetime(2026, 1, 2)})
    db["mi_topics"].docs.append({"id": "t2", "brand_id": "b1", "question": "q2", "created_at": datetime(2026, 1, 1)})
    db["mi_scans"].docs.append({"id": "s1", "topic_id": "t1", "brand_id": "b1", "status": "completed", "started_at": datetime(2026, 1, 1)})
    db["mi_scans"].docs.append({"id": "s2", "topic_id": "t2", "brand_id": "b1", "status": "failed", "started_at": datetime(2026, 1, 1)})

    res = _run(list_topics(ctx=_ctx(), db=db))
    by_id = {t["id"]: t for t in res["responseData"]}
    assert by_id["t1"]["latest_scan"]["id"] == "s1"
    assert by_id["t2"]["latest_scan"]["id"] == "s2"


def test_latest_scan_carries_what_it_actually_found():
    """The same result-summary logic GET /scans/{id} uses also applies
    here, so the payoff is visible on a plain page load, not just while
    a client happens to be actively polling that one scan."""
    from app.agents.market_intelligence.router import list_topics

    db = FakeDb()
    db["mi_topics"].docs.append({"id": "t1", "brand_id": "b1", "question": "q", "created_at": datetime(2026, 1, 1)})
    db["mi_scans"].docs.append({"id": "s1", "topic_id": "t1", "brand_id": "b1", "status": "completed", "started_at": datetime(2026, 1, 1)})
    db["mi_evidence"].docs.append({"id": "e1", "collection_run_id": "s1"})
    db["mi_insights"].docs.append({"id": "i1", "topic_id": "t1", "status": "active", "evidence_ids": ["e1"]})

    res = _run(list_topics(ctx=_ctx(), db=db))
    latest = res["responseData"][0]["latest_scan"]
    assert latest["new_insight_count"] == 1
    assert latest["new_development_count"] == 0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
