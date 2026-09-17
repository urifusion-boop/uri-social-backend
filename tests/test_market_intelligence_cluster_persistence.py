"""
Uri Market Intelligence — cluster/insight persistence across scans (PRD
§11/§19 "keep a stable cluster ID through ordinary updates", §13 "keep
every published insight revision immutable... corrections create a new
revision").

Real embedding calls aren't available in this environment (no working
OpenAI key), so cluster_evidence's actual grouping is stubbed here with a
controlled embedding_centroid — what's under test is the RECONCILIATION
logic (match_existing_cluster, _merge_cluster, _apply_revision) and its
wiring into execute_scan, not the embedding call itself (already covered,
separately, by clustering.py's own documented empirical proof and by the
noise-filter/coverage/development suites' use of the real pipeline).
"""
import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.agents.market_intelligence.adapters.base import AdapterCapabilities, CollectionPage
from app.agents.market_intelligence.clustering import _centroid, match_existing_cluster
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


# ── _centroid ────────────────────────────────────────────────────────────────

def test_centroid_averages_embeddings():
    assert _centroid([[1.0, 1.0], [3.0, 3.0]]) == [2.0, 2.0]


def test_centroid_none_for_empty_list():
    assert _centroid([]) is None


# ── match_existing_cluster ───────────────────────────────────────────────────

def _cluster(**overrides) -> Cluster:
    base = dict(
        id=str(uuid.uuid4()), brand_id="b1", topic_id="t1", primary_type=EvidenceType.CUSTOMER_CONCERN,
        theme="theme", evidence_ids=["e1"], independent_account_count=1, original_thread_count=1,
        first_seen=datetime.utcnow(), last_updated=datetime.utcnow(),
    )
    base.update(overrides)
    return Cluster(**base)


def test_matches_cluster_with_near_identical_centroid():
    existing = _cluster(embedding_centroid=[1.0, 0.0])
    candidate = _cluster(embedding_centroid=[0.99, 0.01])
    assert match_existing_cluster(candidate, [existing]) is existing


def test_does_not_match_dissimilar_centroid():
    existing = _cluster(embedding_centroid=[1.0, 0.0])
    candidate = _cluster(embedding_centroid=[0.0, 1.0])
    assert match_existing_cluster(candidate, [existing]) is None


def test_does_not_match_across_different_topics():
    existing = _cluster(topic_id="OTHER_TOPIC", embedding_centroid=[1.0, 0.0])
    candidate = _cluster(topic_id="t1", embedding_centroid=[1.0, 0.0])
    assert match_existing_cluster(candidate, [existing]) is None


def test_does_not_match_when_candidate_has_no_centroid():
    existing = _cluster(embedding_centroid=[1.0, 0.0])
    candidate = _cluster(embedding_centroid=None)
    assert match_existing_cluster(candidate, [existing]) is None


# ── execute_scan integration: second scan merges + revises ──────────────────

class GenericFakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for d in self._docs:
            yield d


class GenericFakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    def _matches(self, doc, query):
        for key, cond in query.items():
            val = doc.get(key)
            if isinstance(cond, dict) and "$in" in cond:
                if val not in cond["$in"]:
                    return False
            elif isinstance(val, list):
                if cond not in val:
                    return False
            elif val != cond:
                return False
        return True

    def find(self, query=None, projection=None):
        query = query or {}
        return GenericFakeCursor([dict(d) for d in self.docs if self._matches(d, query)])

    async def find_one(self, query=None, sort=None):
        query = query or {}
        matches = [d for d in self.docs if self._matches(d, query)]
        if sort:
            field, direction = sort[0]
            matches.sort(key=lambda d: d.get(field) or datetime.min, reverse=(direction == -1))
        return dict(matches[0]) if matches else None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def insert_many(self, docs):
        self.docs.extend(dict(d) for d in docs)

    def _apply(self, doc, update):
        for k, v in update.get("$inc", {}).items():
            doc[k] = doc.get(k, 0) + v
        doc.update(update.get("$set", {}))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if self._matches(d, query):
                self._apply(d, update)
                return
        if upsert:
            new_doc = dict(query)
            self._apply(new_doc, update)
            self.docs.append(new_doc)

    async def replace_one(self, query, replacement, upsert=False):
        for i, d in enumerate(self.docs):
            if self._matches(d, query):
                self.docs[i] = dict(replacement)
                return
        if upsert:
            self.docs.append(dict(replacement))


