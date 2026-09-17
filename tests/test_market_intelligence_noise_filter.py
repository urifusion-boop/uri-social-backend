"""
Uri Market Intelligence — noise filter unit tests + scan_runner wiring test.

Verifies the deterministic pre-classification filter (adapted from uri-insights'
SignalRefineryService BLOCKLIST/BOT_PATTERNS) both flags the fixture's known
spam post directly, and is actually invoked by execute_scan() BEFORE the LLM
classification call — the whole point being that flagged evidence never incurs
an LLM call (PRD §23 cost control).
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.market_intelligence.models import (
    Classification,
    EvidenceType,
    Geography,
    RawEvidence,
    Topic,
    SourceConfig,
)
from app.agents.market_intelligence.noise_filter import deterministic_noise_reason


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _evidence(source_id: str, text: str) -> RawEvidence:
    return RawEvidence(
        provider="mock",
        platform="mock",
        source_id=source_id,
        text=text,
        collected_at=datetime.now(timezone.utc),
        geography=Geography(),
    )


# ── deterministic_noise_reason unit tests ───────────────────────────────────

def test_flags_bot_pattern_from_mock_fixture():
    ev = _evidence("n1", "FOLLOW FOR FOLLOW check my page for the best deals!!! #promo #follow")
    reason = deterministic_noise_reason(ev)
    assert reason is not None
    assert "follow for follow" in reason


def test_flags_blocklist_term():
    ev = _evidence("s1", "Best forex signals guaranteed, join now and start earning today")
    reason = deterministic_noise_reason(ev)
    assert reason is not None
    assert "forex" in reason


def test_does_not_flag_genuine_customer_text():
    ev = _evidence("c1", "Delivery to Lekki has been taking over a week, is anyone else experiencing this?")
    assert deterministic_noise_reason(ev) is None


def test_does_not_flag_topically_irrelevant_but_non_spam_text():
    # n2 from the mock fixture — irrelevant content, but not spam/promo boilerplate.
    # That distinction is the LLM classifier's job (context resolution), not this filter's.
    ev = _evidence("n2", "Apple just announced a new iPhone event next week.")
    assert deterministic_noise_reason(ev) is None


# ── FakeDB harness for the scan_runner wiring test ──────────────────────────

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

    def find(self, query, projection=None):
        return FakeCursor([])  # no pre-existing evidence — nothing deduped away

    async def insert_many(self, docs):
        self.inserted.extend(docs)

    async def insert_one(self, doc):
        self.inserted.append(doc)

    async def update_one(self, query, update, upsert=False):
        pass


class FakeDb:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def test_execute_scan_short_circuits_noise_before_llm_call():
    """The mock adapter's n1 post ('FOLLOW FOR FOLLOW...') must be classified
    as NOISE via the deterministic filter WITHOUT calling classify_evidence —
    proven here by asserting the mocked classify_evidence is never invoked
    with n1's evidence id, only with the non-spam evidence ids."""
    from app.agents.market_intelligence import scan_runner

    db = FakeDb()
    topic = Topic(
        id="t1",
        brand_id="b1",
        user_id="u1",
        question="What are customers saying about delivery?",
        keywords=["delivery"],
        sources=[SourceConfig(provider="mock", platform="mock")],
        requested_days=7,
    )

    seen_source_ids: list[str] = []

    async def fake_classify(evidence, business_context):
        seen_source_ids.append(evidence.source_id)
        return Classification(
            evidence_id=evidence.id,
            primary_type=EvidenceType.GENERAL_DISCUSSION,
            evidence_span=evidence.text[:50],
            reasoning="stub",
        )

    async def fake_business_context(db, user_id, brand_id):
        return {"brand_name": "Test Brand", "industry": "retail", "key_products_services": []}

    async def fake_cluster_evidence(evidence, **kwargs):
        return []

    with patch.object(scan_runner, "classify_evidence", side_effect=fake_classify), \
         patch.object(scan_runner, "_fetch_business_context", side_effect=fake_business_context), \
         patch.object(scan_runner, "cluster_evidence", side_effect=fake_cluster_evidence):
        _run(scan_runner.execute_scan(topic, "run1", db))

    # n1 (the bot-pattern post) must never reach the LLM classifier.
    assert "n1" not in seen_source_ids
    # A genuine post must still go through it.
    assert "c1" in seen_source_ids

    classifications_coll = db["mi_classifications"]
    noise_records = [
        c for c in classifications_coll.inserted
        if c["model_name"] == "deterministic-filter"
    ]
    assert len(noise_records) == 1
    assert noise_records[0]["primary_type"] == EvidenceType.NOISE.value
    assert "follow for follow" in noise_records[0]["reasoning"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
