"""A plan whose launch request died mid-flight (server restart) must not stay
"launching" forever — live 2026-11-12: every relaunch said "already being launched"."""
import asyncio
from datetime import datetime, timedelta, timezone

from app.agents.jane_ads.router import _release_if_stale_launch


class _Coll:
    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, q):
        return dict(self.doc) if self.doc["plan_id"] == q["plan_id"] else None

    async def update_one(self, q, u):
        if self.doc["plan_id"] == q["plan_id"] and self.doc["status"] == q.get("status", self.doc["status"]):
            self.doc.update(u.get("$set", {}))


class _DB(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, "c")


def _db(doc):
    d = _DB(); d["c"] = _Coll(doc); return d


def test_old_launching_claim_is_released_to_pending():
    doc = {"plan_id": "p1", "status": "launching", "launching_at": datetime.now(timezone.utc) - timedelta(hours=1)}
    db = _db(doc)
    out = asyncio.run(_release_if_stale_launch(db, dict(doc)))
    assert out["status"] == "pending"
    assert "interrupted" in out["last_launch_error"]


def test_recent_launching_claim_is_left_alone():
    doc = {"plan_id": "p1", "status": "launching", "launching_at": datetime.now(timezone.utc) - timedelta(minutes=3)}
    out = asyncio.run(_release_if_stale_launch(_db(doc), dict(doc)))
    assert out["status"] == "launching"


def test_naive_timestamp_from_mongo_is_handled():
    doc = {"plan_id": "p1", "status": "launching", "launching_at": datetime.utcnow() - timedelta(hours=2)}
    assert asyncio.run(_release_if_stale_launch(_db(doc), dict(doc)))["status"] == "pending"


def test_other_states_untouched():
    doc = {"plan_id": "p1", "status": "launched"}
    assert asyncio.run(_release_if_stale_launch(_db(doc), dict(doc)))["status"] == "launched"


def test_status_endpoint_reports_launched_plan():
    from app.agents.jane_ads.router import meta_plan_status
    doc = {"plan_id": "p1", "status": "launched", "brand_id": "b1", "campaign_id": "188",
           "plan": {"platforms": [{"platform": "tiktok"}]}}
    out = asyncio.run(meta_plan_status("p1", db=_db(doc), brand_ctx={"brand_id": "b1"}))
    assert out == {"status": "launched", "campaign_id": "188", "platform": "tiktok", "last_launch_error": ""}


def test_status_endpoint_hides_other_brands_plan():
    import pytest
    from fastapi import HTTPException
    from app.agents.jane_ads.router import meta_plan_status
    doc = {"plan_id": "p1", "status": "pending", "brand_id": "b1"}
    with pytest.raises(HTTPException) as e:
        asyncio.run(meta_plan_status("p1", db=_db(doc), brand_ctx={"brand_id": "other"}))
    assert e.value.status_code == 404
