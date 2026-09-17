"""
Uri Market Intelligence — coverage-preview / truthful-coverage tests (PRD §9,
P0-02: "Unsupported 90-day request shows available scope before start" /
"A shorter available period must never silently replace the requested one").

Covers: the pure clamp function, preview_topic_coverage() against a topic
whose requested_days exceeds the mock adapter's verified lookback, and that
execute_scan() records the SAME clamp as a visible gap rather than quietly
using fewer days.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.agents.market_intelligence.adapters.base import AdapterCapabilities
from app.agents.market_intelligence.adapters.mock import MockSourceAdapter
from app.agents.market_intelligence.models import (
    Classification,
    EvidenceType,
    SourceConfig,
    Topic,
)
from app.agents.market_intelligence.scan_runner import clamp_requested_days, preview_topic_coverage


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


MOCK_CAPABILITY = MockSourceAdapter().capabilities()  # verified_lookback_days=90


# ── clamp_requested_days ─────────────────────────────────────────────────────

def test_clamp_returns_requested_unchanged_when_within_lookback():
    days, note = clamp_requested_days(30, MOCK_CAPABILITY)
    assert days == 30
    assert note is None


def test_clamp_caps_and_explains_when_exceeding_lookback():
    days, note = clamp_requested_days(120, MOCK_CAPABILITY)
    assert days == 90  # mock's verified_lookback_days
    assert note is not None
    assert "120" in note and "90" in note


def test_clamp_never_silently_returns_requested_value_when_capped():
    # The whole point of P0-02: a caller that only looks at the returned days
    # (not the note) would already see the true accessible number, never the
    # originally requested one.
    days, note = clamp_requested_days(365, AdapterCapabilities(
        provider="tiny", platform="tiny", verified_lookback_days=7,
        supports_date_filters=True, supports_keyword_search=True,
        accessible_languages=["en"], refresh_cadence_hours=1,
    ))
    assert days == 7
    assert note is not None


# ── preview_topic_coverage ───────────────────────────────────────────────────

def _topic(requested_days: int, sources: list[str]) -> Topic:
    return Topic(
        id="t1", brand_id="b1", user_id="u1",
        question="What are customers saying?",
        keywords=["delivery"],
        sources=[SourceConfig(provider=p, platform=p) for p in sources],
        requested_days=requested_days,
    )


def test_preview_flags_capped_source_before_any_scan_runs():
    topic = _topic(requested_days=120, sources=["mock"])
    previews = _run(preview_topic_coverage(topic))
    assert len(previews) == 1
    assert previews[0].capped is True
    assert previews[0].accessible_days == 90
    assert previews[0].requested_days == 120  # original request preserved, not overwritten


def test_preview_does_not_flag_source_within_lookback():
    topic = _topic(requested_days=30, sources=["mock"])
    previews = _run(preview_topic_coverage(topic))
    assert previews[0].capped is False
    assert previews[0].accessible_days == 30


def test_preview_flags_unregistered_source_as_fully_capped():
    topic = _topic(requested_days=30, sources=["not_a_real_provider"])
    previews = _run(preview_topic_coverage(topic))
    assert previews[0].capped is True
    assert previews[0].accessible_days == 0
    assert "no registered adapter" in previews[0].note


# ── execute_scan wiring: the clamp must show up as a real gap, not vanish ───

class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for d in self._docs:
            yield d


class FakeCollection:
    def __init__(self):
        self.inserted: list[dict] = []
        self.updates: list[dict] = []

    def find(self, query, projection=None):
        return FakeCursor([])

    async def insert_many(self, docs):
        self.inserted.extend(docs)

    async def insert_one(self, doc):
        self.inserted.append(doc)

    async def update_one(self, query, update, upsert=False):
        self.updates.append(update.get("$set", {}))


class FakeDb:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def test_execute_scan_records_coverage_clamp_as_a_visible_gap():
    from app.agents.market_intelligence import scan_runner

    db = FakeDb()
    topic = _topic(requested_days=200, sources=["mock"])  # far beyond mock's 90-day lookback

    async def fake_classify(evidence, business_context):
        return Classification(
            evidence_id=evidence.id, primary_type=EvidenceType.NOISE,
            evidence_span=evidence.text[:50], reasoning="stub",
        )

    async def fake_business_context(db, user_id, brand_id):
        return {"brand_name": "Test", "industry": "retail", "key_products_services": []}

    async def fake_cluster_evidence(evidence, **kwargs):
        return []

    with patch.object(scan_runner, "classify_evidence", side_effect=fake_classify), \
         patch.object(scan_runner, "_fetch_business_context", side_effect=fake_business_context), \
         patch.object(scan_runner, "cluster_evidence", side_effect=fake_cluster_evidence):
        _run(scan_runner.execute_scan(topic, "run1", db))

    final_update = db["mi_scans"].updates[-1]
    assert any("200" in g and "90" in g for g in final_update["gaps"]), final_update["gaps"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
