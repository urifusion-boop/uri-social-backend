"""
Plan-tier limits (PRD §5's "Operating policy: Plan tier... Scheduler
priority, delivery policy"; §21 open decision #8). An agency-owned brand's
cadence/alert-budget follow its agency's plan_tier; a solo user's own
subscription_tier otherwise; everything falls back to config.py's
plan_tiers["default"] rather than guessing. Every named tier starts equal to
the single global values that existed before this, so nothing changes
behaviour until a tier is deliberately differentiated. Uses an in-memory
MongoDB; no network, no AI — the Agency/CreditService lookups are stubbed.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import cycle, markets, profile as profile_mod
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge

NOW = datetime(2026, 10, 21, 12, 0)
CFG = _merge(DEFAULTS, {"plan_tiers": {
    "default": {"cadence_hours": 12, "max_alerts_per_week": 3, "history_days": 90},
    "starter": {"cadence_hours": 12, "max_alerts_per_week": 1, "history_days": 90},
    "pro": {"cadence_hours": 2, "max_alerts_per_week": 10, "history_days": 90},
    "agency_starter": {"cadence_hours": 12, "max_alerts_per_week": 3, "history_days": 90},
}})


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["plan_tier_test"]


class _Agency:
    def __init__(self, plan_tier):
        self.plan_tier = plan_tier


class _Wallet:
    def __init__(self, subscription_tier):
        self.subscription_tier = subscription_tier


# ── profile.effective_plan_tier ──────────────────────────────────────────────

class TestEffectivePlanTier:
    def test_agency_owned_brand_uses_the_agencys_plan_tier(self):
        db = fresh_db()
        with patch("app.services.AgencyService.AgencyService.get_agency_for_brand",
                  new=AsyncMock(return_value=_Agency("agency_starter"))):
            tier = run(profile_mod.effective_plan_tier(db, "u1", "b1"))
        assert tier == "agency_starter"

    def test_solo_user_falls_back_to_their_own_subscription_tier(self):
        db = fresh_db()
        with patch("app.services.AgencyService.AgencyService.get_agency_for_brand",
                  new=AsyncMock(return_value=None)), \
             patch("app.services.CreditService.CreditService.get_user_wallet",
                  new=AsyncMock(return_value=_Wallet("pro"))):
            tier = run(profile_mod.effective_plan_tier(db, "u1", "b1"))
        assert tier == "pro"

    def test_neither_answering_falls_back_to_default(self):
        db = fresh_db()
        with patch("app.services.AgencyService.AgencyService.get_agency_for_brand",
                  new=AsyncMock(return_value=None)), \
             patch("app.services.CreditService.CreditService.get_user_wallet",
                  new=AsyncMock(return_value=None)):
            tier = run(profile_mod.effective_plan_tier(db, "u1", "b1"))
        assert tier == profile_mod.PLAN_TIER_FALLBACK

    def test_a_failing_lookup_falls_back_rather_than_raising(self):
        db = fresh_db()
        with patch("app.services.AgencyService.AgencyService.get_agency_for_brand",
                  new=AsyncMock(side_effect=RuntimeError("db down"))), \
             patch("app.services.CreditService.CreditService.get_user_wallet",
                  new=AsyncMock(side_effect=RuntimeError("db down"))):
            tier = run(profile_mod.effective_plan_tier(db, "u1", "b1"))
        assert tier == profile_mod.PLAN_TIER_FALLBACK


# ── cycle.py wiring: cadence floor + alert budget by resolved tier ──────────

class TestCycleUsesResolvedTier:
    def test_market_cadence_floors_to_its_fastest_subscribers_tier(self):
        db = fresh_db()
        run(db["mi_markets"].insert_one({
            "key": "hair_wigs:NG", "category_id": "hair_wigs", "country": "NG", "status": "active",
            "subscribers": ["b_pro"], "learned_terms": [],
        }))
        profiles_by_brand = {"b_pro": {"brand_id": "b_pro", "user_id": "u_pro"}}
        with patch("app.agents.market_intelligence.listening.profile.effective_plan_tier",
                  new=AsyncMock(return_value="pro")):
            floor = run(cycle._market_tier_floor_hours(
                db, CFG, {"key": "hair_wigs:NG", "subscribers": ["b_pro"]}, profiles_by_brand, {}))
        assert floor == 2  # CFG's "pro" tier cadence_hours

    def test_the_fastest_of_several_subscribers_tiers_wins(self):
        db = fresh_db()
        profiles_by_brand = {
            "b_starter": {"brand_id": "b_starter", "user_id": "u1"},
            "b_pro": {"brand_id": "b_pro", "user_id": "u2"},
        }

        async def fake_tier(db_, user_id, brand_id):
            return "pro" if brand_id == "b_pro" else "starter"

        with patch("app.agents.market_intelligence.listening.profile.effective_plan_tier", new=fake_tier):
            floor = run(cycle._market_tier_floor_hours(
                db, CFG, {"key": "k", "subscribers": ["b_starter", "b_pro"]}, profiles_by_brand, {}))
        assert floor == 2  # pro's 2h beats starter's 12h

    def test_a_subscriber_with_no_loaded_profile_is_skipped_not_fatal(self):
        db = fresh_db()
        floor = run(cycle._market_tier_floor_hours(
            db, CFG, {"key": "k", "subscribers": ["ghost_brand"]}, {}, {}))
        assert floor is None

    def test_tier_lookups_are_cached_once_per_brand_per_cycle(self):
        db = fresh_db()
        calls = []

        async def fake_tier(db_, user_id, brand_id):
            calls.append(brand_id)
            return "starter"

        profiles_by_brand = {"b1": {"brand_id": "b1", "user_id": "u1"}}
        cache: dict = {}
        with patch("app.agents.market_intelligence.listening.profile.effective_plan_tier", new=fake_tier):
            run(cycle._market_tier_floor_hours(db, CFG, {"key": "k1", "subscribers": ["b1"]}, profiles_by_brand, cache))
            run(cycle._market_tier_floor_hours(db, CFG, {"key": "k2", "subscribers": ["b1"]}, profiles_by_brand, cache))
        assert calls == ["b1"]  # second market's lookup for the same brand hit the cache


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
