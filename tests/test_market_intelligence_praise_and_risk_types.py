"""
Uri Market Intelligence — product_praise and reputation_risk wiring tests.

Both were previously dead ends: classified and stored in mi_classifications
but never clustered or surfaced as an insight. general_discussion is
deliberately NOT included here — PRD §10 routes it to "Supporting evidence,"
meaning it enriches other insights rather than becoming a standalone card,
which is the PRD's own intent, not something to fix.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.agents.market_intelligence.adapters.base import AdapterCapabilities, CollectionPage
from app.agents.market_intelligence.models import (
    Classification,
    Cluster,
    EvidenceType,
    InsightVersion,
    RawEvidence,
    ScanStatus,
    SourceConfig,
    Topic,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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
        self.docs: list[dict] = []

    def find(self, query=None, projection=None):
        return FakeCursor([dict(d) for d in self.docs])

    async def find_one(self, query):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def insert_many(self, docs):
        self.docs.extend(dict(d) for d in docs)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)

    async def replace_one(self, query, replacement, upsert=False):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                self.docs[i] = dict(replacement)
                return
        if upsert:
            self.docs.append(dict(replacement))


class FakeDb:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


class _SingleEvidenceAdapter:
    def __init__(self, source_id: str, text: str, author: str):
        self._evidence = RawEvidence(
            provider="fake", platform="fake", source_id=source_id,
            url=f"https://example.com/{source_id}", text=text,
            published_at=datetime.utcnow(), collected_at=datetime.now(timezone.utc), author_handle=author,
        )

    def capabilities(self):
        return AdapterCapabilities(
            provider="fake", platform="fake", verified_lookback_days=30,
            supports_date_filters=True, supports_keyword_search=True,
            accessible_languages=["en"], refresh_cadence_hours=1,
        )

    async def estimate_cost(self, keywords, days):
        return 0.0

    async def start_collection(self, keywords, excluded_keywords, since, until):
        return "run1"

    async def fetch_page(self, run_id, cursor=None):
        return CollectionPage(evidence=[self._evidence], has_more=False)

    async def get_status(self, run_id):
        return ScanStatus.COMPLETED

    async def cancel_if_supported(self, run_id):
        return False


def _topic() -> Topic:
    return Topic(
        id="t1", brand_id="b1", user_id="u1", question="q", keywords=["x"],
        sources=[SourceConfig(provider="fake", platform="fake")], requested_days=30,
    )


async def _fake_business_context(db, user_id, brand_id):
    return {"brand_name": "T", "industry": "x", "key_products_services": []}


async def _fake_compose(cluster, member_evidence, member_classifications, confidence, relevance, urgency):
    return InsightVersion(
        id=str(uuid.uuid4()), revision=1, brand_id=cluster.brand_id, topic_id=cluster.topic_id,
        cluster_id=cluster.id, type=cluster.primary_type, headline="h", observed_change="oc",
        business_implication="bi", suggested_next_step="sns", evidence_ids=cluster.evidence_ids,
        confidence=confidence, relevance=relevance, urgency=urgency,
        first_seen=cluster.first_seen, last_updated=datetime.utcnow(),
    )


def _run_scan_for_type(evidence_type: EvidenceType):
    from app.agents.market_intelligence import scan_runner

    db = FakeDb()
    topic = _topic()

    async def fake_classify(evidence, business_context):
        return Classification(
            evidence_id=evidence.id, primary_type=evidence_type,
            evidence_span=evidence.text[:50], reasoning="stub",
        )

    async def fake_cluster_evidence(evidence_list, **kwargs):
        e = evidence_list[0]
        return [Cluster(
            id=str(uuid.uuid4()), brand_id="b1", topic_id="t1", primary_type=evidence_type,
            theme="theme", evidence_ids=[e.id], independent_account_count=5, original_thread_count=3,
            first_seen=e.published_at, last_updated=datetime.utcnow(), embedding_centroid=[1.0],
        )]

    with patch.dict(scan_runner.ADAPTER_REGISTRY, {"fake": _SingleEvidenceAdapter("e1", "some text", "@a1")}), \
         patch.object(scan_runner, "classify_evidence", side_effect=fake_classify), \
         patch.object(scan_runner, "_fetch_business_context", side_effect=_fake_business_context), \
         patch.object(scan_runner, "cluster_evidence", side_effect=fake_cluster_evidence), \
         patch.object(scan_runner, "compose_insight", side_effect=_fake_compose), \
         patch.object(scan_runner, "is_concern_eligible", return_value=(True, "eligible (test override)")):
        _run(scan_runner.execute_scan(topic, "run1", db))

    return db


def test_product_praise_produces_a_surfaced_insight():
    db = _run_scan_for_type(EvidenceType.PRODUCT_PRAISE)
    insights = db["mi_insights"].docs
    assert len(insights) == 1
    assert insights[0]["type"] == EvidenceType.PRODUCT_PRAISE.value


def test_reputation_risk_produces_an_insight_with_unverified_coverage_note():
    db = _run_scan_for_type(EvidenceType.REPUTATION_RISK)
    insights = db["mi_insights"].docs
    assert len(insights) == 1
    assert insights[0]["type"] == EvidenceType.REPUTATION_RISK.value
    assert "unverified" in insights[0]["coverage_note"].lower()


def test_reputation_risk_never_creates_an_outbox_entry():
    db = _run_scan_for_type(EvidenceType.REPUTATION_RISK)
    assert db["mi_outbox"].docs == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
