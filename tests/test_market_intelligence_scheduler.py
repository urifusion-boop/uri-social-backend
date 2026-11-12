"""
Uri Market Intelligence — scheduled collection tests (PRD §18, P0-04).

Covers: _due_for_refresh's three cases (no prior scan, in-flight scan,
cadence not yet elapsed vs elapsed), and run_scheduled_market_intelligence_
scans' end-to-end topic selection — only active+keep_updating topics that
are actually due get scanned, an in-flight topic is left alone, and a
budget-limited reservation never triggers execute_scan.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.market_intelligence.models import CollectionRun, ScanStatus, SourceConfig, Topic
from app.agents.market_intelligence.scheduler import _due_for_refresh, run_scheduled_market_intelligence_scans


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


class FakeScansCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    async def find_one(self, query, sort=None):
        matches = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        if sort:
            field, direction = sort[0]
            matches.sort(key=lambda d: d.get(field) or datetime.min, reverse=(direction == -1))
        return dict(matches[0]) if matches else None


class FakeTopicsCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query):
        return FakeCursor([d for d in self.docs if all(d.get(k) == v for k, v in query.items())])


class FakeDb:
    def __init__(self, topics=None, scans=None):
        self._topics = FakeTopicsCollection(topics or [])
        self._scans = FakeScansCollection(scans or [])

    def __getitem__(self, name):
        if name == "mi_topics":
            return self._topics
        if name == "mi_scans":
            return self._scans
        raise AssertionError(f"unexpected collection: {name}")


def _topic_doc(topic_id="t1", keep_updating=True, active=True) -> dict:
    return Topic(
        id=topic_id, brand_id="b1", user_id="u1", question="q",
        keywords=["x"], sources=[SourceConfig(provider="mock", platform="mock")],
        requested_days=30, keep_updating=keep_updating, active=active,
    ).dict()


# ── _due_for_refresh ──────────────────────────────────────────────────────

def test_due_when_no_prior_scan():
    db = FakeDb(scans=[])
    assert _run(_due_for_refresh(db, "t1", 1)) is True


def test_not_due_when_a_scan_is_in_flight():
    db = FakeDb(scans=[{"topic_id": "t1", "status": "collecting", "started_at": datetime.utcnow()}])
    assert _run(_due_for_refresh(db, "t1", 1)) is False


def test_not_due_when_cadence_has_not_elapsed():
    db = FakeDb(scans=[{
        "topic_id": "t1", "status": "completed", "started_at": datetime.utcnow() - timedelta(minutes=10),
    }])
    assert _run(_due_for_refresh(db, "t1", 1)) is False


def test_due_when_cadence_has_elapsed():
    db = FakeDb(scans=[{
        "topic_id": "t1", "status": "completed", "started_at": datetime.utcnow() - timedelta(hours=2),
    }])
    assert _run(_due_for_refresh(db, "t1", 1)) is True


# ── run_scheduled_market_intelligence_scans ─────────────────────────────────

def test_scans_only_active_keep_updating_topics_that_are_due():
    from app.agents.market_intelligence import scheduler

    db = FakeDb(topics=[_topic_doc("t1")], scans=[])  # no prior scan -> due

    fake_run = CollectionRun(id="r1", topic_id="t1", brand_id="b1", source_provider="mock", status=ScanStatus.QUEUED)

    with patch.object(scheduler, "create_scan_run", new=AsyncMock(return_value=fake_run)) as mock_create, \
         patch.object(scheduler, "execute_scan", new=AsyncMock()) as mock_execute:
        result = _run(run_scheduled_market_intelligence_scans(db))

    mock_create.assert_awaited_once()
    mock_execute.assert_awaited_once()
    assert mock_execute.await_args.args[1:] == ("r1", db)
    assert result == {"topics_checked": 1, "scans_started": 1, "skipped": 0}


def test_skips_topic_not_yet_due():
    from app.agents.market_intelligence import scheduler

    db = FakeDb(
        topics=[_topic_doc("t1")],
        scans=[{"topic_id": "t1", "status": "completed", "started_at": datetime.utcnow() - timedelta(minutes=5)}],
    )

    with patch.object(scheduler, "create_scan_run", new=AsyncMock()) as mock_create, \
         patch.object(scheduler, "execute_scan", new=AsyncMock()) as mock_execute:
        result = _run(run_scheduled_market_intelligence_scans(db))

    mock_create.assert_not_awaited()
    mock_execute.assert_not_awaited()
    assert result == {"topics_checked": 1, "scans_started": 0, "skipped": 1}


def test_budget_limited_reservation_never_triggers_execute_scan():
    from app.agents.market_intelligence import scheduler

    db = FakeDb(topics=[_topic_doc("t1")], scans=[])

    limited_run = CollectionRun(
        id="r1", topic_id="t1", brand_id="b1", source_provider="mock", status=ScanStatus.BUDGET_LIMITED
    )

    with patch.object(scheduler, "create_scan_run", new=AsyncMock(return_value=limited_run)), \
         patch.object(scheduler, "execute_scan", new=AsyncMock()) as mock_execute:
        result = _run(run_scheduled_market_intelligence_scans(db))

    mock_execute.assert_not_awaited()
    assert result == {"topics_checked": 1, "scans_started": 0, "skipped": 1}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
