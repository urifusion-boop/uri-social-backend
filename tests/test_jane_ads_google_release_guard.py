"""
Change account / Disconnect while URI campaigns are still in that Google Ads
account: once URI lets go it can no longer pause, track or bill them, so the
owner is told first and chooses — pause them, keep them running, or cancel.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.agents.jane_ads import google_flow as gf
from app.agents.jane_ads import router as R


def run(coro):
    return asyncio.run(coro)


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return [dict(d) for d in self.docs]


class Coll:
    def __init__(self, docs):
        self.docs = docs

    def _match(self, d, q):
        return all(d.get(k) == v for k, v in q.items())

    def find(self, q, proj=None):
        return Cursor([d for d in self.docs if self._match(d, q)])

    async def find_one(self, q, *a, **kw):
        return next((dict(d) for d in self.docs if self._match(d, q)), None)

    async def update_many(self, q, update):
        for d in self.docs:
            if self._match(d, q):
                d.update(update["$set"])

    async def delete_one(self, q):
        self.docs[:] = [d for d in self.docs if not self._match(d, q)]


class Db:
    def __init__(self, docs):
        self.coll = Coll(docs)

    def __getitem__(self, name):
        return self.coll


def campaign(cid, customer="4052658060", brand="b1", **kw):
    return {"campaign_id": cid, "brand_id": brand, "customer_id": customer, "platform": "google",
            "display_name": f"Campaign {cid}", **kw}


CONN = {"customer_id": "4052658060", "manager_link_status": "active"}
BRAND = {"user_id": "u1", "brand_id": "b1"}


def deliveries(mapping):
    async def summary(self, cid):
        if mapping[cid] is Exception:
            raise gf.GoogleAdsAPIError("unreachable")
        return {"delivery": mapping[cid]}
    return patch.object(gf.GoogleCampaigns, "fetch_campaign_summary", summary)


class TestWhatsStillInTheAccount:
    def test_only_this_brands_campaigns_in_this_account(self):
        db = Db([campaign("1"), campaign("2", customer="999"), campaign("3", brand="b2"),
                 campaign("4", detached_at="x")])
        with deliveries({"1": "Active"}):
            out = run(gf.campaigns_in_account(db, "b1", "4052658060"))
        assert out == [{"campaign_id": "1", "name": "Campaign 1", "delivery": "Active"}]

    def test_removed_ones_dont_count_and_unreadable_ones_do(self):
        db = Db([campaign("1"), campaign("2")])
        with deliveries({"1": "Deleted", "2": Exception}):
            out = run(gf.campaigns_in_account(db, "b1", "4052658060"))
        assert [(c["campaign_id"], c["delivery"]) for c in out] == [("2", "Unknown")]


class TestReleaseAsksFirst:
    def _release(self, db, **kw):
        released = AsyncMock(return_value={"released": True, "removed": kw.get("remove", False), "google_note": ""})
        with patch("app.agents.jane_ads.google_ads_connection.get_any_google_ads_connection",
                   AsyncMock(return_value=CONN)), \
                patch("app.agents.jane_ads.google_ads_connection.release_account", released):
            out = run(R._release_google_ads(db, BRAND, remove=kw.pop("remove", False), **kw))
        return out, released

    def test_campaigns_in_the_account_stop_it_and_are_listed(self):
        db = Db([campaign("1"), campaign("2")])
        with deliveries({"1": "Active", "2": "Paused"}), pytest.raises(HTTPException) as e:
            self._release(db)
        detail = e.value.detail
        assert e.value.status_code == 409 and detail["code"] == "google_campaigns_in_account"
        assert detail["running"] == 1 and len(detail["campaigns"]) == 2
        assert "2 URI campaigns are still" in detail["message"] and "(1 running)" in detail["message"]
        assert not any(d.get("detached_at") for d in db.coll.docs)

    def test_pause_first_pauses_the_running_ones_then_lets_go(self):
        db = Db([campaign("1"), campaign("2")])
        paused = AsyncMock(return_value={})
        with deliveries({"1": "Active", "2": "Paused"}), \
                patch.object(gf.GoogleCampaigns, "set_delivery", paused):
            out, released = self._release(db, pause_campaigns=True, remove=True)
        assert paused.await_args_list[0].args == ("1", False) and paused.await_count == 1
        assert out["campaigns_paused"] == 1 and out["campaigns_left_running"] == 0
        released.assert_awaited_once()
        assert all(d.get("detached_at") for d in db.coll.docs)

    def test_if_one_cant_be_paused_nothing_is_let_go_of(self):
        db = Db([campaign("1")])
        with deliveries({"1": "Active"}), \
                patch.object(gf.GoogleCampaigns, "set_delivery", AsyncMock(side_effect=gf.GoogleAdsAPIError("no"))), \
                pytest.raises(HTTPException) as e:
            self._release(db, pause_campaigns=True)
        assert e.value.status_code == 502
        assert not db.coll.docs[0].get("detached_at")

    def test_keep_running_lets_go_and_says_how_many_are_left(self):
        db = Db([campaign("1")])
        with deliveries({"1": "Active"}):
            out, released = self._release(db, keep_running=True)
        assert out["campaigns_left_running"] == 1 and out["campaigns_paused"] == 0
        released.assert_awaited_once()

    def test_no_campaigns_no_question(self):
        out, released = self._release(Db([]))
        assert out["released"] and out["campaigns_left_running"] == 0
        released.assert_awaited_once()


class TestAfterLettingGo:
    def test_uri_stops_reaching_for_a_detached_campaign(self):
        db = Db([campaign("1", detached_at="x")])
        with pytest.raises(gf.GoogleAdsAPIError) as e:
            run(gf.GoogleCampaigns(db)._adapter("1"))
        assert "no longer has access" in str(e.value)
