"""
Uri Market Intelligence — topic setup UX tests (PRD §9): source listing,
keyword suggestion fallback, and the new advanced-input fields actually
landing on the created Topic.
"""
import asyncio
from unittest.mock import patch

import pytest

from app.agents.market_intelligence.models import NotificationSensitivity, TopicCreateRequest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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


def test_list_sources_returns_registered_adapter_capabilities():
    from app.agents.market_intelligence.router import list_sources

    res = _run(list_sources(ctx={"user_id": "u1", "brand_id": "b1"}))
    sources = res["responseData"]
    assert len(sources) >= 1
    assert any(s["provider"] == "mock" for s in sources)


def test_suggest_keywords_falls_back_when_llm_fails():
    from app.agents.market_intelligence import router
    from app.agents.market_intelligence.models import KeywordSuggestionRequest

    async def failing_suggest(question):
        return None

    with patch.object(router, "suggest_keywords", side_effect=failing_suggest):
        res = _run(router.suggest_topic_keywords(
            KeywordSuggestionRequest(question="What stops Lagos customers from ordering online delivery"),
            ctx={"user_id": "u1", "brand_id": "b1"},
        ))

    suggestion = res["responseData"]
    assert len(suggestion["keywords"]) > 0
    assert suggestion["excluded_keywords"] == []


def test_create_topic_persists_advanced_fields():
    from app.agents.market_intelligence.router import create_topic

    db = FakeDb()
    ctx = {"user_id": "u1", "brand_id": "b1"}
    body = TopicCreateRequest(
        question="q", competitors=["RivalCo"], languages=["en", "pcm"],
        notification_sensitivity=NotificationSensitivity.HIGH,
    )

    with patch("app.agents.market_intelligence.router.track_event"):
        res = _run(create_topic(body, ctx=ctx, db=db))

    topic = res["responseData"]
    assert topic["competitors"] == ["RivalCo"]
    assert topic["languages"] == ["en", "pcm"]
    assert topic["notification_sensitivity"] == "high"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
