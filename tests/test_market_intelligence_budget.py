"""
Uri Market Intelligence — budget reservation tests (PRD §23, P0-15:
"Concurrent jobs cannot reserve beyond allowance; limit causes a partial
result").

The FakeBudgetCollection's find_one_and_update below actually implements the
$expr-based conditional-update semantics reserve_budget() relies on (not a
stub) — it evaluates reserved_usd + spent_usd + amount <= monthly_allowance_usd
against the stored document before applying the $inc, exactly like real
MongoDB would. That's what lets test_two_concurrent_reservations_only_one_
fits below actually prove the cap holds, not just assume it.
"""
import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest

from app.agents.market_intelligence.budget import get_or_create_budget, reconcile_spend, reserve_budget
from app.agents.market_intelligence.models import BrandBudget


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeBudgetCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    async def find_one(self, query):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                self._apply(d, update)
                return

    async def find_one_and_update(self, query, update):
        """Real conditional-update semantics: only applies (and returns) if
        a document matches — including the $expr clause — exactly like
        MongoDB's atomic find_one_and_update. This is what makes the
        concurrency test below meaningful rather than a tautology."""
        expr = query.pop("$expr", None)
        for d in self.docs:
            if not all(d.get(k) == v for k, v in query.items()):
                continue
            if expr is not None and not self._eval_expr(expr, d):
                continue
            self._apply(d, update)
            return dict(d)
        return None

    def _apply(self, doc, update):
        for k, v in update.get("$inc", {}).items():
            doc[k] = doc.get(k, 0) + v
        doc.update(update.get("$set", {}))

    def _eval_expr(self, expr, doc):
        # Only supports the exact shape reserve_budget() emits:
        # {"$lte": [{"$add": ["$reserved_usd", "$spent_usd", amount]}, "$monthly_allowance_usd"]}
        lte = expr["$lte"]
        add_expr, allowance_field = lte
        total = sum(doc[f[1:]] if isinstance(f, str) and f.startswith("$") else f for f in add_expr["$add"])
        allowance = doc[allowance_field[1:]]
        return total <= allowance


class FakeBudgetDb:
    def __init__(self, docs=None):
        self._coll = FakeBudgetCollection(docs)

    def __getitem__(self, name):
        return self._coll


# ── get_or_create_budget ─────────────────────────────────────────────────────

def test_creates_default_budget_on_first_use():
    db = FakeBudgetDb()
    budget = _run(get_or_create_budget(db, "b1"))
    assert budget["brand_id"] == "b1"
    assert budget["monthly_allowance_usd"] == 10.0
    assert budget["reserved_usd"] == 0.0
    assert budget["spent_usd"] == 0.0


def test_resets_usage_on_period_rollover_but_keeps_allowance():
    db = FakeBudgetDb([{
        "brand_id": "b1", "monthly_allowance_usd": 25.0, "period": "2020-01",
        "reserved_usd": 20.0, "spent_usd": 4.0, "updated_at": datetime(2020, 1, 15),
    }])
    budget = _run(get_or_create_budget(db, "b1"))
    assert budget["period"] != "2020-01"
    assert budget["reserved_usd"] == 0.0
    assert budget["spent_usd"] == 0.0
    assert budget["monthly_allowance_usd"] == 25.0  # allowance itself is never reset


# ── reserve_budget ────────────────────────────────────────────────────────────

def test_reserves_when_within_allowance():
    db = FakeBudgetDb()
    ok, reason = _run(reserve_budget(db, "b1", 3.0))
    assert ok is True
    budget = _run(get_or_create_budget(db, "b1"))
    assert budget["reserved_usd"] == 3.0


def test_zero_cost_reservation_always_succeeds_without_touching_budget():
    db = FakeBudgetDb()
    ok, _ = _run(reserve_budget(db, "b1", 0.0))
    assert ok is True
    budget = _run(get_or_create_budget(db, "b1"))
    assert budget["reserved_usd"] == 0.0


def test_rejects_reservation_that_would_exceed_allowance():
    db = FakeBudgetDb([{
        "brand_id": "b1", "monthly_allowance_usd": 5.0, "period": _run(get_or_create_budget(FakeBudgetDb(), "x"))["period"],
        "reserved_usd": 4.0, "spent_usd": 0.0, "updated_at": datetime.utcnow(),
    }])
    ok, reason = _run(reserve_budget(db, "b1", 2.0))  # 4 + 0 + 2 = 6 > 5
    assert ok is False
    assert "exceed" in reason
    # Rejected reservation must not have mutated reserved_usd.
    budget = _run(get_or_create_budget(db, "b1"))
    assert budget["reserved_usd"] == 4.0


