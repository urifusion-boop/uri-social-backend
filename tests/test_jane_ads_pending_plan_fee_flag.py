"""
The launch step rebuilds from the stored pending plan, not from the in-memory build, so
whether a plan was quoted fee-free has to be written into that stored plan. Live-caught
on prod 2026-10-27: a TikTok plan quoted ₦31,000 ("from your wallet") was charged
₦34,444.44 (31,000 / 0.9) at launch because the stored plan carried no `fee_bypassed`
and launch read it back as False.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.agents.jane_ads import router as R

BRAND = {"brand_id": "brand-1", "user_id": "user-1"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Dump:
    def model_dump(self, mode="json"):
        return {}


class _Coll:
    def __init__(self):
        self.inserted = []

    async def find_one(self, *a, **kw):
        return None

    async def insert_one(self, doc):
        self.inserted.append(doc)


class _Db:
    def __init__(self):
        self.pending = _Coll()

    def __getitem__(self, name):
        return self.pending if name == "jane_ads_pending_plans" else _Coll()


def _stored_plan_for(monkeypatch, fee_bypassed):
    built = SimpleNamespace(
        business_id="b1", req=SimpleNamespace(budget_ngn=31_000.0, model_dump=lambda mode="json": {}),
        plan=_Dump(), jane_platforms=["tiktok"], forced_to_meta=False, geo_dump=None,
        understood={}, budget_estimate=None, summary=None, thread_id="", variant_group_id=None,
        selected_plan_variant=None, fee_bypassed=fee_bypassed,
    )

    async def _build(*a, **kw):
        return built

    async def _status(*a, **kw):
        return 100_000.0, True

    monkeypatch.setattr(R, "_build_campaign_plan", _build)
    monkeypatch.setattr(R, "_wallet_status", _status)
    monkeypatch.setattr(R, "_plan_response_dict", lambda b: {})

    db = _Db()
    body = R.MetaLaunchFromMessageBody(message="run a TikTok ad", preferred_platform="tiktok" if fee_bypassed else "")
    out = _run(R.meta_plan_from_message(body, db, BRAND))
    assert out["stage"] == "planned"
    assert len(db.pending.inserted) == 1
    return db.pending.inserted[0]


def test_a_fee_free_tiktok_plan_is_stored_fee_free(monkeypatch):
    doc = _stored_plan_for(monkeypatch, fee_bypassed=True)
    assert doc["fee_bypassed"] is True


def test_a_meta_plan_is_stored_with_the_fee_still_applying(monkeypatch):
    doc = _stored_plan_for(monkeypatch, fee_bypassed=False)
    assert doc["fee_bypassed"] is False
