"""Ads Intelligence PRD P0: the standing brief and its plan checks, exactly-once
role-gated launches, verbatim-only testimonials and deferred recommendations."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.agents.jane_ads import brief as ab
from app.agents.jane_ads import launch_governance as gov
from app.agents.jane_ads.growth import engine
from app.agents.jane_ads.vsg01_orchestrator import _appears_in

from tests.test_jane_ads_growth import _seed_engine, fake, mongomock_motor, run  # noqa: F401


def _plan(pins=("Surulere",), city="Lagos", mode="own_radius", **over):
    return {"geo": {"mode": mode, "city": city, "pins": [{"name": p} for p in pins]},
            "objective": "engagement", "destination_type": "whatsapp", "creative": {"source": "upload"},
            "platforms": [{"budget_ngn": 14000, "days": 7}], **over}


REQ = {"budget_ngn": 14000}


def codes(checks):
    return {i["code"] for i in checks["issues"]}


def test_targets_outside_the_service_area_block_until_overridden():
    brief = {"service_area": ["Surulere", "Yaba"]}
    assert not ab.check_plan(_plan(city="Ibadan"), REQ, brief)["blocking"]
    far = ab.check_plan(_plan(pins=("Lekki",), city="Ibadan"), REQ, brief)
    assert codes(far) >= {"outside_service_area"}
    assert ab.unresolved_blocks(far, allow_out_of_area=False)
    assert not ab.unresolved_blocks(far, allow_out_of_area=True)
    nationwide = ab.check_plan(_plan(pins=(), city="", mode="non_local"), REQ, brief)
    assert "whole country" in nationwide["blocking"][0]["message"]


def test_a_whole_city_in_the_service_area_covers_its_pockets():
    assert not ab.check_plan(_plan(pins=("Ojuelegba",), city="Lagos"), REQ, {"service_area": ["Lagos"]})["blocking"]


def test_excluded_places_block_and_cannot_be_overridden():
    checks = ab.check_plan(_plan(pins=("Surulere", "Ikoyi")), REQ, {"excluded_locations": ["ikoyi"]})
    assert ab.unresolved_blocks(checks, allow_out_of_area=True)[0]["code"] == "excluded_location"


def test_constraints_hold_at_launch():
    brief = {"constraints": ["own_photos_only", "no_website"]}
    plan = _plan(creative={"source": "generate"}, destination_type="website")
    assert codes(ab.check_plan(plan, REQ, brief)) >= {"constraint_own_photos", "constraint_no_website"}
    assert not ab.check_plan(_plan(), REQ, brief)["blocking"]


def test_proxy_optimisation_is_named():
    sales_site = _plan(objective="sales", destination_type="website")
    assert "proxy_optimisation" in codes(ab.check_plan(sales_site, REQ, {"tracking": "none"}))
    assert "proxy_optimisation" not in codes(ab.check_plan(sales_site, REQ, {"tracking": "website_purchases"}))
    assert "proxy_optimisation" in codes(ab.check_plan(_plan(objective="leads"), REQ, {}))


def test_structure_and_split_budget_warning():
    one = ab.check_plan(_plan(), REQ, {})["structure"]
    assert (one["campaigns"], one["ad_sets"], one["ads_per_ad_set"]) == (1, 1, 1) and one["daily_ngn"] == 2000
    three = ab.check_plan(_plan(), REQ, {}, sibling_plans=3)
    assert {"split_budget", "budget_too_thin"} <= codes(three)


def test_missing_brief_lowers_confidence_and_costs_are_separated():
    assert ab.check_plan(_plan(), REQ, {})["confidence"] == "lower"
    c = ab.costs({"budget_ngn": 10000}, 11500)
    assert (c["ad_spend_ngn"], c["uri_fee_ngn"], c["total_ngn"]) == (10000, 1500, 11500)


def test_brief_saves_only_known_fields():
    db = mongomock_motor.AsyncMongoMockClient()["brief"]
    saved = run(ab.save(db, "b1", {"service_area": "Surulere, Yaba", "constraints": ["no_ai_video", "bogus"],
                                    "tracking": "nonsense", "success_definition": "Orders over ₦5k"}, "u1"))
    assert saved["service_area"] == ["Surulere", "Yaba"] and saved["constraints"] == ["no_ai_video"]
    assert saved["tracking"] == "none" and saved["success_definition"] == "Orders over ₦5k"


def _pending(db, plan_id="p1"):
    run(db[gov.PLANS].insert_one({"plan_id": plan_id, "brand_id": "b1", "status": "pending",
                                  "plan": {"x": 1}, "req": {"budget_ngn": 1}}))


def test_a_plan_is_claimed_exactly_once_and_released_on_failure():
    db = mongomock_motor.AsyncMongoMockClient()["gov"]
    _pending(db)
    assert run(gov.claim(db, "p1", "u1"))["status"] == "launching"
    assert run(gov.claim(db, "p1", "u2")) is None
    run(gov.release(db, "p1", "u1", "Meta said no"))
    doc = run(db[gov.PLANS].find_one({"plan_id": "p1"}))
    assert doc["status"] == "pending" and doc["last_launch_error"] == "Meta said no"
    assert [h["state"] for h in doc["state_history"]] == ["approved", "failed"]
    assert run(gov.claim(db, "p1", "u1"))
    run(gov.mark_launched(db, "p1", "u1", "c9"))
    assert run(gov.claim(db, "p1", "u1")) is None


def test_the_approval_records_who_and_what():
    db = mongomock_motor.AsyncMongoMockClient()["gov"]
    _pending(db)
    doc = run(db[gov.PLANS].find_one({"plan_id": "p1"}))
    h = run(gov.record_approval(db, doc, "u1", 11500))
    a = run(db[gov.APPROVALS].find_one({"plan_id": "p1"}))
    assert a["approved_by"] == "u1" and a["config_hash"] == h and a["total_due_ngn"] == 11500
    assert gov.config_hash({"x": 2}, {"budget_ngn": 1}) != h


def test_only_agency_admins_launch(monkeypatch):
    from app.services.AgencyService import AgencyService

    async def is_admin(user_id, agency_id, db):
        return user_id == "admin"
    monkeypatch.setattr(AgencyService, "is_agency_admin", staticmethod(is_admin))
    run(gov.require_launch_role(None, {"user_id": "anyone"}))
    run(gov.require_launch_role(None, {"user_id": "admin", "agency_id": "a1"}))
    with pytest.raises(HTTPException) as e:
        run(gov.require_launch_role(None, {"user_id": "agent", "agency_id": "a1"}))
    assert e.value.status_code == 403


def test_a_quote_must_be_in_the_business_text_word_for_word():
    text = 'Ngozi A. wrote: “Best jollof in Surulere!” and we love her.'
    assert _appears_in("Best jollof in Surulere", text) and _appears_in("Ngozi A.", text)
    assert not _appears_in("Best jollof in Lagos", text) and not _appears_in("", text)


def test_a_deferred_suggestion_returns_when_its_wait_is_over(fake, monkeypatch):  # noqa: F811
    db = mongomock_motor.AsyncMongoMockClient()["growth"]
    record = _seed_engine(db, fake, monkeypatch)
    run(engine.run_campaign(db, fake, record))
    rec = run(db[engine.RECOMMENDATIONS].find_one({"action_type": "pause_ad"}))
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    run(db[engine.RECOMMENDATIONS].update_one({"id": rec["id"]},
                                              {"$set": {"status": "deferred", "deferred_until": past}}))
    run(engine.run_campaign(db, fake, record))
    assert run(db[engine.RECOMMENDATIONS].find_one({"id": rec["id"]}))["status"] == "proposed"
