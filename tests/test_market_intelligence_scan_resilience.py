"""
Uri Market Intelligence — scan crash-safety tests.

Live bug this covers: execute_scan is a FastAPI background task with no
caller left to notice or retry once it starts — if any unhandled exception
occurred anywhere inside the pipeline (classification, clustering,
notification queueing, budget reconciliation, ...), the run was left
permanently stranded in COLLECTING/ANALYSING, which is exactly what a user
saw as "scan is taking longer than expected" with nothing actually running
in the background. execute_scan() must now ALWAYS drive the run to a
terminal status, even when the real pipeline blows up.
"""
import asyncio
from datetime import datetime

import pytest

from app.agents.market_intelligence.models import ScanStatus, SourceConfig, Topic


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)

    async def find_one(self, query):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None


class FakeDb:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def _topic() -> Topic:
    return Topic(
        id="t1", brand_id="b1", user_id="u1", question="q", keywords=["x"],
        sources=[SourceConfig(provider="mock", platform="mock")], requested_days=30,
    )


def test_execute_scan_marks_run_failed_when_pipeline_raises():
    from app.agents.market_intelligence import scan_runner
    from unittest.mock import patch

    db = FakeDb()
    db["mi_scans"].docs.append({"id": "run1", "topic_id": "t1", "brand_id": "b1", "status": "queued"})

    async def boom(*args, **kwargs):
        raise RuntimeError("boom: simulated bug deep in the pipeline")

    with patch.object(scan_runner, "_fetch_business_context", side_effect=boom):
        _run(scan_runner.execute_scan(_topic(), "run1", db))

    run_doc = db["mi_scans"].docs[0]
    assert run_doc["status"] == ScanStatus.FAILED.value
    assert "completed_at" in run_doc
    assert any("boom" in g for g in run_doc["gaps"])


def test_execute_scan_never_leaves_run_in_a_non_terminal_status_on_crash():
    from app.agents.market_intelligence import scan_runner
    from unittest.mock import patch

    db = FakeDb()
    db["mi_scans"].docs.append({"id": "run1", "topic_id": "t1", "brand_id": "b1", "status": "queued"})

    async def boom(*args, **kwargs):
        raise ValueError("unexpected shape from a real Mongo document")

    with patch.object(scan_runner, "_fetch_business_context", side_effect=boom):
        _run(scan_runner.execute_scan(_topic(), "run1", db))

    non_terminal = {ScanStatus.QUEUED.value, ScanStatus.COLLECTING.value, ScanStatus.ANALYSING.value}
    assert db["mi_scans"].docs[0]["status"] not in non_terminal


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
