"""
Competitor ads (PRD WS3) and "Make one like this" (WS4).

Vision and image generation are mocked; the rest is real: saving queues a read, the read is
stored in plain words, lists filter and page, a recreation checks credits, makes each version
with Jane's own ad maker from the reference's idea (never its words), charges a credit per
version, and a retry carries on instead of starting over.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.agents.competitors import ads as A
from app.agents.jane_ads.models import AdCreative
from app.jobs import queue
from app.jobs.worker import JobError


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _db():
    return AsyncMongoMockClient()["test_competitor_ads"]


class _Ctx:
    def __init__(self):
        self.steps = []

    async def step(self, sid, label, status="running", detail=""):
        self.steps.append((sid, status, detail))

    async def found(self, items):
        pass


VISION = {"headline": "Never miss a flight", "angle": "problem", "hook": "a worried traveller at the airport",
          "offer": "₦20,000 fixed", "cta": "Book now", "customer_problem": "Missing flights in traffic",
          "format": "photo", "layout": "photo of a traveller, bold headline band at the bottom",
          "why_it_works": "It shows the fear first, then a fixed price.", "junk": "x"}


def test_saving_an_ad_queues_a_read_and_the_read_is_stored_in_plain_words():
    db = _db()
    _run(db["brand_competitors"].insert_one({"brand_id": "b1", "competitor_id": "cmp_1", "name": "RideEasy"}))
    ad = _run(A.save_ad(db, brand_id="b1", user_id="u1", image_url="https://cdn.uri/ad.jpg", competitor_id="cmp_1",
                        platform="Instagram"))
    assert ad["status"] == "analyzing" and ad["competitor_name"] == "RideEasy" and ad["platform"] == "instagram"
    job = _run(queue.claim(db, [A.ANALYSIS_JOB]))
    with patch.object(A, "_vision", new=AsyncMock(return_value=VISION)):
        _run(A.analysis_job(db, job, _Ctx()))
    listed = _run(A.list_ads(db, "b1"))["ads"][0]
    assert listed["status"] == "ready" and listed["angle_label"] == "Problem / solution"
    assert listed["analysis"]["why_it_works"] == "It shows the fear first, then a fixed price."
    assert "junk" not in listed["analysis"]


def test_a_picture_that_isnt_uploaded_is_refused():
    with pytest.raises(ValueError):
        _run(A.save_ad(_db(), brand_id="b1", user_id="u1", image_url="file:///etc/passwd"))


def test_an_unknown_angle_or_format_is_tidied():
    out = A.clean_analysis({"angle": "weird", "format": "hologram", "headline": "  Hi   there "})
    assert out["angle"] == "other" and out["format"] == "graphic" and out["headline"] == "Hi there"


def test_ads_filter_by_competitor_and_angle_and_page_newest_first():
    db = _db()
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 11, 13, tzinfo=timezone.utc)
    for i in range(5):
        _run(db[A.ADS].insert_one({"ad_id": f"cad_{i}", "brand_id": "b1", "image_url": "https://x", "status": "ready",
                                   "competitor_id": "cmp_1" if i % 2 else "cmp_2",
                                   "analysis": {"angle": "offer" if i < 2 else "problem"},
                                   "created_at": base + timedelta(minutes=i)}))
    first = _run(A.list_ads(db, "b1", limit=2))
    assert [a["id"] for a in first["ads"]] == ["cad_4", "cad_3"] and first["next_before"]
    second = _run(A.list_ads(db, "b1", limit=2, before=datetime.fromisoformat(first["next_before"])))
    assert [a["id"] for a in second["ads"]] == ["cad_2", "cad_1"]
    assert [a["id"] for a in _run(A.list_ads(db, "b1", competitor_id="cmp_1"))["ads"]] == ["cad_3", "cad_1"]
    assert [a["id"] for a in _run(A.list_ads(db, "b1", angle="offer"))["ads"]] == ["cad_1", "cad_0"]


def _ready_ad(db):
    _run(db[A.ADS].insert_one({"ad_id": "cad_1", "brand_id": "b1", "image_url": "https://cdn.uri/ad.jpg",
                               "status": "ready", "competitor_name": "RideEasy", "analysis": A.clean_analysis(VISION)}))


class _Credits:
    def __init__(self, enough=True):
        self.enough, self.deducted = enough, 0

    async def check_sufficient_credits(self, user_id, required=1):
        return self.enough

    async def deduct_credit(self, user_id, **kw):
        self.deducted += 1


def test_making_versions_checks_credits_first():
    db = _db()
    _ready_ad(db)
    with patch("app.services.CreditService.credit_service", new=_Credits(enough=False)):
        with pytest.raises(PermissionError) as e:
            _run(A.start_recreate(db, brand_id="b1", user_id="u1", ad_id="cad_1", count=3))
    assert "3 content credits" in str(e.value)


def test_versions_are_made_from_the_idea_not_the_words_and_charged_one_by_one():
    db = _db()
    _ready_ad(db)
    credits = _Credits()
    made = []

    async def fake_generate(business_name, category, goal, description, **kw):
        made.append({"business": business_name, "brief": description, "layout": kw.get("image_layout")})
        return AdCreative(image_url=f"https://cdn.uri/v{len(made)}.png", headline=f"Version {len(made)}",
                          primary_text="Fixed-price airport runs from Magen.")

    profile = {"brand_name": "Magen", "industry": "Event transport", "product_description": "Airport transport",
               "customer_location": "Lekki"}
    with patch("app.services.CreditService.credit_service", new=credits), \
         patch("app.agents.jane_ads.creative.generate_ad_creative", new=fake_generate), \
         patch.object(A, "_profile", new=AsyncMock(return_value=profile)):
        rec = _run(A.start_recreate(db, brand_id="b1", user_id="u1", ad_id="cad_1", product="Airport runs", count=2))
        job = _run(queue.claim(db, [A.RECREATE_JOB]))
        _run(A.recreate_job(db, job, _Ctx()))
        out = _run(A.get_recreation(db, "b1", rec["id"]))
    assert out["status"] == "done" and [r["headline"] for r in out["results"]] == ["Version 1", "Version 2"]
    assert credits.deducted == 2
    assert made[0]["business"] == "Magen" and "Advertise: Airport runs." in made[0]["brief"]
    assert "Never reuse the reference's words" in made[0]["brief"]
    assert "Never miss a flight" not in made[0]["brief"] and "₦20,000" not in made[0]["brief"]   # not its words or price
    assert made[0]["layout"]   # the reference's problem-first layout
    assert out["reference"]["competitor_name"] == "RideEasy"


def test_a_retry_carries_on_from_the_versions_already_made():
    db = _db()
    _ready_ad(db)
    credits = _Credits()
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            return AdCreative(image_url="", headline="", primary_text="")
        return AdCreative(image_url=f"https://cdn.uri/v{calls['n']}.png", headline=f"V{calls['n']}", primary_text="t")

    with patch("app.services.CreditService.credit_service", new=credits), \
         patch("app.agents.jane_ads.creative.generate_ad_creative", new=flaky), \
         patch.object(A, "_profile", new=AsyncMock(return_value={"brand_name": "Magen"})):
        rec = _run(A.start_recreate(db, brand_id="b1", user_id="u1", ad_id="cad_1", count=2))
        job = _run(queue.claim(db, [A.RECREATE_JOB]))
        with pytest.raises(JobError):
            _run(A.recreate_job(db, job, _Ctx()))
        _run(A.recreate_job(db, job, _Ctx()))
        out = _run(A.get_recreation(db, "b1", rec["id"]))
    assert [r["headline"] for r in out["results"]] == ["V1", "V3"] and credits.deducted == 2   # failed one not charged
    assert out["status"] == "done" and out["error"] == ""


def test_an_ad_still_being_read_cannot_be_recreated_yet():
    db = _db()
    _run(db[A.ADS].insert_one({"ad_id": "cad_9", "brand_id": "b1", "image_url": "https://x", "status": "analyzing"}))
    with patch("app.services.CreditService.credit_service", new=_Credits()):
        with pytest.raises(ValueError):
            _run(A.start_recreate(db, brand_id="b1", user_id="u1", ad_id="cad_9"))


def test_using_a_version_makes_one_content_draft_the_builder_can_start_from():
    db = _db()
    _run(db[A.RECREATIONS].insert_one({"recreation_id": "rec_1", "brand_id": "b1", "user_id": "u1", "ad_id": "cad_1",
                                       "count": 1, "status": "done",
                                       "results": [{"image_url": "https://cdn.uri/v1.png", "headline": "Ride easy",
                                                    "primary_text": "Fixed-price airport runs.", "angle": "offer"}]}))
    first = _run(A.use_version(db, brand_id="b1", user_id="u1", recreation_id="rec_1", index=0))
    again = _run(A.use_version(db, brand_id="b1", user_id="u1", recreation_id="rec_1", index=0))
    assert first["draft_id"] == again["draft_id"] and first["image_url"] == "https://cdn.uri/v1.png"
    drafts = _run(db["content_drafts"].find({}).to_list(length=10))
    assert len(drafts) == 1 and drafts[0]["has_image"] and drafts[0]["user_id"] == "u1"
    from app.agents.jane_ads.creative import get_draft_image
    assert _run(get_draft_image(first["draft_id"], "u1", db))["image_url"] == "https://cdn.uri/v1.png"
    with pytest.raises(LookupError):
        _run(A.use_version(db, brand_id="b2", user_id="u2", recreation_id="rec_1", index=0))