def test_two_concurrent_reservations_only_one_fits():
    """The core P0-15 guarantee: two requests each asking for more than half
    the remaining allowance can't both succeed."""
    period = _run(get_or_create_budget(FakeBudgetDb(), "x"))["period"]
    db = FakeBudgetDb([{
        "brand_id": "b1", "monthly_allowance_usd": 5.0, "period": period,
        "reserved_usd": 0.0, "spent_usd": 0.0, "updated_at": datetime.utcnow(),
    }])
    ok1, _ = _run(reserve_budget(db, "b1", 3.0))
    ok2, _ = _run(reserve_budget(db, "b1", 3.0))  # 3 + 3 = 6 > 5
    assert ok1 is True
    assert ok2 is False
    budget = _run(get_or_create_budget(db, "b1"))
    assert budget["reserved_usd"] == 3.0  # never both


# ── reconcile_spend ───────────────────────────────────────────────────────────

def test_reconcile_releases_reservation_and_books_spend():
    period = _run(get_or_create_budget(FakeBudgetDb(), "x"))["period"]
    db = FakeBudgetDb([{
        "brand_id": "b1", "monthly_allowance_usd": 10.0, "period": period,
        "reserved_usd": 3.0, "spent_usd": 0.0, "updated_at": datetime.utcnow(),
    }])
    _run(reconcile_spend(db, "b1", reserved_usd=3.0, actual_usd=3.0))
    budget = _run(get_or_create_budget(db, "b1"))
    assert budget["reserved_usd"] == 0.0
    assert budget["spent_usd"] == 3.0


# ── create_scan_run integration: an over-budget scan is BUDGET_LIMITED ──────

class FakeMiScansCollection:
    def __init__(self):
        self.inserted: list[dict] = []

    async def insert_one(self, doc):
        self.inserted.append(doc)


class FakeIntegrationDb:
    """Backs both mi_budgets (real conditional-update semantics, reused from
    FakeBudgetCollection) and mi_scans (plain insert capture) — everything
    create_scan_run touches."""
    def __init__(self):
        self._mi_budgets = FakeBudgetCollection()
        self._mi_scans = FakeMiScansCollection()

    def __getitem__(self, name):
        if name == "mi_budgets":
            return self._mi_budgets
        if name == "mi_scans":
            return self._mi_scans
        raise AssertionError(f"unexpected collection: {name}")


class _ExpensiveAdapter:
    """A minimal SourceAdapter stand-in whose estimated cost alone exceeds
    the $10 default monthly allowance — enough to prove create_scan_run
    actually blocks on budget, not just on paper."""
    def capabilities(self):
        from app.agents.market_intelligence.adapters.base import AdapterCapabilities
        return AdapterCapabilities(
            provider="expensive", platform="expensive", verified_lookback_days=30,
            supports_date_filters=True, supports_keyword_search=True,
            accessible_languages=["en"], refresh_cadence_hours=1,
        )

    async def estimate_cost(self, keywords, days):
        return 50.0  # far beyond the $10 pilot default allowance

    async def start_collection(self, keywords, excluded_keywords, since, until):
        raise AssertionError("must never be called for a budget-limited run")

    async def fetch_page(self, run_id, cursor=None):
        raise AssertionError("must never be called for a budget-limited run")

    async def get_status(self, run_id):
        raise AssertionError("unused in this test")

    async def cancel_if_supported(self, run_id):
        return False


def test_create_scan_run_marks_over_budget_topic_as_budget_limited():
    from app.agents.market_intelligence import scan_runner
    from app.agents.market_intelligence.models import ScanStatus, SourceConfig, Topic

    topic = Topic(
        id="t1", brand_id="b1", user_id="u1", question="Expensive research",
        keywords=["x"], sources=[SourceConfig(provider="expensive", platform="expensive")],
        requested_days=30,
    )
    db = FakeIntegrationDb()

    with patch.dict(scan_runner.ADAPTER_REGISTRY, {"expensive": _ExpensiveAdapter()}):
        run = _run(scan_runner.create_scan_run(topic, db))

    assert run.status == ScanStatus.BUDGET_LIMITED
    assert run.estimated_cost_usd == 50.0
    assert any("budget limit" in g for g in run.gaps)
    # The run IS recorded — visibly limited, never silently dropped.
    assert len(db["mi_scans"].inserted) == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
