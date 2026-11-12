"""
Uri Market Intelligence — topic creation dedup + the new DELETE /topics
endpoint (router.py). Confirmed live: this account had the exact same
question submitted twice, 11 minutes apart, as two separate topics with
no dedup check anywhere, and a near-duplicate pair differing only by a
trailing "?" — and no way to remove either one once created.
"""
import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.agents.market_intelligence.models import TopicCreateRequest


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

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if self._matches(d, query):
                del self.docs[i]
                return

    async def delete_many(self, query):
        class _Result:
            def __init__(self, n):
                self.deleted_count = n
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._matches(d, query)]
        return _Result(before - len(self.docs))


class FakeDb:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def _ctx():
    return {"user_id": "u1", "brand_id": "b1"}


class TestCreateTopicDedup:
    def test_creates_a_brand_new_topic_when_nothing_matches(self):
        from app.agents.market_intelligence.router import create_topic

        db = FakeDb()
        with patch("app.agents.market_intelligence.router.track_event"):
            res = _run(create_topic(TopicCreateRequest(question="What stops Lagos customers from buying?"), ctx=_ctx(), db=db))
        assert res["responseData"]["question"] == "What stops Lagos customers from buying?"
        assert len(db["mi_topics"].docs) == 1

    def test_exact_resubmission_returns_the_existing_topic_not_a_duplicate(self):
        from app.agents.market_intelligence.router import create_topic

        db = FakeDb()
        with patch("app.agents.market_intelligence.router.track_event"):
            first = _run(create_topic(TopicCreateRequest(question="What stops Lagos customers from buying?"), ctx=_ctx(), db=db))
            second = _run(create_topic(TopicCreateRequest(question="What stops Lagos customers from buying?"), ctx=_ctx(), db=db))

        assert len(db["mi_topics"].docs) == 1
        assert second["responseData"]["id"] == first["responseData"]["id"]

    def test_trailing_punctuation_and_case_differences_are_treated_as_the_same_question(self):
        """The real live bug: "...online?" and "...online" sat as two
        separate topics with no dedup check at all."""
        from app.agents.market_intelligence.router import create_topic

        db = FakeDb()
        with patch("app.agents.market_intelligence.router.track_event"):
            first = _run(create_topic(
                TopicCreateRequest(question="What stops Lagos customers from ordering ready-to-wear clothes online?"),
                ctx=_ctx(), db=db,
            ))
            second = _run(create_topic(
                TopicCreateRequest(question="what stops lagos customers from ordering ready-to-wear clothes online"),
                ctx=_ctx(), db=db,
            ))

        assert len(db["mi_topics"].docs) == 1
        assert second["responseData"]["id"] == first["responseData"]["id"]

    def test_a_genuinely_different_question_is_not_deduped(self):
        from app.agents.market_intelligence.router import create_topic

        db = FakeDb()
        with patch("app.agents.market_intelligence.router.track_event"):
            _run(create_topic(TopicCreateRequest(question="What stops Lagos customers from buying?"), ctx=_ctx(), db=db))
            _run(create_topic(TopicCreateRequest(question="What do Abuja customers ask about shipping?"), ctx=_ctx(), db=db))
        assert len(db["mi_topics"].docs) == 2

    def test_a_different_brands_identical_question_is_not_deduped(self):
        from app.agents.market_intelligence.router import create_topic

        db = FakeDb()
        with patch("app.agents.market_intelligence.router.track_event"):
            _run(create_topic(TopicCreateRequest(question="Same question"), ctx={"user_id": "u1", "brand_id": "b1"}, db=db))
            _run(create_topic(TopicCreateRequest(question="Same question"), ctx={"user_id": "u2", "brand_id": "b2"}, db=db))
        assert len(db["mi_topics"].docs) == 2

    def test_an_inactive_duplicate_does_not_block_a_fresh_topic(self):
        from app.agents.market_intelligence.router import create_topic

        db = FakeDb()
        db["mi_topics"].docs.append({"id": "old", "brand_id": "b1", "question": "Same question", "active": False})
        with patch("app.agents.market_intelligence.router.track_event"):
            res = _run(create_topic(TopicCreateRequest(question="Same question"), ctx=_ctx(), db=db))
        assert res["responseData"]["id"] != "old"
        assert len(db["mi_topics"].docs) == 2


class TestDeleteTopicEndpoint:
    def test_deletes_an_owned_topic(self):
        from app.agents.market_intelligence.router import delete_topic

        db = FakeDb()
        db["mi_topics"].docs.append({"id": "t1", "brand_id": "b1", "question": "q"})
        res = _run(delete_topic("t1", ctx=_ctx(), db=db))
        assert res["responseData"]["topic_id"] == "t1"
        assert db["mi_topics"].docs == []

    def test_404s_for_a_topic_that_does_not_exist(self):
        from app.agents.market_intelligence.router import delete_topic

        db = FakeDb()
        with pytest.raises(HTTPException) as exc_info:
            _run(delete_topic("missing", ctx=_ctx(), db=db))
        assert exc_info.value.status_code == 404

    def test_404s_rather_than_leaking_another_brands_topic(self):
        from app.agents.market_intelligence.router import delete_topic

        db = FakeDb()
        db["mi_topics"].docs.append({"id": "t1", "brand_id": "OTHER_BRAND", "question": "q"})
        with pytest.raises(HTTPException) as exc_info:
            _run(delete_topic("t1", ctx=_ctx(), db=db))
        assert exc_info.value.status_code == 404
        assert len(db["mi_topics"].docs) == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
