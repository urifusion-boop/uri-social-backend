"""
Uri Market Intelligence — classification cache tests (PRD §23).

The critical thing under test isn't just "does it cache" — it's that the
cache is scoped per-brand, never by text alone. classify.py's own prompt is
explicitly context-dependent (its "Apple the fruit vs. the device"
resolution only works because business context is fed in with the text),
so serving a brand-B classification to brand-A because the raw text happens
to match would be a real correctness bug, not just a missed cache hit.
"""
import asyncio
from datetime import datetime

import pytest

from app.agents.market_intelligence.classification_cache import (
    get_cached_classification,
    store_classification_cache,
)
from app.agents.market_intelligence.models import Classification, EvidenceType


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, query):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)


class FakeDb:
    def __init__(self):
        self._coll = FakeCollection()

    def __getitem__(self, name):
        return self._coll


def _classification(evidence_id="e1") -> Classification:
    return Classification(
        evidence_id=evidence_id, primary_type=EvidenceType.CUSTOMER_CONCERN,
        evidence_span="delivery is slow", reasoning="genuine complaint",
    )


def test_returns_none_on_cache_miss():
    db = FakeDb()
    result = _run(get_cached_classification(db, "some text", "brand1"))
    assert result is None


def test_returns_cached_result_on_hit():
    db = FakeDb()
    _run(store_classification_cache(db, "delivery is slow", "brand1", _classification()))
    result = _run(get_cached_classification(db, "delivery is slow", "brand1"))
    assert result is not None
    assert result.primary_type == EvidenceType.CUSTOMER_CONCERN
    assert result.reasoning == "genuine complaint"


def test_cache_is_case_and_whitespace_insensitive():
    db = FakeDb()
    _run(store_classification_cache(db, "Delivery Is Slow", "brand1", _classification()))
    result = _run(get_cached_classification(db, "  delivery is slow  ", "brand1"))
    assert result is not None


def test_cache_never_crosses_brand_boundary():
    # The whole point: identical text classified differently (or just
    # differently CONTEXTED) for a different brand must never be served
    # cross-tenant, since classification is context-dependent.
    db = FakeDb()
    _run(store_classification_cache(db, "I love my apple", "fruit_seller_brand", _classification()))
    result = _run(get_cached_classification(db, "I love my apple", "phone_seller_brand"))
    assert result is None


def test_cached_result_gets_a_fresh_evidence_id_and_timestamp():
    db = FakeDb()
    _run(store_classification_cache(db, "text", "brand1", _classification(evidence_id="original")))
    result = _run(get_cached_classification(db, "text", "brand1"))
    assert result.evidence_id == ""  # caller stamps the real one, same as a fresh classification
    assert isinstance(result.classified_at, datetime)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
