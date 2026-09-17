"""
Uri Market Intelligence — Development entity / upcoming-development workflow
tests (PRD §12, §13, P0-10: "Cancellation revises the item and suppresses
pending preparation alerts").

Covers: the eligibility gate (source + verifiable date + preparation action
all required), that execute_scan wires classified UPCOMING_DEVELOPMENT
evidence through extraction into a persisted Development record, and that
an ineligible extraction (no date, no source) is left out with a visible gap
rather than silently dropped or fabricated.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.agents.market_intelligence.classification.scoring import is_development_eligible
from app.agents.market_intelligence.development_extractor import DevelopmentExtraction
from app.agents.market_intelligence.models import (
    Classification,
    Evidence,
    EvidenceType,
    Geography,
    SourceConfig,
    Topic,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _evidence(source_id: str, text: str, url: str = "https://example.com/post") -> Evidence:
    return Evidence(
        id=f"e_{source_id}", brand_id="b1", user_id="u1", topic_id="t1",
        provider="mock", platform="mock", source_id=source_id, url=url, text=text,
        collected_at=datetime.now(timezone.utc), geography=Geography(),
    )


# ── is_development_eligible ──────────────────────────────────────────────────

def test_eligible_with_source_date_and_preparation_action():
    ev = _evidence("d1", "Lagos Fashion Week announced for March 2027, register by Jan.")
    eligible, reason = is_development_eligible(
        ev, has_verifiable_source=True, event_date=datetime(2027, 3, 1), preparation_action="Prepare a booth"
    )
    assert eligible is True


def test_ineligible_without_source_url():
    ev = _evidence("d2", "Some event is happening", url=None)
    eligible, reason = is_development_eligible(
        ev, has_verifiable_source=True, event_date=datetime(2027, 3, 1), preparation_action="Prepare"
    )
    assert eligible is False
    assert "source URL" in reason


def test_ineligible_when_not_verifiable():
    ev = _evidence("d3", "I heard there might be an event soon")
    eligible, reason = is_development_eligible(
        ev, has_verifiable_source=False, event_date=datetime(2027, 3, 1), preparation_action="Prepare"
    )
    assert eligible is False
    assert "verify" in reason


def test_ineligible_without_event_date():
    ev = _evidence("d4", "Lagos Fashion Week is coming back")
    eligible, reason = is_development_eligible(
        ev, has_verifiable_source=True, event_date=None, preparation_action="Prepare"
    )
    assert eligible is False
    assert "date to confirm" in reason


def test_ineligible_without_preparation_action():
    ev = _evidence("d5", "Lagos Fashion Week announced for March 2027")
    eligible, reason = is_development_eligible(
        ev, has_verifiable_source=True, event_date=datetime(2027, 3, 1), preparation_action=None
    )
    assert eligible is False
    assert "preparation action" in reason


# ── execute_scan wiring ──────────────────────────────────────────────────────

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
        return FakeCursor([])

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


def _topic() -> Topic:
    return Topic(
        id="t1", brand_id="b1", user_id="u1",
        question="What's happening in fashion events?",
        keywords=["fashion"],
        sources=[SourceConfig(provider="mock", platform="mock")],
        requested_days=30,
    )


def test_execute_scan_creates_development_for_eligible_extraction():
    from app.agents.market_intelligence import scan_runner

    db = FakeDb()
    topic = _topic()

    async def fake_classify(evidence, business_context):
        return Classification(
            evidence_id=evidence.id, primary_type=EvidenceType.UPCOMING_DEVELOPMENT,
            evidence_span=evidence.text[:50], reasoning="stub",
        )

    async def fake_extract(evidence, business_context):
        return DevelopmentExtraction(
            issuer="Lagos Fashion Week Org",
            headline="Lagos Fashion Week 2027",
            event_date=datetime.utcnow() + timedelta(days=90),
            preparation_action="Prepare a booth submission",
            has_verifiable_source=True,
        )

    async def fake_business_context(db, user_id, brand_id):
        return {"brand_name": "Test Brand", "industry": "fashion", "key_products_services": []}

    async def fake_cluster_evidence(evidence, **kwargs):
        return []

    with patch.object(scan_runner, "classify_evidence", side_effect=fake_classify), \
         patch.object(scan_runner, "extract_development", side_effect=fake_extract), \
         patch.object(scan_runner, "_fetch_business_context", side_effect=fake_business_context), \
         patch.object(scan_runner, "cluster_evidence", side_effect=fake_cluster_evidence):
        _run(scan_runner.execute_scan(topic, "run1", db))

    developments = db["mi_developments"].inserted
    # The mock adapter's fixture has 10 items; n1 is caught by the
    # deterministic noise filter before classify_evidence even runs (see
    # test_market_intelligence_noise_filter.py), so 9 reach this stubbed
    # classify->extract path and all become eligible developments.
    assert len(developments) == 9
    assert all(d["issuer"] == "Lagos Fashion Week Org" for d in developments)
    assert all(d["status"] == "scheduled" for d in developments)


def test_execute_scan_leaves_ineligible_development_as_a_gap_not_fabricated():
    from app.agents.market_intelligence import scan_runner

    db = FakeDb()
    topic = _topic()

    async def fake_classify(evidence, business_context):
        return Classification(
            evidence_id=evidence.id, primary_type=EvidenceType.UPCOMING_DEVELOPMENT,
            evidence_span=evidence.text[:50], reasoning="stub",
        )

    async def fake_extract(evidence, business_context):
        # No event_date, no preparation_action — an honest "couldn't verify" extraction.
        return DevelopmentExtraction(
            headline="Vague mention of a possible event",
            has_verifiable_source=False,
        )

    async def fake_business_context(db, user_id, brand_id):
        return {"brand_name": "Test Brand", "industry": "fashion", "key_products_services": []}

    async def fake_cluster_evidence(evidence, **kwargs):
        return []

    with patch.object(scan_runner, "classify_evidence", side_effect=fake_classify), \
         patch.object(scan_runner, "extract_development", side_effect=fake_extract), \
         patch.object(scan_runner, "_fetch_business_context", side_effect=fake_business_context), \
         patch.object(scan_runner, "cluster_evidence", side_effect=fake_cluster_evidence):
        _run(scan_runner.execute_scan(topic, "run1", db))

    assert db["mi_developments"].inserted == []


# ── PATCH /developments/{id}: cancellation revises the same item ───────────

class FakeDevCollection:
    def __init__(self, docs):
        self.docs = docs

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


class FakeDevDb:
    def __init__(self, docs):
        self._coll = FakeDevCollection(docs)

    def __getitem__(self, name):
        return self._coll


def test_patch_cancellation_revises_same_item_not_a_new_one():
    from app.agents.market_intelligence.models import DevelopmentUpdateRequest, DevelopmentStatus
    from app.agents.market_intelligence.router import update_development

    original = {
        "id": "dev1", "brand_id": "b1", "topic_id": "t1", "evidence_id": "e1",
        "issuer": "Lagos Fashion Week Org", "headline": "Lagos Fashion Week 2027",
        "event_date": datetime(2027, 3, 1), "event_date_range_end": None, "location": None,
        "registration_deadline": None, "preparation_action": "Prepare a booth",
        "source_url": "https://example.com/x", "status": "scheduled", "verification_note": None,
        "first_seen": datetime(2026, 1, 1), "last_updated": datetime(2026, 1, 1),
    }
    db = FakeDevDb([original])

    result = _run(update_development(
        "dev1",
        DevelopmentUpdateRequest(status=DevelopmentStatus.CANCELLED, verification_note="Organizer confirmed cancellation"),
        ctx={"brand_id": "b1", "user_id": "u1"},
        db=db,
    ))

    # Same id, same issuer/headline — revised in place, not replaced.
    assert db["mi_developments"].docs[0]["id"] == "dev1"
    assert db["mi_developments"].docs[0]["status"] == "cancelled"
    assert db["mi_developments"].docs[0]["verification_note"] == "Organizer confirmed cancellation"
    assert db["mi_developments"].docs[0]["issuer"] == "Lagos Fashion Week Org"
    assert len(db["mi_developments"].docs) == 1  # never a second record


def test_patch_rejects_cross_tenant_development():
    from fastapi import HTTPException
    from app.agents.market_intelligence.models import DevelopmentUpdateRequest, DevelopmentStatus
    from app.agents.market_intelligence.router import update_development

    db = FakeDevDb([{"id": "dev1", "brand_id": "OTHER_BRAND"}])
    with pytest.raises(HTTPException) as exc_info:
        _run(update_development(
            "dev1", DevelopmentUpdateRequest(status=DevelopmentStatus.CANCELLED),
            ctx={"brand_id": "b1", "user_id": "u1"}, db=db,
        ))
    assert exc_info.value.status_code == 404


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