class GenericFakeDb:
    def __init__(self):
        self._colls: dict[str, GenericFakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, GenericFakeCollection())


class _SingleEvidenceAdapter:
    """Returns exactly one fixed RawEvidence per scan — enough to drive
    execute_scan's full pipeline with a controlled, predictable input."""
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


async def _fake_classify(evidence, business_context):
    return Classification(
        evidence_id=evidence.id, primary_type=EvidenceType.CUSTOMER_CONCERN,
        evidence_span=evidence.text[:50], reasoning="stub",
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


def _make_cluster_evidence_stub(shared_centroid):
    async def fake_cluster_evidence(evidence_list, **kwargs):
        e = evidence_list[0]
        return [Cluster(
            id=str(uuid.uuid4()), brand_id="b1", topic_id="t1", primary_type=EvidenceType.CUSTOMER_CONCERN,
            theme="lekki delivery", evidence_ids=[e.id], independent_account_count=1, original_thread_count=1,
            first_seen=e.published_at, last_updated=datetime.utcnow(), embedding_centroid=shared_centroid,
        )]
    return fake_cluster_evidence


def test_second_scan_merges_into_existing_cluster_and_revises_insight():
    from app.agents.market_intelligence import scan_runner

    db = GenericFakeDb()
    topic = _topic()
    shared_centroid = [1.0, 0.0, 0.0]

    # ── Scan 1 ──
    with patch.dict(scan_runner.ADAPTER_REGISTRY, {"fake": _SingleEvidenceAdapter("e1", "Lekki delivery is slow", "@a1")}), \
         patch.object(scan_runner, "classify_evidence", side_effect=_fake_classify), \
         patch.object(scan_runner, "_fetch_business_context", side_effect=_fake_business_context), \
         patch.object(scan_runner, "cluster_evidence", side_effect=_make_cluster_evidence_stub(shared_centroid)), \
         patch.object(scan_runner, "compose_insight", side_effect=_fake_compose), \
         patch.object(scan_runner, "is_concern_eligible", return_value=(True, "eligible (test override)")):
        _run(scan_runner.execute_scan(topic, "run1", db))

    clusters_after_1 = db["mi_clusters"].docs
    assert len(clusters_after_1) == 1
    cluster_id = clusters_after_1[0]["id"]

    active_after_1 = [d for d in db["mi_insights"].docs if d["status"] == "active"]
    assert len(active_after_1) == 1
    assert active_after_1[0]["revision"] == 1
    first_seen_1 = active_after_1[0]["first_seen"]

    # ── Scan 2: different evidence, same conversation (same centroid) ──
    with patch.dict(scan_runner.ADAPTER_REGISTRY, {"fake": _SingleEvidenceAdapter("e2", "Still waiting on Lekki delivery, day 9", "@a2")}), \
         patch.object(scan_runner, "classify_evidence", side_effect=_fake_classify), \
         patch.object(scan_runner, "_fetch_business_context", side_effect=_fake_business_context), \
         patch.object(scan_runner, "cluster_evidence", side_effect=_make_cluster_evidence_stub(shared_centroid)), \
         patch.object(scan_runner, "compose_insight", side_effect=_fake_compose), \
         patch.object(scan_runner, "is_concern_eligible", return_value=(True, "eligible (test override)")):
        _run(scan_runner.execute_scan(topic, "run2", db))

    clusters_after_2 = db["mi_clusters"].docs
    # Still exactly one cluster — merged, not duplicated — and it's the SAME id.
    assert len(clusters_after_2) == 1
    assert clusters_after_2[0]["id"] == cluster_id
    assert len(clusters_after_2[0]["evidence_ids"]) == 2
    assert clusters_after_2[0]["independent_account_count"] == 2  # @a1 + @a2

    all_insights = db["mi_insights"].docs
    active_after_2 = [d for d in all_insights if d["status"] == "active"]
    superseded = [d for d in all_insights if d["status"] == "superseded"]
    assert len(active_after_2) == 1
    assert len(superseded) == 1
    assert superseded[0]["id"] == active_after_1[0]["id"]  # the original revision, untouched otherwise
    assert active_after_2[0]["revision"] == 2
    assert active_after_2[0]["cluster_id"] == cluster_id
    assert active_after_2[0]["first_seen"] == first_seen_1  # original first_seen preserved across revisions


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
