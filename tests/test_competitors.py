"""
Competitor discovery and management (PRD Workstream 2).

The search is mocked; everything around it is real: candidates are only kept when a real page
vouches for them, the business itself and duplicates are dropped, suggestions wait for the
owner, removed ones never come back, kept ones reach the Playbook's competitor list, and a
search is reused by similar brands.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.agents.brand_intel.safe_fetch import FetchError, Page
from app.agents.competitors import discovery as D
from app.agents.competitors import service as S
from app.jobs.worker import JobError


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _db():
    return AsyncMongoMockClient()["test_competitors"]


SITES = {
    "https://rideeasy.ng/": "<title>RideEasy Lagos | Airport transfers</title><meta name='description' content='Airport transfers in Lagos'>"
                            "<a href='https://instagram.com/rideeasy.ng'>IG</a><p>RideEasy airport pickups</p>",
    "https://lagosshuttle.com/": "<title>Lagos Shuttle</title><p>Staff bus services across Lekki</p>",
    "https://magen.ng/": "<title>Magen</title>",
    "https://directory.ng/vip-wheels": "<title>VIP Wheels listing</title><p>VIP Wheels — wedding car hire, Ikoyi</p>",
}


async def _fake_fetch(url, **kw):
    for known, html in SITES.items():
        if url.rstrip("/") == known.rstrip("/"):
            return Page(url=known, status=200, content_type="text/html", text=html)
    raise FetchError("We couldn't find that website.", "dns")


CANDIDATES = [
    {"name": "RideEasy", "website": "https://rideeasy.ng", "kind": "direct", "why": "Same airport runs in Lagos",
     "what_they_sell": "Airport transfers", "source_url": ""},
    {"name": "Lagos Shuttle", "website": "https://lagosshuttle.com", "kind": "similar", "why": "Staff buses"},
    {"name": "Phantom Movers", "website": "https://phantom-movers.ng", "kind": "direct", "source_url": "https://nowhere.ng"},
    {"name": "VIP Wheels", "website": "", "instagram": "@vipwheels", "kind": "similar", "source_url": "https://directory.ng/vip-wheels"},
    {"name": "Magen", "website": "https://magen.ng", "kind": "direct"},
    {"name": "RideEasy", "website": "https://www.rideeasy.ng/", "kind": "direct"},
]
PROFILE = {"brand_name": "Magen", "industry": "Event transport", "website": "https://magen.ng",
           "key_products_services": ["Airport runs", "Staff buses"], "customer_location": "Lekki, Lagos",
           "competitor_handles": ["@oldrival"]}


class _Ctx:
    def __init__(self):
        self.steps, self.found_items = [], []

    async def step(self, sid, label, status="running", detail=""):
        self.steps.append((sid, status, detail))

    async def found(self, items):
        self.found_items += items


def _patches(profile=PROFILE, search=None):
    saved = []

    class FakeBPS:
        @staticmethod
        async def get(user_id, db, brand_id=None):
            return {"status": True, "responseData": dict(profile)}

        @staticmethod
        async def save(user_id, data, db, brand_id=None):
            saved.append(data)
            profile.update(data)
            return {"status": True}

    return saved, [
        patch("app.agents.competitors.discovery.fetch", new=_fake_fetch),
        patch("app.agents.competitors.service.fetch", new=_fake_fetch),
        patch("app.agents.competitors.discovery.search", new=search or AsyncMock(return_value=list(CANDIDATES))),
        patch("app.agents.social_media_manager.services.brand_profile_service.BrandProfileService", new=FakeBPS),
        patch("app.services.brand_notifier.notify_in_app", new=AsyncMock()),
    ]


def _with(patches, fn):
    for p in patches:
        p.start()
    try:
        return fn()
    finally:
        for p in patches:
            p.stop()


def test_only_businesses_a_real_page_vouches_for_are_suggested():
    db = _db()
    profile = dict(PROFILE)
    _, ps = _patches(profile)
    ctx = _Ctx()
    out = _with(ps, lambda: _run(S.discovery_job(db, {"brand_id": "b1", "user_id": "u1", "payload": {}}, ctx)))
    listed = _run(S.list_for(db, "b1"))
    names = [c["name"] for c in listed["suggested"]]
    assert names == ["RideEasy", "Lagos Shuttle", "VIP Wheels"]          # direct first
    assert "Phantom Movers" not in names and "Magen" not in names        # invented, and the business itself
    ride = listed["suggested"][0]
    assert ride["instagram"] == "rideeasy.ng" and ride["description"] == "Airport transfers in Lagos"
    vip = next(c for c in listed["suggested"] if c["name"] == "VIP Wheels")
    assert vip["website"] == "" and vip["source_url"] == "https://directory.ng/vip-wheels"
    assert out == {"suggested": 3, "checked": 3}
    assert ("check", "done", "3 of 6 checked out") in ctx.steps
    assert listed["active"] == []                                         # nothing is kept without the owner


def test_a_thin_playbook_is_told_what_to_add():
    db = _db()
    _, ps = _patches({"brand_name": "Magen"})
    with pytest.raises(JobError) as e:
        _with(ps, lambda: _run(S.discovery_job(db, {"brand_id": "b1", "user_id": "u1", "payload": {}}, _Ctx())))
    assert e.value.code == "playbook_thin" and "products and the areas you serve" in e.value.message


def test_keeping_one_adds_it_to_the_playbook_and_removing_takes_it_out():
    db = _db()
    profile = dict(PROFILE)
    saved, ps = _patches(profile)

    def flow():
        _run(S.discovery_job(db, {"brand_id": "b1", "user_id": "u1", "payload": {}}, _Ctx()))
        ride = next(c for c in _run(S.list_for(db, "b1"))["suggested"] if c["name"] == "RideEasy")
        kept = _run(S.keep(db, brand_id="b1", user_id="u1", competitor_id=ride["id"]))
        assert kept["status"] == "active"
        assert profile["competitor_handles"] == ["@oldrival", "@rideeasy.ng"]
        _run(S.remove(db, brand_id="b1", user_id="u1", competitor_id=ride["id"], reason="different_area"))
        assert profile["competitor_handles"] == ["@oldrival"]      # the owner's own entry is untouched
        listed = _run(S.list_for(db, "b1"))
        assert listed["removed_count"] == 1 and all(c["name"] != "RideEasy" for c in listed["suggested"] + listed["active"])
    _with(ps, flow)


def test_removed_and_known_businesses_are_never_suggested_again_and_searches_are_reused():
    db = _db()
    profile = dict(PROFILE)
    search = AsyncMock(return_value=list(CANDIDATES))
    _, ps = _patches(profile, search=search)

    def flow():
        _run(S.discovery_job(db, {"brand_id": "b1", "user_id": "u1", "payload": {}}, _Ctx()))
        ride = next(c for c in _run(S.list_for(db, "b1"))["suggested"] if c["name"] == "RideEasy")
        _run(S.remove(db, brand_id="b1", user_id="u1", competitor_id=ride["id"], reason="not_a_competitor"))
        again = _run(S.discovery_job(db, {"brand_id": "b1", "user_id": "u1", "payload": {}}, _Ctx()))
        assert again["suggested"] == 0
        # Another brand with the same industry, products and area reuses the search.
        other = _run(S.discovery_job(db, {"brand_id": "b2", "user_id": "u2", "payload": {}}, _Ctx()))
        assert other["suggested"] == 3
        assert search.await_count == 1
        # The removed business isn't sent back to the search as a candidate either.
    _with(ps, flow)


def test_an_owner_can_add_one_by_hand_and_change_its_kind():
    db = _db()
    profile = dict(PROFILE)
    _, ps = _patches(profile)

    def flow():
        c = _run(S.add(db, brand_id="b1", user_id="u1", name="Lagos Shuttle", website="lagosshuttle.com"))
        assert c["status"] == "active" and c["source"] == "added" and c["website"] == "https://lagosshuttle.com/"
        assert "Lagos Shuttle" in profile["competitor_handles"]
        c = _run(S.set_kind(db, brand_id="b1", competitor_id=c["id"], kind="similar"))
        assert c["kind"] == "similar"
        with pytest.raises(ValueError):
            _run(S.add(db, brand_id="b1", user_id="u1", name="  "))
    _with(ps, flow)


def test_a_brand_cannot_search_without_limit():
    db = _db()
    from app.jobs import queue
    for _ in range(S.DISCOVERIES_PER_BRAND_PER_DAY):
        _run(queue.enqueue(db, S.JOB_TYPE, {}, brand_id="b1"))
    with pytest.raises(ValueError):
        _run(S.start_discovery(db, brand_id="b1", user_id="u1"))


def test_the_search_prompt_carries_the_playbook_and_exclusions():
    brief = D.brief_from_profile(PROFILE)
    prompt = D._prompt(brief, ["RideEasy"])
    assert "Event transport" in prompt and "Lekki, Lagos" in prompt and "Airport runs" in prompt
    assert "RideEasy" in prompt and "Never this business itself" in prompt
    assert D.enough_to_search(brief) and not D.enough_to_search(D.brief_from_profile({"industry": "x"}))


def test_search_reads_json_even_when_the_model_wraps_it_in_text():
    class Resp:
        output_text = 'Here you go:\n{"competitors": [{"name": "RideEasy", "website": "https://rideeasy.ng"}]}\nDone.'

    with patch("openai.AsyncOpenAI") as cls, patch.object(D.settings, "OPENAI_API_KEY", "k"):
        cls.return_value.responses.create = AsyncMock(return_value=Resp())
        out = _run(D.search(D.brief_from_profile(PROFILE), []))
    assert out == [{"name": "RideEasy", "website": "https://rideeasy.ng"}]
