"""
Uri Market Intelligence — trend eligibility and lifecycle tests (PRD §12,
part of P0-08).

is_trend_eligible/compute_lifecycle are tested directly with hand-crafted
data (precise and fast). The scan_runner wiring — that an EMERGING_TREND
cluster is routed through is_trend_eligible instead of is_concern_eligible,
that its counters/lifecycle are updated and persisted, and that lifecycle
actually progresses EARLY -> EMERGING -> COOLING across scans — is proven
separately with is_trend_eligible mocked (its own correctness is already
covered above), reusing the multi-scan merge harness from
test_market_intelligence_cluster_persistence.py.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.agents.market_intelligence.adapters.base import AdapterCapabilities, CollectionPage
from app.agents.market_intelligence.classification.scoring import compute_lifecycle, is_trend_eligible
from app.agents.market_intelligence.models import (
    Classification,
    Cluster,
    Evidence,
    EvidenceType,
    Geography,
    InsightVersion,
    Lifecycle,
    RawEvidence,
    ScanStatus,
    SourceConfig,
    Topic,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _ev(author: str, days_ago: float, hours_ago: float = 0) -> Evidence:
    published = datetime.utcnow() - timedelta(days=days_ago, hours=hours_ago)
    return Evidence(
        id=f"e_{author}_{days_ago}_{hours_ago}", brand_id="b1", user_id="u1", topic_id="t1",
        provider="mock", platform="mock", source_id=f"s_{author}_{days_ago}_{hours_ago}",
        text="trend post", collected_at=datetime.now(timezone.utc), author_handle=author,
        published_at=published, geography=Geography(),
    )


def _well_formed_trend_evidence() -> list[Evidence]:
    """10 distinct authors in the latest 24h, 21 distinct authors spread
    evenly across the prior 7 days (exactly 3/day baseline) — satisfies
    every bar with no single dominant author."""
    latest = [_ev(f"latest_{i}", days_ago=0, hours_ago=i) for i in range(10)]
    baseline = [_ev(f"base_{d}_{i}", days_ago=1 + d) for d in range(7) for i in range(3)]
    return latest + baseline


# ── is_trend_eligible ─────────────────────────────────────────────────────

def test_eligible_well_formed_trend():
    eligible, reason = is_trend_eligible(_well_formed_trend_evidence())
    assert eligible is True


def test_ineligible_too_few_latest_accounts():
    evidence = _well_formed_trend_evidence()[:5] + _well_formed_trend_evidence()[10:]  # only 5 latest accounts
    eligible, reason = is_trend_eligible(evidence)
    assert eligible is False
    assert "latest complete 24h" in reason


def test_ineligible_thin_baseline():
    latest = [_ev(f"latest_{i}", days_ago=0) for i in range(10)]
    thin_baseline = [_ev(f"base_{i}", days_ago=3) for i in range(2)]  # well under 3/day
    eligible, reason = is_trend_eligible(latest + thin_baseline)
    assert eligible is False
    assert "baseline" in reason


def test_ineligible_not_double_baseline():
    latest = [_ev(f"latest_{i}", days_ago=0) for i in range(10)]
    # Baseline high enough to pass the minimum-per-day bar but too close to latest volume.
    heavy_baseline = [_ev(f"base_{d}_{i}", days_ago=1 + d) for d in range(7) for i in range(6)]  # 6/day
    eligible, reason = is_trend_eligible(latest + heavy_baseline)
    assert eligible is False
    assert "twice" in reason


def test_ineligible_dominant_repost_cascade():
    # 10 distinct latest-day accounts (clears the account-count bar), but
    # "spammer" alone accounts for the majority of raw post VOLUME that day.
    dominant_posts = [_ev("spammer", days_ago=0, hours_ago=i) for i in range(15)]
    other_accounts = [_ev(f"real_{i}", days_ago=0, hours_ago=i) for i in range(9)]
    baseline = [_ev(f"base_{d}_{i}", days_ago=1 + d) for d in range(7) for i in range(3)]
    eligible, reason = is_trend_eligible(dominant_posts + other_accounts + baseline)
    assert eligible is False
    assert "cascade" in reason


# ── compute_lifecycle ────────────────────────────────────────────────────────

def test_first_eligible_evaluation_is_early():
    assert compute_lifecycle(Lifecycle.UNKNOWN, True, eligible_evaluation_count=1, consecutive_ineligible_count=0) == Lifecycle.EARLY


def test_second_eligible_evaluation_is_emerging():
    assert compute_lifecycle(Lifecycle.EARLY, True, eligible_evaluation_count=2, consecutive_ineligible_count=0) == Lifecycle.EMERGING


def test_third_eligible_evaluation_is_established():
    assert compute_lifecycle(Lifecycle.EMERGING, True, eligible_evaluation_count=3, consecutive_ineligible_count=0) == Lifecycle.ESTABLISHED


def test_single_ineligible_evaluation_does_not_downgrade_emerging():
    assert compute_lifecycle(Lifecycle.EMERGING, False, eligible_evaluation_count=2, consecutive_ineligible_count=1) == Lifecycle.EMERGING


def test_two_consecutive_ineligible_evaluations_cools_emerging():
    assert compute_lifecycle(Lifecycle.EMERGING, False, eligible_evaluation_count=2, consecutive_ineligible_count=2) == Lifecycle.COOLING


def test_ineligible_never_cools_early_or_unknown():
    assert compute_lifecycle(Lifecycle.EARLY, False, eligible_evaluation_count=1, consecutive_ineligible_count=5) == Lifecycle.EARLY
    assert compute_lifecycle(Lifecycle.UNKNOWN, False, eligible_evaluation_count=0, consecutive_ineligible_count=5) == Lifecycle.UNKNOWN


# ── scan_runner wiring across multiple scans ─────────────────────────────────

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
    def __init__(self, source_id: str, author: str):
        self._evidence = RawEvidence(
            provider="fake", platform="fake", source_id=source_id,
            url=f"https://example.com/{source_id}", text="trend post",
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
        evidence_id=evidence.id, primary_type=EvidenceType.EMERGING_TREND,
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
        first_seen=cluster.first_seen, last_updated=datetime.utcnow(), lifecycle=cluster.lifecycle,
    )


def _make_cluster_evidence_stub(shared_centroid):
    async def fake_cluster_evidence(evidence_list, **kwargs):
        e = evidence_list[0]
        return [Cluster(
            id=str(uuid.uuid4()), brand_id="b1", topic_id="t1", primary_type=EvidenceType.EMERGING_TREND,
            theme="trend theme", evidence_ids=[e.id], independent_account_count=1, original_thread_count=1,
            first_seen=e.published_at, last_updated=datetime.utcnow(), embedding_centroid=shared_centroid,
        )]
    return fake_cluster_evidence


def _run_one_scan(db, source_id, author, run_id, is_trend_eligible_result, shared_centroid):
    from app.agents.market_intelligence import scan_runner
    topic = _topic()
    with patch.dict(scan_runner.ADAPTER_REGISTRY, {"fake": _SingleEvidenceAdapter(source_id, author)}), \
         patch.object(scan_runner, "classify_evidence", side_effect=_fake_classify), \
         patch.object(scan_runner, "_fetch_business_context", side_effect=_fake_business_context), \
         patch.object(scan_runner, "cluster_evidence", side_effect=_make_cluster_evidence_stub(shared_centroid)), \
         patch.object(scan_runner, "compose_insight", side_effect=_fake_compose), \
         patch.object(scan_runner, "is_trend_eligible", return_value=is_trend_eligible_result):
        _run(scan_runner.execute_scan(topic, run_id, db))


def test_lifecycle_progresses_from_early_to_emerging_to_cooling_across_scans():
    db = GenericFakeDb()
    centroid = [1.0, 0.0]

    # Scan 1: eligible -> EARLY, 1st eligible evaluation.
    _run_one_scan(db, "e1", "@a1", "run1", (True, "eligible"), centroid)
    cluster = db["mi_clusters"].docs[0]
    cluster_id = cluster["id"]
    assert cluster["eligible_evaluation_count"] == 1
    assert cluster["lifecycle"] == Lifecycle.EARLY.value
    active = [d for d in db["mi_insights"].docs if d["status"] == "active"]
    assert len(active) == 1  # eligible -> insight composed

    # Scan 2: eligible again -> EMERGING, 2nd eligible evaluation.
    _run_one_scan(db, "e2", "@a2", "run2", (True, "eligible"), centroid)
    cluster = next(d for d in db["mi_clusters"].docs if d["id"] == cluster_id)
    assert cluster["eligible_evaluation_count"] == 2
    assert cluster["lifecycle"] == Lifecycle.EMERGING.value

    # Scan 3 & 4: two consecutive ineligible evaluations -> COOLING.
    _run_one_scan(db, "e3", "@a3", "run3", (False, "volume dropped"), centroid)
    cluster = next(d for d in db["mi_clusters"].docs if d["id"] == cluster_id)
    assert cluster["consecutive_ineligible_count"] == 1
    assert cluster["lifecycle"] == Lifecycle.EMERGING.value  # one blip doesn't cool it yet

    _run_one_scan(db, "e4", "@a4", "run4", (False, "volume dropped further"), centroid)
    cluster = next(d for d in db["mi_clusters"].docs if d["id"] == cluster_id)
    assert cluster["consecutive_ineligible_count"] == 2
    assert cluster["lifecycle"] == Lifecycle.COOLING.value

    # Still exactly one cluster throughout — every scan merged into it.
    assert len({d["id"] for d in db["mi_clusters"].docs}) == 1
    # Ineligible scans must not have produced a new active insight beyond
    # the 2 genuinely-eligible ones (scan 1 and 2).
    active_ever = [d for d in db["mi_insights"].docs]
    assert len(active_ever) == 2


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
