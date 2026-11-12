"""
Product metrics (PRD §17): share of cards rated useful or acted on,
dismissal reasons, content-created rate — computed from
mi_listen_cards/mi_listen_card_actions, plus two real joins once the data to
join against exists: first-draft approval (content_drafts.status) and
ad-test cost per qualified lead (jane_ads' own spend/lead-stage data, for a
card's own opened thread). Both are still reported as explicitly unavailable
— with a specific reason — rather than invented when nothing to join against
exists yet. Uses an in-memory MongoDB; no network, no AI.
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


class TestFirstDraftApprovalRate:
    def test_no_drafts_created_is_unavailable(self):
        db = fresh_db()
        run(_card(db, "c1"))
        result = run(metrics.product_metrics(db, now=NOW))
        assert "first_draft_approval_rate" in result["not_available"]
        assert "first_draft_approval_rate" not in result

    def test_approved_and_denied_drafts_compute_a_real_rate(self):
        db = fresh_db()
        run(db["mi_listen_cards"].insert_one({
            "id": "c1", "brand_id": "b1", "market": MARKET_KEY, "created_at": NOW - timedelta(days=1),
            "created_draft_ids": ["d1", "d2", "d3"],
        }))
        run(db["content_drafts"].insert_one({"id": "d1", "status": "approved"}))
        run(db["content_drafts"].insert_one({"id": "d2", "status": "denied"}))
        run(db["content_drafts"].insert_one({"id": "d3", "status": "pending_approval"}))  # still undecided

        result = run(metrics.product_metrics(db, now=NOW))

        fd = result["first_draft_approval_rate"]
        assert fd == {"drafts_created": 3, "drafts_decided": 2, "drafts_approved": 1, "drafts_pending": 1,
                     "rate": 0.5}
        assert "first_draft_approval_rate" not in result.get("not_available", {})

    def test_refined_counts_as_not_approved_on_first_draft(self):
        db = fresh_db()
        run(db["mi_listen_cards"].insert_one({
            "id": "c1", "brand_id": "b1", "market": MARKET_KEY, "created_at": NOW - timedelta(days=1),
            "created_draft_ids": ["d1"],
        }))
        run(db["content_drafts"].insert_one({"id": "d1", "status": "refined"}))

        result = run(metrics.product_metrics(db, now=NOW))

        assert result["first_draft_approval_rate"]["rate"] == 0.0


class TestAdTestCostPerQualifiedLead:
    def _ad_test_card(self, db, thread_id="thr_1", brand_id="b1"):
        run(db["mi_listen_cards"].insert_one({
            "id": "c1", "brand_id": brand_id, "market": MARKET_KEY, "created_at": NOW - timedelta(days=1),
            "content_id": thread_id, "action": {"type": "ad_test"},
        }))

    def test_no_launched_campaign_is_unavailable(self):
        db = fresh_db()
        self._ad_test_card(db)
        result = run(metrics.product_metrics(db, now=NOW))
        assert "ad_test_cost_per_qualified_lead" in result["not_available"]
        assert "ad_test_cost_per_qualified_lead" not in result

    def test_a_launched_campaign_with_leads_computes_a_real_cost(self):
        db = fresh_db()
        self._ad_test_card(db, thread_id="thr_1", brand_id="b1")
        run(db["jane_ads_meta_campaigns"].insert_one(
            {"brand_id": "b1", "thread_id": "thr_1", "campaign_id": "camp_1"}))
        run(db["jane_ads_metric_snapshots"].insert_one(
            {"campaign_id": "camp_1", "level": "campaign", "date": "2026-10-20", "spend_ngn": 20000.0,
             "impressions": 1000, "reach": 900, "clicks": 50, "conversations": 5}))
        run(db["jane_ads_metric_snapshots"].insert_one(
            {"campaign_id": "camp_1", "level": "ad", "ad_id": "ad_1", "date": "2026-10-20", "spend_ngn": 20000.0,
             "impressions": 1000, "reach": 900, "clicks": 50, "conversations": 5}))
        run(db["inbox_conversations"].insert_one(
            {"workspace_id": "b1", "source_ad_id": "ad_1", "lead_stage": "qualified"}))
        run(db["inbox_conversations"].insert_one(
            {"workspace_id": "b1", "source_ad_id": "ad_1", "lead_stage": "new"}))

        result = run(metrics.product_metrics(db, now=NOW))

        at = result["ad_test_cost_per_qualified_lead"]
        assert at["campaigns_counted"] == 1
        assert at["total_spend_ngn"] == 20000.0
        assert at["total_leads"] == 2
        assert at["total_qualified_leads"] == 1
        assert at["cost_per_qualified_lead_ngn"] == 20000.0
        assert "ad_test_cost_per_qualified_lead" not in result.get("not_available", {})

    def test_leads_with_no_outcome_marked_is_unavailable_but_named(self):
        db = fresh_db()
        self._ad_test_card(db, thread_id="thr_1", brand_id="b1")
        run(db["jane_ads_meta_campaigns"].insert_one(
            {"brand_id": "b1", "thread_id": "thr_1", "campaign_id": "camp_1"}))
        run(db["jane_ads_metric_snapshots"].insert_one(
            {"campaign_id": "camp_1", "level": "campaign", "date": "2026-10-20", "spend_ngn": 5000.0}))
        run(db["jane_ads_metric_snapshots"].insert_one(
            {"campaign_id": "camp_1", "level": "ad", "ad_id": "ad_1", "date": "2026-10-20", "spend_ngn": 5000.0}))
        run(db["inbox_conversations"].insert_one({"workspace_id": "b1", "source_ad_id": "ad_1"}))

        result = run(metrics.product_metrics(db, now=NOW))

        at = result["ad_test_cost_per_qualified_lead"]
        assert at["total_qualified_leads"] == 0
        assert at["cost_per_qualified_lead_ngn"] is None
        assert at["note"]


class TestCategoryContentPerformance:
    CFG = {"learning": {"category_prior_min_samples": 3}}

    def _published_card(self, db, cid, category, signal_type, draft_ids, rates):
        run(db["mi_listen_cards"].insert_one({
            "id": cid, "brand_id": "b1", "market": f"{category}:NG", "type": signal_type,
            "created_at": NOW - timedelta(days=1), "created_draft_ids": draft_ids,
        }))
        for d, r in zip(draft_ids, rates):
            run(db["content_drafts"].insert_one({"id": d, "status": "published", "engagement_rate": r}))

    def test_a_group_below_the_minimum_is_withheld_entirely(self):
        db = fresh_db()
        self._published_card(db, "c1", "hair_wigs", "rising", ["d1", "d2"], [5.0, 6.0])  # only 2, min is 3
        result = run(metrics.category_content_performance(db, self.CFG))
        assert result == []

    def test_a_group_at_the_minimum_is_reported_de_identified(self):
        db = fresh_db()
        self._published_card(db, "c1", "hair_wigs", "rising", ["d1", "d2", "d3"], [4.0, 6.0, 8.0])
        result = run(metrics.category_content_performance(db, self.CFG))
        assert result == [{"category_id": "hair_wigs", "signal_type": "rising", "samples": 3,
                           "avg_engagement_rate": 6.0}]
        assert "brand_id" not in result[0] and "b1" not in str(result[0])  # genuinely de-identified

    def test_different_categories_and_types_do_not_mix(self):
        db = fresh_db()
        self._published_card(db, "c1", "hair_wigs", "rising", ["d1", "d2", "d3"], [10.0, 10.0, 10.0])
        self._published_card(db, "c2", "hair_wigs", "recurring_need", ["d4", "d5", "d6"], [2.0, 2.0, 2.0])
        self._published_card(db, "c3", "beauty_skincare", "rising", ["d7", "d8", "d9"], [5.0, 5.0, 5.0])
        result = run(metrics.category_content_performance(db, self.CFG))
        assert len(result) == 3
        assert result[0] == {"category_id": "hair_wigs", "signal_type": "rising", "samples": 3,
                             "avg_engagement_rate": 10.0}  # highest first

    def test_unpublished_drafts_with_no_engagement_rate_dont_count(self):
        db = fresh_db()
        run(db["mi_listen_cards"].insert_one({
            "id": "c1", "brand_id": "b1", "market": "hair_wigs:NG", "type": "rising",
            "created_at": NOW - timedelta(days=1), "created_draft_ids": ["d1", "d2", "d3"],
        }))
        run(db["content_drafts"].insert_one({"id": "d1", "status": "approved", "engagement_rate": None}))
        result = run(metrics.category_content_performance(db, self.CFG))
        assert result == []


class TestMetricsRouter:
    @pytest.mark.asyncio
    async def test_endpoint_delegates_correctly(self):
        db = fresh_db()
        await _card(db, "c1")
        await _action(db, "c1", "saved")
        result = await product_metrics_endpoint(market=None, window_days=30, admin=ADMIN, db=db)
        assert result["responseData"]["cards_shown"] == 1
        assert result["responseData"]["useful_or_acted_on_rate"] == 1.0
        assert result["responseData"]["category_content_performance"] == []  # nothing meets the minimum yet


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
