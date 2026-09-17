"""
Uri Market Intelligence — GET /scans/{id} self-heal tests.

Covers the case execute_scan's own crash-safety net can't reach: a run
whose entire background task died some other way (worker killed/redeployed
mid-run), leaving it stuck in a non-terminal status forever. A caller
polling this endpoint should eventually get a terminal answer instead of
polling into the void.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.agents.market_intelligence.models import ScanStatus
from app.agents.market_intelligence.router import _self_heal_stale_scan


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return


class FakeDb:
    def __init__(self, docs):
        self._coll = FakeCollection(docs)

    def __getitem__(self, name):
        return self._coll


def test_heals_a_scan_stuck_collecting_past_the_timeout():
    scan_doc = {
        "id": "run1", "status": "collecting",
        "started_at": datetime.utcnow() - timedelta(minutes=15), "gaps": [],
    }
    db = FakeDb([scan_doc])

    healed = _run(_self_heal_stale_scan(scan_doc, db))

    assert healed["status"] == ScanStatus.FAILED.value
    assert "completed_at" in healed
    assert any("did not complete" in g for g in healed["gaps"])
    # Actually persisted, not just returned.
    assert db["mi_scans"].docs[0]["status"] == ScanStatus.FAILED.value


def test_does_not_heal_a_scan_still_within_the_timeout():
    scan_doc = {
        "id": "run1", "status": "collecting",
        "started_at": datetime.utcnow() - timedelta(minutes=2), "gaps": [],
    }
    db = FakeDb([scan_doc])

    healed = _run(_self_heal_stale_scan(scan_doc, db))

    assert healed["status"] == "collecting"


def test_does_not_touch_an_already_terminal_scan():
    scan_doc = {
        "id": "run1", "status": "completed",
        "started_at": datetime.utcnow() - timedelta(hours=5), "gaps": [],
    }
    db = FakeDb([scan_doc])

    healed = _run(_self_heal_stale_scan(scan_doc, db))

    assert healed["status"] == "completed"
    assert "completed_at" not in healed or healed.get("completed_at") is None


def test_does_not_heal_a_queued_scan_with_no_started_at():
    scan_doc = {"id": "run1", "status": "queued", "started_at": None, "gaps": []}
    db = FakeDb([scan_doc])

    healed = _run(_self_heal_stale_scan(scan_doc, db))

    assert healed["status"] == "queued"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
