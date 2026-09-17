"""
Uri Market Intelligence — business context (PRD §7) tests: real fulfilment
facts actually reach relevance scoring, and the "Check suitability" vs
"Ready to act" label is derived from that, never guessed independently.
"""
import asyncio

import pytest

from app.agents.market_intelligence.models import ActionReadiness, ComponentScore, ScoreBreakdown
from app.agents.market_intelligence.scan_runner import _action_readiness_from, _fetch_business_context


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _relevance_with_feasibility(points: int) -> ScoreBreakdown:
    components = [
        ComponentScore(name="product_fit", points=1, reason="x"),
        ComponentScore(name="served_geography", points=1, reason="x"),
        ComponentScore(name="customer_need_fit", points=1, reason="x"),
        ComponentScore(name="fulfilment_feasibility", points=points, reason="x"),
        ComponentScore(name="alignment_with_goal", points=1, reason="x"),
    ]
    total = sum(c.points for c in components)
    return ScoreBreakdown(components=components, total=total, band=ScoreBreakdown.band_for(total))


def test_check_suitability_when_fulfilment_unknown():
    relevance = _relevance_with_feasibility(0)
    assert _action_readiness_from(relevance) == ActionReadiness.CHECK_SUITABILITY


def test_ready_to_act_when_fulfilment_known():
    relevance = _relevance_with_feasibility(1)
    assert _action_readiness_from(relevance) == ActionReadiness.READY_TO_ACT


def test_check_suitability_when_component_missing_entirely():
    components = [ComponentScore(name="product_fit", points=2, reason="x")]
    relevance = ScoreBreakdown(components=components, total=2, band=ScoreBreakdown.band_for(2))
    assert _action_readiness_from(relevance) == ActionReadiness.CHECK_SUITABILITY


# ── _fetch_business_context reads the real new fields ────────────────────────

class FakeGetResult(dict):
    pass


def test_fetch_business_context_reads_fulfilment_fields(monkeypatch):
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService

    async def fake_get(user_id, db, brand_id=None):
        return {
            "status": True,
            "responseData": {
                "brand_name": "Test Brand", "industry": "retail", "key_products_services": ["shoes"],
                "primary_goal": "grow sales", "region": "Lagos",
                "stock_availability": "in_stock", "delivery_capability": "same-day in Lagos",
                "lead_time": "2-3 days", "budget_ceiling": 50000.0, "margin_band": "medium",
            },
        }

    monkeypatch.setattr(BrandProfileService, "get", staticmethod(fake_get))
    context = _run(_fetch_business_context(db=None, user_id="u1", brand_id="b1"))

    assert context["stock_availability"] == "in_stock"
    assert context["delivery_capability"] == "same-day in Lagos"
    assert context["lead_time"] == "2-3 days"
    assert context["budget_ceiling"] == 50000.0
    assert context["margin_band"] == "medium"
    assert context["service_locations"] == ["Lagos"]


def test_fetch_business_context_treats_empty_strings_as_unknown(monkeypatch):
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService

    async def fake_get(user_id, db, brand_id=None):
        return {
            "status": True,
            "responseData": {
                "brand_name": "Test Brand", "stock_availability": "", "delivery_capability": "",
                "lead_time": "", "budget_ceiling": None, "margin_band": "", "region": "",
            },
        }

    monkeypatch.setattr(BrandProfileService, "get", staticmethod(fake_get))
    context = _run(_fetch_business_context(db=None, user_id="u1", brand_id="b1"))

    assert context["stock_availability"] is None
    assert context["delivery_capability"] is None
    assert context["service_locations"] is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
