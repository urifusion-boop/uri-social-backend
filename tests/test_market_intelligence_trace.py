"""
Uri Market Intelligence — observability trace tests (PRD §16/§17, P0-16:
"A finding can be traced to topic, collection, evidence and model
versions").
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.agents.market_intelligence.models import Classification, EvidenceType, SourceConfig, Topic
from app.agents.market_intelligence.router import get_insight_trace


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
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    def _matches(self, doc, query):
        for key, cond in query.items():
            val = doc.get(key)
            if isinstance(cond, dict) and "$in" in cond:
                if val not in cond["$in"]:
                    return False
            elif val != cond:
                return False
        return True

    async def find_one(self, query):
        for d in self.docs:
            if self._matches(d, query):
                return dict(d)
        return None

    def find(self, query=None, projection=None):
        return FakeCursor([dict(d) for d in self.docs if self._matches(d, query or {})])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def insert_many(self, docs):
        self.docs.extend(dict(d) for d in docs)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]]):
        self._colls = {name: FakeCollection(docs) for name, docs in collections.items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


CTX = {"brand_id": "b1", "user_id": "u1"}


def test_trace_walks_evidence_scans_and_model_versions():
    db = FakeDb({
        "mi_insights": [{
            "id": "i1", "brand_id": "b1", "revision": 2, "topic_id": "t1", "cluster_id": "c1",
            "evidence_ids": ["e1", "e2"],
        }],
        "mi_evidence": [
            {"id": "e1", "brand_id": "b1", "collection_run_id": "run1"},
            {"id": "e2", "brand_id": "b1", "collection_run_id": "run2"},
        ],
        "mi_classifications": [
            {"evidence_id": "e1", "model_name": "gpt-4o-mini", "prompt_version": "mi-classify-v1"},
            {"evidence_id": "e2", "model_name": "gpt-4o-mini", "prompt_version": "mi-classify-v1"},
        ],
        "mi_scans": [
            {"id": "run1", "topic_id": "t1", "status": "completed", "provider_run_ids": {"mock": "mockrun_abc"}},
            {"id": "run2", "topic_id": "t1", "status": "completed", "provider_run_ids": {"mock": "mockrun_def"}},
        ],
    })

    res = _run(get_insight_trace("i1", ctx=CTX, db=db))
    trace = res["responseData"]

    assert trace["insight_revision"] == 2
    assert trace["topic_id"] == "t1"
    assert trace["cluster_id"] == "c1"
    assert set(trace["evidence_ids"]) == {"e1", "e2"}
    assert {s["id"] for s in trace["collection_runs"]} == {"run1", "run2"}
    assert trace["collection_runs"][0]["provider_run_ids"]  # real provider run id survived the round trip
    assert trace["model_versions"] == ["gpt-4o-mini@mi-classify-v1"]
    assert len(trace["classifications"]) == 2


def test_trace_rejects_cross_tenant_insight():
    db = FakeDb({"mi_insights": [{"id": "i1", "brand_id": "OTHER_BRAND"}]})
    with pytest.raises(HTTPException) as exc_info:
        _run(get_insight_trace("i1", ctx=CTX, db=db))
    assert exc_info.value.status_code == 404


# ── execute_scan persists provider_run_ids and Evidence.collection_run_id ──

class SimpleFakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    def find(self, query=None, projection=None):
        return FakeCursor([])

    async def find_one(self, query):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def insert_many(self, docs):
        self.docs.extend(dict(d) for d in docs)

    def _apply_set(self, doc, set_fields):
        # Mirrors real MongoDB's dotted-path $set semantics (e.g.
        # "provider_run_ids.mock") — nests into (creating if needed) rather
        # than setting a literal key containing a dot.
        for key, value in set_fields.items():
            parts = key.split(".")
            target = doc
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                self._apply_set(d, update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(query)
            self._apply_set(new_doc, update.get("$set", {}))
            self.docs.append(new_doc)


class SimpleFakeDb:
    def __init__(self):
        self._colls: dict[str, SimpleFakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, SimpleFakeCollection())


def test_execute_scan_persists_provider_run_id_and_stamps_evidence():
    from app.agents.market_intelligence import scan_runner

    db = SimpleFakeDb()
    # Seed the run doc the way create_scan_run would, so the $set update_one has something to match.
    db["mi_scans"].docs.append({"id": "run1", "topic_id": "t1", "brand_id": "b1", "estimated_cost_usd": 0.0, "gaps": []})

    topic = Topic(
        id="t1", brand_id="b1", user_id="u1", question="q", keywords=["delivery"],
        sources=[SourceConfig(provider="mock", platform="mock")], requested_days=7,
    )

    async def fake_classify(evidence, business_context):
        return Classification(
            evidence_id=evidence.id, primary_type=EvidenceType.NOISE,
            evidence_span=evidence.text[:50], reasoning="stub",
        )

    async def fake_business_context(db, user_id, brand_id):
        return {"brand_name": "T", "industry": "x", "key_products_services": []}

    with patch.object(scan_runner, "classify_evidence", side_effect=fake_classify), \
         patch.object(scan_runner, "_fetch_business_context", side_effect=fake_business_context):
        _run(scan_runner.execute_scan(topic, "run1", db))

    run_doc = db["mi_scans"].docs[0]
    assert run_doc["provider_run_ids"]["mock"].startswith("mockrun_")

    evidence_docs = db["mi_evidence"].docs
    assert len(evidence_docs) > 0
    assert all(e["collection_run_id"] == "run1" for e in evidence_docs)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
