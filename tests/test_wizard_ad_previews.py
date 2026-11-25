"""
The campaign wizard's Ads step (jane_ads/ad_previews.py): the ads before the plan.

Image generation is mocked; the rest is real. Asking checks the balance first; the job makes
each ad the way the plan makes one (the plan's angles, or the owner's style for a single ad),
keeps it in Creatives, then charges for it; a retry carries on; the owner can change the words,
remove one or make one again; and the plan runs exactly these ads without making or charging
anything again.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.agents.jane_ads import ad_previews as P
from app.agents.jane_ads.models import AdCreative
from app.jobs import queue
from app.jobs.worker import JobError


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _db():
    return AsyncMongoMockClient()["test_ad_previews"]


class _Ctx:
    async def step(self, *a, **k):
        pass


class _Credits:
    def __init__(self, balance=100):
        self.balance, self.charged = balance, []

    async def check_sufficient_credits(self, user_id, required=1):
        return self.balance >= required

    async def deduct_credit(self, user_id, amount=1, **kw):
        self.charged.append(amount)

    async def get_user_wallet(self, user_id):
        return SimpleNamespace(credits_remaining=self.balance)


INPUTS = P.clean_inputs(description="Airport runs (from ₦25,000) — for frequent flyers in Lekki", city="Lagos",
                        objective="leads", destination_type="website", budget_ngn=40_000, vsg01_format_id="SEED-100")


def _start(db, credits, count=3, inputs=None):
    with patch("app.services.CreditService.credit_service", new=credits):
        return _run(P.start(db, brand_id="b1", user_id="u1", count=count, inputs=inputs or INPUTS))


def _work(db, credits, make, job=None):
    """Claim the next job and run it with `make` standing in for Jane's ad maker."""
    job = job or _run(queue.claim(db, [P.JOB]))
    with patch("app.services.CreditService.credit_service", new=credits), \
         patch("app.agents.jane_ads.creative.generate_ad_creative", new=make), \
         patch("app.agents.jane_ads.destination.get_brand_destination",
               new=AsyncMock(return_value={"destination_type": "whatsapp", "destination_cta": ""})), \
         patch.object(P, "_profile", new=AsyncMock(return_value={"brand_name": "Magen", "industry": "Transport"})):
        _run(P.preview_job(db, job, _Ctx()))
    return job


def _maker(calls, fail_at=None):
    async def make(business_name, category, goal, description, **kw):
        calls.append({"business": business_name, "goal": goal, "brief": description, **kw})
        if fail_at is not None and len(calls) == fail_at:
            raise RuntimeError("image model down")
        n = len(calls)
        return AdCreative(image_url=f"https://cdn.uri/ad{n}.png", headline=f"Ad {n}", primary_text=f"Text {n}.",
                          vsg01_format_id=kw.get("vsg01_format_id") or "")
    return make


def test_the_inputs_are_tidied_and_a_blank_ask_is_refused():
    assert INPUTS["goal"] == "leads" and INPUTS["destination_type"] == "website"
    assert P.clean_inputs(description="x", objective="engagement", destination_type="fax")["destination_type"] == ""
    assert P.clean_inputs(description="x", objective="engagement")["goal"] == "followers"
    with pytest.raises(ValueError):
        P.clean_inputs(description="   ")


def test_asking_checks_the_balance_covers_every_ad_first():
    db = _db()
    with pytest.raises(PermissionError, match="takes 3 content credits"):
        _start(db, _Credits(balance=2), count=3)
    assert _run(db[P.PREVIEWS].count_documents({})) == 0
    out = _start(db, _Credits(balance=6), count=9)        # capped at the most a campaign can test
    assert out["count"] == P.MAX_ADS and out["status"] == "running"


def test_several_ads_are_made_like_the_plan_makes_them_kept_and_charged_one_by_one():
    db, credits, calls = _db(), _Credits(), []
    rec = _start(db, credits, count=3)
    _work(db, credits, _maker(calls))
    out = _run(P.get(db, "b1", rec["id"]))
    assert out["status"] == "done" and [a["headline"] for a in out["results"]] == ["Ad 1", "Ad 2", "Ad 3"]
    # The plan's own angles, in the plan's order, each its own layout; a style is Jane's call here.
    assert [a["angle"] for a in out["results"]] == ["problem", "offer", "outcome"]
    assert len({c["image_layout"] for c in calls}) == 3 and all(c["vsg01_format_id"] is None for c in calls)
    # The Audience step's words and the Goal step's answers reach the ad maker; the budget doesn't.
    assert calls[0]["business"] == "Magen" and calls[0]["goal"] == "leads" and "Airport runs" in calls[0]["brief"]
    assert calls[0]["destination_type"] == "website" and calls[0]["city"] == "Lagos"
    assert "40,000" not in calls[0]["brief"] and "40000" not in calls[0]["brief"]
    assert credits.charged == [1, 1, 1]
    drafts = _run(db["content_drafts"].find({"preview_id": rec["id"]}).to_list(10))
    assert len(drafts) == 3 and {d["content_source"] for d in drafts} == {P.DRAFT_SOURCE}


def test_one_ad_follows_the_owners_style():
    db, credits, calls = _db(), _Credits(), []
    rec = _start(db, credits, count=1)
    _work(db, credits, _maker(calls))
    assert calls[0]["vsg01_format_id"] == "SEED-100" and calls[0]["image_layout"] == ""
    assert _run(P.get(db, "b1", rec["id"]))["results"][0]["angle"] == ""


