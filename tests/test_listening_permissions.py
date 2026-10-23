"""
Per-source permission records (PRD §15): "each connector has a permission
record... approved before production use." Confirms this is enforced, not
just documented — collect_market() refuses an unapproved source, and
evidence samples respect the record's own display_rules (excerpt length,
requiring a link back to the original post). Uses an in-memory MongoDB;
no network, no AI.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from fastapi import HTTPException

from app.agents.market_intelligence.listening import permissions
from app.agents.market_intelligence.listening.router import list_source_permissions, set_source_approval

NOW = datetime(2026, 10, 21, 12, 0)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["permissions_test"]


ADMIN = {"claims": {"email": "admin@urisocial.com"}}


class TestSeedAndLookup:
    def test_x_and_x_pro_ship_pre_approved(self):
        db = fresh_db()
        run(permissions.ensure_seeded(db))
        assert run(permissions.is_approved(db, "x")) is True
        assert run(permissions.is_approved(db, "x_pro")) is True

    def test_an_unknown_source_is_not_approved(self):
        db = fresh_db()
        run(permissions.ensure_seeded(db))
        assert run(permissions.is_approved(db, "tiktok")) is False

    def test_ensure_seeded_never_overwrites_an_existing_record(self):
        db = fresh_db()
        run(db["mi_source_permissions"].insert_one({"source": "x", "approved": False, "approved_by": "ops-decision"}))
        run(permissions.ensure_seeded(db))
        doc = run(permissions.get(db, "x"))
        assert doc["approved"] is False and doc["approved_by"] == "ops-decision"

    def test_display_rules_falls_back_to_the_default_for_an_unknown_source(self):
        db = fresh_db()
        assert run(permissions.display_rules(db, "nothing-here")) == permissions.DEFAULT_DISPLAY_RULES


class TestSetApproval:
    def test_revoking_an_approved_source_persists(self):
        db = fresh_db()
        run(permissions.ensure_seeded(db))
        result = run(permissions.set_approval(db, "x", False, "legal@urisocial.com", notes="Terms changed"))
        assert result["approved"] is False
        assert run(permissions.is_approved(db, "x")) is False

    def test_approving_a_brand_new_source_creates_the_record(self):
        db = fresh_db()
        result = run(permissions.set_approval(db, "tiktok", True, "ops@urisocial.com"))
        assert result["approved"] is True
        assert run(permissions.is_approved(db, "tiktok")) is True


class TestCollectionGate:
    def test_collect_market_refuses_an_unapproved_source(self):
        from app.agents.market_intelligence.listening import collector
        db = fresh_db()
        market = {"key": "hair_wigs:NG", "category_id": "hair_wigs", "country": "NG"}
        cfg = {"source": "tiktok", "monthly_budget_usd": 100}

        class _Adapter:
            pass  # never called if the gate works

        res = run(collector.collect_market(db, market, cfg, _Adapter(), "tiktok", NOW))
        assert res["status"] == "blocked_unapproved_source"


class TestEvidenceDisplayRules:
    def test_excerpt_is_truncated_to_the_sources_configured_length(self):
        from app.agents.market_intelligence.listening import detect

        db = fresh_db()
        run(permissions.set_approval(db, "x", True, "ops", notes=None))
        run(db["mi_source_permissions"].update_one(
            {"source": "x"}, {"$set": {"display_rules": {"max_excerpt_chars": 10, "require_link": True}}}))
        run(db["mi_listen_readings"].insert_one({
            "source_id": "s1", "market": "hair_wigs:NG", "topics": ["latasha wig"], "on_topic": True,
            "measured": True, "intent": "purchase", "buying_intent": "high", "confidence": 0.9,
            "author": "@a", "published_at": NOW - timedelta(minutes=30),
        }))
        run(db["mi_listen_items"].insert_one({
            "source_id": "s1", "url": "https://x.com/s1", "text": "this text is definitely longer than ten chars",
            "raw_metrics": {"likes": 1}, "published_at": NOW - timedelta(minutes=30), "author_handle": "@a",
        }))
        cfg = {"source": "x", "score": {"card_ttl_days": 7}}

        sig = run(detect._evidence(db, {"key": "hair_wigs:NG", "label": "Hair & wigs"}, "latasha wig", "rising",
                                    NOW - timedelta(hours=1), NOW, cfg))

        assert len(sig["samples"][0]["text"]) == 10

    def test_samples_with_no_link_are_dropped_when_a_link_is_required(self):
        from app.agents.market_intelligence.listening import detect

        db = fresh_db()
        run(permissions.set_approval(db, "x", True, "ops"))
        run(db["mi_listen_readings"].insert_one({
            "source_id": "no-url", "market": "hair_wigs:NG", "topics": ["latasha wig"], "on_topic": True,
            "measured": True, "intent": "purchase", "buying_intent": "high", "confidence": 0.9,
            "author": "@a", "published_at": NOW - timedelta(minutes=30),
        }))
        run(db["mi_listen_items"].insert_one({
            "source_id": "no-url", "url": None, "text": "no link on this one",
            "raw_metrics": {}, "published_at": NOW - timedelta(minutes=30), "author_handle": "@a",
        }))
        cfg = {"source": "x", "score": {"card_ttl_days": 7}}

        sig = run(detect._evidence(db, {"key": "hair_wigs:NG", "label": "Hair & wigs"}, "latasha wig", "rising",
                                    NOW - timedelta(hours=1), NOW, cfg))

        assert sig["samples"] == []
        assert sig["posts"] == 1  # the reading itself still counts; only the quoted sample is withheld


class TestRouterEndpoints:
    @pytest.mark.asyncio
    async def test_list_source_permissions_seeds_and_returns_x(self):
        db = fresh_db()
        result = await list_source_permissions(admin=ADMIN, db=db)
        sources = {s["source"] for s in result["responseData"]}
        assert {"x", "x_pro"} <= sources

    @pytest.mark.asyncio
    async def test_set_source_approval_records_the_admins_email(self):
        db = fresh_db()
        await permissions.ensure_seeded(db)

        class _Body:
            approved, notes = False, "Pausing while we review terms"
        result = await set_source_approval("x", _Body(), admin=ADMIN, db=db)

        assert result["responseData"]["approved"] is False
        assert result["responseData"]["approved_by"] == "admin@urisocial.com"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
