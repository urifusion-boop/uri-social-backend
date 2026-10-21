"""
Product metrics (PRD §17): share of cards rated useful or acted on,
dismissal reasons, content-created rate — computed only from
mi_listen_cards/mi_listen_card_actions already recorded. Two PRD-named
metrics (first-draft approval, ad-test cost per qualified lead) are
explicitly reported as unavailable rather than invented, since nothing
tracks either yet. Uses an in-memory MongoDB; no network, no AI.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import metrics
from app.agents.market_intelligence.listening.router import product_metrics_endpoint

NOW = datetime(2026, 10, 21, 12, 0)
MARKET_KEY = "hair_wigs:NG"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["metrics_test"]


async def _card(db, cid, market=MARKET_KEY, days_ago=1):
    await db["mi_listen_cards"].insert_one({"id": cid, "brand_id": "b1", "market": market,
                                            "created_at": NOW - timedelta(days=days_ago)})


async def _action(db, cid, action, market=MARKET_KEY, reason=None, days_ago=1, seconds_offset=0):
    await db["mi_listen_card_actions"].insert_one({
        "card_id": cid, "market": market, "action": action, "reason": reason,
        "at": NOW - timedelta(days=days_ago) + timedelta(seconds=seconds_offset),
    })


ADMIN = {"claims": {"email": "admin@urisocial.com"}}


class TestProductMetrics:
    def test_no_cards_in_window_returns_a_clean_zero(self):
        db = fresh_db()
        result = run(metrics.product_metrics(db, now=NOW))
        assert result["cards_shown"] == 0
        assert "not_available" in result

    def test_useful_and_dismissal_rates(self):
        db = fresh_db()
        for cid in ("c1", "c2", "c3", "c4"):
            run(_card(db, cid))
        run(_action(db, "c1", "saved"))
        run(_action(db, "c2", "acted"))
        run(_action(db, "c3", "dismissed", reason="Not relevant to us"))
        run(_action(db, "c4", "dismissed", reason="Not relevant to us"))

        result = run(metrics.product_metrics(db, now=NOW))

        assert result["cards_shown"] == 4
        assert result["useful_or_acted_on_rate"] == 0.5
        assert result["dismissal_rate"] == 0.5
        assert result["dismissal_reasons"] == {"Not relevant to us": 2}

    def test_content_created_rate(self):
        db = fresh_db()
        for cid in ("c1", "c2"):
            run(_card(db, cid))
        run(_action(db, "c1", "created"))
        run(_action(db, "c2", "shown"))

        result = run(metrics.product_metrics(db, now=NOW))

        assert result["content_created_count"] == 1
        assert result["content_created_rate"] == 0.5

    def test_only_the_latest_action_per_card_counts(self):
        db = fresh_db()
        run(_card(db, "c1"))
        run(_action(db, "c1", "dismissed", seconds_offset=0))
        run(_action(db, "c1", "saved", seconds_offset=10))  # owner changed their mind

        result = run(metrics.product_metrics(db, now=NOW))

        assert result["useful_or_acted_on_rate"] == 1.0
        assert result["dismissal_rate"] == 0.0

    def test_reason_defaults_when_none_given(self):
        db = fresh_db()
        run(_card(db, "c1"))
        run(_action(db, "c1", "dismissed", reason=None))

        result = run(metrics.product_metrics(db, now=NOW))

        assert result["dismissal_reasons"] == {"no reason given": 1}

    def test_cards_outside_the_window_are_excluded(self):
        db = fresh_db()
        run(_card(db, "c1", days_ago=60))
        result = run(metrics.product_metrics(db, window_days=30, now=NOW))
        assert result["cards_shown"] == 0

    def test_filters_by_market(self):
        db = fresh_db()
        run(_card(db, "c1", market=MARKET_KEY))
        run(_card(db, "c2", market="beauty_skincare:NG"))
        result = run(metrics.product_metrics(db, market=MARKET_KEY, now=NOW))
        assert result["cards_shown"] == 1

    def test_unavailable_metrics_are_named_not_invented(self):
        db = fresh_db()
        run(_card(db, "c1"))
        result = run(metrics.product_metrics(db, now=NOW))
        assert "first_draft_approval_rate" in result["not_available"]
        assert "ad_test_cost_per_qualified_lead" in result["not_available"]
        assert "first_draft_approval_rate" not in result  # not silently reported as a real 0 or fake number


class TestMetricsRouter:
    @pytest.mark.asyncio
    async def test_endpoint_delegates_correctly(self):
        db = fresh_db()
        await _card(db, "c1")
        await _action(db, "c1", "saved")
        result = await product_metrics_endpoint(market=None, window_days=30, admin=ADMIN, db=db)
        assert result["responseData"]["cards_shown"] == 1
        assert result["responseData"]["useful_or_acted_on_rate"] == 1.0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