def test_a_failure_charges_nothing_and_the_retry_carries_on():
    db, credits, calls = _db(), _Credits(), []
    rec = _start(db, credits, count=3)
    with pytest.raises(JobError):
        _work(db, credits, _maker(calls, fail_at=2))
    mid = _run(P.get(db, "b1", rec["id"]))
    assert mid["status"] == "running" and len(mid["results"]) == 1 and credits.charged == [1]
    job = _run(db["jobs"].find_one({})) or {}
    _work(db, credits, _maker(calls), job={**job, "payload": {"preview_id": rec["id"]}, "attempts": 2})
    out = _run(P.get(db, "b1", rec["id"]))
    assert out["status"] == "done" and len(out["results"]) == 3 and credits.charged == [1, 1, 1]


def test_the_last_failed_attempt_ends_the_request_so_the_page_stops_waiting():
    db, credits = _db(), _Credits()
    rec = _start(db, credits, count=2)
    job = {"payload": {"preview_id": rec["id"]}, "attempts": 2, "max_attempts": 2}
    with pytest.raises(JobError):
        _work(db, credits, _maker([], fail_at=1), job=job)
    out = _run(P.get(db, "b1", rec["id"]))
    assert out["status"] == "failed" and out["error"] and credits.charged == []


def test_the_owner_can_change_words_remove_one_and_make_one_again():
    db, credits, calls = _db(), _Credits(), []
    rec = _start(db, credits, count=3)
    _work(db, credits, _maker(calls))
    pid = rec["id"]

    edited = _run(P.edit(db, brand_id="b1", preview_id=pid, index=1, headline=" Fly calm ", primary_text="Book now."))
    assert edited["headline"] == "Fly calm"
    draft_id = _run(P.get(db, "b1", pid))["results"][1]["draft_id"]
    assert _run(db["content_drafts"].find_one({"id": draft_id}))["headline"] == "Fly calm"
    with pytest.raises(ValueError):
        _run(P.edit(db, brand_id="b1", preview_id=pid, index=0, headline="x" * 41, primary_text="t"))
    with pytest.raises(LookupError):
        _run(P.edit(db, brand_id="someone-else", preview_id=pid, index=0, headline="x", primary_text="t"))

    out = _run(P.remove(db, brand_id="b1", preview_id=pid, index=0))
    assert [a["headline"] for a in out["results"]] == ["Fly calm", "Ad 3"] and out["count"] == 2

    with patch("app.services.CreditService.credit_service", new=credits):
        started = _run(P.redo(db, brand_id="b1", user_id="u1", preview_id=pid, index=1))
    assert started["status"] == "running" and started["results"][1]["redoing"]
    _work(db, credits, _maker(calls))
    out = _run(P.get(db, "b1", pid))
    assert out["status"] == "done" and out["results"][1]["headline"] == "Ad 4" and not out["results"][1]["redoing"]
    assert "Ad 3" in calls[-1]["brief"]                       # told to differ from the one it replaces
    assert out["results"][1]["angle"] == "outcome"            # and keeps its angle
    assert credits.charged == [1, 1, 1, 1]
    assert _run(db["content_drafts"].count_documents({"preview_id": pid})) == 2   # replaced in place


def test_the_last_ad_cant_be_removed_and_nothing_changes_while_jane_works():
    db, credits = _db(), _Credits()
    rec = _start(db, credits, count=1)
    _work(db, credits, _maker([]))
    with pytest.raises(ValueError, match="at least one ad"):
        _run(P.remove(db, brand_id="b1", preview_id=rec["id"], index=0))
    with patch("app.services.CreditService.credit_service", new=credits):
        _run(P.redo(db, brand_id="b1", user_id="u1", preview_id=rec["id"], index=0))
        with pytest.raises(ValueError, match="still making"):
            _run(P.redo(db, brand_id="b1", user_id="u1", preview_id=rec["id"], index=0))


def test_the_plan_runs_exactly_these_ads_and_makes_or_charges_nothing():
    from app.agents.jane_ads import router as R
    from app.agents.jane_ads.destination import DestinationType
    db, credits, calls = _db(), _Credits(), []
    rec = _start(db, credits, count=3)
    _work(db, credits, _maker(calls))
    _run(P.edit(db, brand_id="b1", preview_id=rec["id"], index=2, headline="My words", primary_text="Mine."))
    plan = SimpleNamespace(concepts=[])
    with patch("app.services.CreditService.credit_service", new=credits):
        first = _run(R._preview_creative(db, {"brand_id": "b1"}, rec["id"], plan, "meta",
                                         DestinationType.WEBSITE, ""))
    assert first.headline == "Ad 1" and [c.headline for c in plan.concepts] == ["Ad 2", "My words"]
    assert first.cta and first.cta == plan.concepts[0].cta    # the button says what this plan's tap does
    assert len(calls) == 3 and credits.charged == [1, 1, 1]   # nothing new made or charged

    # Not Meta: only the first ad runs.
    other = SimpleNamespace(concepts=[])
    _run(R._preview_creative(db, {"brand_id": "b1"}, rec["id"], other, "tiktok", DestinationType.WEBSITE, ""))
    assert other.concepts == []
    # Someone else's ads, or none ready yet: refused.
    with pytest.raises(HTTPException):
        _run(R._preview_creative(db, {"brand_id": "b2"}, rec["id"], plan, "meta", DestinationType.WEBSITE, ""))
