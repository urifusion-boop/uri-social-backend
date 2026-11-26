"""
PRD follow-ups: feature switches with staged rollout, job monitoring and alerts, the weekly
competitor refresh, reading the brand's own Instagram/Facebook, logo suggestions, and the
per-version actions in "Make one like this" (edit words, make again, remove, kept in Creatives).
"""
import asyncio
import io
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.features import flags
from app.jobs import monitor, queue

@pytest.fixture(autouse=True)
def _no_look_model_call():
    """Reads also pick a look with a model call (tests/test_brand_intel_look.py covers it); keep the
    other read tests off the network."""
    with patch("app.agents.brand_intel.look.pick", new=AsyncMock(return_value={})):
        yield



def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _db():
    flags.clear_cache()
    return AsyncMongoMockClient()["test_followups"]


# ── feature switches ────────────────────────────────────────────────────────────

def test_new_features_start_staff_only():
    db = _db()
    assert all(_run(flags.enabled_for(db, "brand_a", is_staff=True)).values())
    assert not any(_run(flags.enabled_for(db, "brand_a", is_staff=False)).values())


def test_raising_the_percentage_only_adds_brands_and_never_flickers():
    db = _db()
    brands = [f"brand_{i}" for i in range(400)]
    _run(flags.update(db, "competitors", percent=5))
    five = {b for b in brands if _run(flags.is_enabled(db, "competitors", b))}
    _run(flags.update(db, "competitors", percent=50))
    fifty = {b for b in brands if _run(flags.is_enabled(db, "competitors", b))}
    assert five <= fifty                                  # nobody who had it loses it
    assert 5 <= len(five) <= 40 and 150 <= len(fifty) <= 250
    _run(flags.update(db, "competitors", percent=100, staff=True))
    assert all(_run(flags.is_enabled(db, "competitors", b)) for b in brands[:20])


def test_staff_can_be_switched_off_and_unknown_flags_refused():
    db = _db()
    _run(flags.update(db, "website_playbook", percent=0, staff=False))
    assert not _run(flags.is_enabled(db, "website_playbook", "b", is_staff=True))
    with pytest.raises(KeyError):
        _run(flags.update(db, "nope", percent=10))


def test_percentages_are_clamped():
    db = _db()
    assert _run(flags.update(db, "competitors", percent=250))["percent"] == 100
    assert _run(flags.update(db, "competitors", percent=-3))["percent"] == 0


# ── job monitoring ──────────────────────────────────────────────────────────────

def test_stats_count_by_type_and_flag_failures_and_waiting():
    db = _db()
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    docs = []
    for i in range(6):
        docs.append({"job_id": f"f{i}", "type": "website_read", "status": "failed" if i < 3 else "done",
                     "started_at": now - timedelta(minutes=10), "finished_at": now - timedelta(minutes=9),
                     "created_at": now - timedelta(minutes=11), "attempts": 2 if i == 0 else 1})
    docs.append({"job_id": "q1", "type": "competitor_discovery", "status": "queued",
                 "created_at": now - timedelta(minutes=25), "attempts": 0})
    _run(db[queue.COLLECTION].insert_many(docs))
    report = _run(monitor.stats(db, hours=1, now=now))
    types = {t["type"]: t for t in report["types"]}
    assert types["website_read"]["failed"] == 3 and types["website_read"]["failure_rate"] == 0.5
    assert types["website_read"]["median_seconds"] == 60.0 and types["website_read"]["retries"] == 1
    assert types["competitor_discovery"]["oldest_waiting_minutes"] == 25.0
    found = monitor.problems(report)
    assert any("website_read: 3 of 6 failed" in f for f in found)
    assert any("waited 25 minutes" in f for f in found)
    with patch("app.services.platform_alerts.raise_alert", new=AsyncMock()) as alert:
        _run(monitor.check(db, now=now))
    assert alert.await_args.kwargs["kind"] == "background_jobs_unhealthy"


def test_a_healthy_queue_raises_nothing():
    db = _db()
    with patch("app.services.platform_alerts.raise_alert", new=AsyncMock()) as alert:
        assert _run(monitor.check(db)) == []
    assert alert.await_count == 0


# ── weekly competitor refresh ───────────────────────────────────────────────────

def test_brands_last_searched_over_a_week_ago_are_queued_once():
    from app.agents.competitors import service as S
    db = _db()
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    _run(db[S.COLLECTION].insert_many([
        {"brand_id": "old", "competitor_id": "c1", "key": "a", "status": "active", "decided_by": "u_old"},
        {"brand_id": "recent", "competitor_id": "c2", "key": "b", "status": "active", "decided_by": "u_recent"},
        {"brand_id": "none_kept", "competitor_id": "c3", "key": "c", "status": "suggested"},
    ]))
    _run(db[queue.COLLECTION].insert_many([
        {"job_id": "j1", "brand_id": "old", "type": S.JOB_TYPE, "status": "done", "created_at": now - timedelta(days=9)},
        {"job_id": "j2", "brand_id": "recent", "type": S.JOB_TYPE, "status": "done", "created_at": now - timedelta(days=2)},
    ]))
    assert _run(S.refresh_due(db, now=now)) == 1
    queued = _run(db[queue.COLLECTION].find({"status": "queued"}).to_list(length=10))
    assert [(q["brand_id"], q["user_id"]) for q in queued] == [("old", "u_old")]
    assert _run(S.refresh_due(db, now=now)) == 0   # it now has a recent search: nothing more queued
    assert _run(db[queue.COLLECTION].count_documents({"status": "queued"})) == 1


# ── the brand's own Instagram / Facebook ────────────────────────────────────────

class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.content = data, status, b"x"

    def json(self):
        return self._d


def _graph_client(responses):
    client = AsyncMock()
    client.get = AsyncMock(side_effect=lambda url, params=None: responses[url.rsplit("/", 1)[-1]])
    ctx = AsyncMock()
    ctx.__aenter__.return_value = client
    ctx.__aexit__.return_value = False
    return ctx


def test_a_connected_instagram_is_read_through_the_official_api():
    from app.agents.brand_intel import social
    responses = {
        "ig1": _Resp({"username": "magen.ng", "name": "Magen Transport", "biography": "Airport runs and staff buses in Lagos. Book on WhatsApp.",
                      "website": "https://magen.ng", "profile_picture_url": "https://cdn.ig/pic.jpg"}),
        "media": _Resp({"data": [{"caption": "Never miss a flight again — airport runs from ₦25,000"}, {"caption": "Staff buses, monthly plans"}]}),
    }
    with patch("app.agents.brand_intel.social.httpx.AsyncClient", return_value=_graph_client(responses)):
        site = _run(social.read_profile({"ig_user_id": "ig1", "page_access_token": "t"}, "instagram"))
    assert site["url"] == "https://instagram.com/magen.ng" and site["website_found"] == "https://magen.ng"
    assert "BIO: Airport runs" in site["text"] and "₦25,000" in site["text"]
    assert site["logo_candidates"] == ["https://cdn.ig/pic.jpg"] and site["socials"] == {"instagram": "https://instagram.com/magen.ng"}
    assert site["images"] == []  # captions only in this fixture: no post images to show


def test_an_expired_connection_says_to_reconnect():
    from app.agents.brand_intel import social
    from app.agents.brand_intel.safe_fetch import FetchError
    responses = {"ig1": _Resp({"error": {"message": "Error validating access token: Session has expired"}}, 400)}
    with patch("app.agents.brand_intel.social.httpx.AsyncClient", return_value=_graph_client(responses)):
        with pytest.raises(FetchError) as e:
            _run(social.read_profile({"ig_user_id": "ig1", "page_access_token": "t"}, "instagram"))
    assert e.value.code == "reconnect"


def test_reading_instagram_needs_it_connected_and_never_uses_another_brands():
    from app.agents.brand_intel import service, social
    from app.agents.brand_intel.safe_fetch import FetchError
    db = _db()
    _run(db["social_connections"].insert_one({"user_id": "u1", "brand_id": "other", "platform": "instagram",
                                              "connection_status": "active", "ig_user_id": "x", "page_access_token": "t"}))
    with pytest.raises(FetchError) as e:
        _run(service.start_read(db, brand_id="b1", user_id="u1", source="instagram"))
    assert e.value.code == "not_connected" and "Connect your Instagram" in e.value.message
    _run(db["social_connections"].insert_one({"user_id": "u1", "platform": "instagram", "connection_status": "active",
                                              "ig_user_id": "mine", "page_access_token": "t"}))
    assert _run(social.find_connection(db, "u1", "b1", "instagram"))["ig_user_id"] == "mine"
    job = _run(service.start_read(db, brand_id="b1", user_id="u1", source="instagram"))
    assert job["payload"] == {"source": "instagram", "url": "", "force": True}


def test_the_instagram_read_fills_the_playbook_like_a_website_read():
    from app.agents.brand_intel import extractor, service
    db = _db()
    _run(db["social_connections"].insert_one({"user_id": "u1", "platform": "instagram", "connection_status": "active",
                                              "ig_user_id": "ig1", "page_access_token": "t"}))
    site = {"url": "https://instagram.com/magen.ng", "text": "x" * 100, "pages": [], "socials": {"instagram": "https://instagram.com/magen.ng"},
            "page_list": [{"kind": "instagram", "url": "https://instagram.com/magen.ng", "title": "Magen"}],
            "colors": [], "logo_candidates": [], "jsonld": [], "website_found": "https://magen.ng"}

    class Ctx:
        async def step(self, *a, **k):
            pass

        async def found(self, items):
            pass

    with patch("app.agents.brand_intel.social.read_profile", new=AsyncMock(return_value=site)), \
         patch.object(extractor, "extract", new=AsyncMock(return_value={"brand_name": {"value": "Magen", "basis": "stated", "page": site["url"]}})), \
         patch("app.services.brand_notifier.notify_in_app", new=AsyncMock()):
        out = _run(service.read_site_job(db, {"job_id": "j", "user_id": "u1", "brand_id": "b1",
                                              "payload": {"source": "instagram", "url": "", "force": True}}, Ctx()))
    assert out["fields"]["website"]["value"] == "https://magen.ng"
    assert out["fields"]["social_links"]["value"] == {"instagram": "https://instagram.com/magen.ng"}
    assert out["fields"]["brand_name"]["value"] == "Magen"
    assert out["fields"]["default_link"]["value"] == "https://magen.ng"


# ── a read fills the whole Playbook, not half of it (staging 2026-11-16: 47%) ────

def test_instagram_formats_come_from_what_the_account_actually_posts():
    from app.agents.brand_intel import social
    media = [{"media_type": "VIDEO"}, {"media_type": "CAROUSEL_ALBUM"}, {"media_type": "VIDEO"}, {"media_type": "IMAGE"},
             {"media_type": "SOMETHING_NEW"}]
    assert social._formats(media) == ["Short Video / Reel", "Carousel", "Single Image"]


def test_choice_fields_keep_only_the_playbooks_own_options_spelled_its_way():
    from app.agents.brand_intel import extractor
    site = {"url": "https://instagram.com/jtf", "pages": [], "text": "We go to schools every term. Period poverty ends with education, "
            "and every girl deserves dignity. Send us a DM to volunteer."}
    raw = {
        "content_pillars": {"value": ["tips & education", "Customer Stories", "Memes", "Behind the Scenes"], "basis": "inferred"},
        "cta_styles": {"value": "DM us", "basis": "inferred"},
        "primary_goal": {"value": ["build community", "Drive Sales"], "basis": "inferred"},
        "audience_age_range": {"value": ["Gen Z (18-24)", "Millennials (25-40)", "kids"], "basis": "inferred"},
        "business_stage": {"value": "a start-up", "basis": "inferred"},
        "voice_sample": {"value": "Period poverty ends with education, and every girl deserves dignity.", "basis": "stated"},
    }
    out = extractor.clean(raw, site)
    assert out["content_pillars"]["value"] == ["Tips & Education", "Customer Stories", "Behind the Scenes"]
    assert out["cta_styles"]["value"] == ["DM us"]
    assert out["primary_goal"]["value"] == "Build Community"
    assert out["audience_age_range"]["value"] == "Gen Z (18-24), Millennials (25-40)"
    assert "business_stage" not in out          # not one of the options: left for the owner
    assert out["voice_sample"]["value"].startswith("Period poverty")


def test_a_voice_sample_jane_wrote_herself_is_dropped():
    from app.agents.brand_intel import extractor
    site = {"url": "u", "pages": [], "text": "Airport runs from 25,000 naira."}
    raw = {"voice_sample": {"value": "We are passionate about delivering excellence to every valued customer we serve daily.",
                            "basis": "stated"}}
    assert "voice_sample" not in extractor.clean(raw, site)


def test_the_prompt_lists_the_options_for_choice_fields():
    from app.agents.brand_intel import extractor
    prompt = extractor._prompt({"url": "u", "text": "t"})
    assert '"Build Community"' in prompt and '"Gen Z (18-24)"' in prompt and "social media profile" in prompt


def test_a_logo_gives_its_colours_leaving_out_white_black_and_grey():
    from PIL import Image
    from app.agents.brand_intel import service
    img = Image.new("RGB", (100, 100), (255, 255, 255))
    for x in range(60):
        for y in range(100):
            img.putpixel((x, y), (194, 24, 91))
    for x in range(60, 80):
        for y in range(100):
            img.putpixel((x, y), (20, 20, 20))
    assert service.logo_colors(img) == ["#C01848"]


def test_a_social_read_without_website_colours_takes_them_from_the_logo():
    from app.agents.brand_intel import extractor, service
    db = _db()
    _run(db["social_connections"].insert_one({"user_id": "u1", "platform": "instagram", "connection_status": "active",
                                              "ig_user_id": "ig1", "page_access_token": "t"}))
    site = {"url": "https://instagram.com/jtf", "text": "x" * 100, "pages": [], "socials": {"instagram": "https://instagram.com/jtf"},
            "page_list": [{"kind": "instagram", "url": "https://instagram.com/jtf", "title": "JTF"}],
            "colors": [], "logo_candidates": ["https://cdn/p.jpg"], "jsonld": [], "website_found": "",
            "formats": ["Carousel", "Single Image"]}

    class Ctx:
        async def step(self, *a, **k):
            pass

        async def found(self, items):
            pass

    logo = {"value": "https://s3/l.png", "basis": "inferred", "page": site["url"], "colors": ["#C01848"]}
    with patch("app.agents.brand_intel.social.read_profile", new=AsyncMock(return_value=site)), \
         patch.object(extractor, "extract", new=AsyncMock(return_value={})), \
         patch.object(service, "find_logo", new=AsyncMock(return_value=logo)), \
         patch("app.services.brand_notifier.notify_in_app", new=AsyncMock()):
        out = _run(service.read_site_job(db, {"job_id": "j", "user_id": "u1", "brand_id": "b1",
                                              "payload": {"source": "instagram", "url": "", "force": True}}, Ctx()))
    f = out["fields"]
    assert f["brand_colors"]["value"] == ["#C01848"] and f["brand_colors"]["basis"] == "inferred"
    assert f["logo_url"] == {"value": "https://s3/l.png", "basis": "inferred", "page": site["url"]}
    assert f["preferred_formats"]["value"] == ["Carousel", "Single Image"]
    assert f["default_link"]["value"] == "https://instagram.com/jtf"


# ── logo suggestions ────────────────────────────────────────────────────────────

def _png(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (194, 24, 91)).save(buf, format="PNG")
    return buf.getvalue()


def test_the_first_real_sized_logo_is_stored_as_a_guess():
    from app.agents.brand_intel import service
    from app.agents.brand_intel.safe_fetch import FetchError, Page
    images = {"https://x/tiny.png": _png(16, 16), "https://x/banner.png": _png(1200, 100), "https://x/logo.png": _png(300, 120)}

    async def fake_fetch(url, **kw):
        if url in images:
            return Page(url=url, status=200, content_type="image/png", text="", body=images[url])
        raise FetchError("no", "not_found")

    with patch("app.agents.brand_intel.service.fetch", new=fake_fetch), \
         patch("app.utils.s3_upload.upload_bytes", new=AsyncMock(return_value="https://s3/logo.png")) as up:
        out = _run(service.find_logo(["https://x/icon.svg", "https://x/missing.png", "https://x/tiny.png",
                                      "https://x/banner.png", "https://x/logo.png"], "https://x/"))
    # A pink logo also gives its colour, for a profile with no website colours to go on.
    assert out == {"value": "https://s3/logo.png", "basis": "inferred", "page": "https://x/", "colors": ["#C01848"]}
    assert up.await_count == 1


def test_no_usable_logo_gives_no_suggestion():
    from app.agents.brand_intel import service
    from app.agents.brand_intel.safe_fetch import FetchError
    with patch("app.agents.brand_intel.service.fetch", new=AsyncMock(side_effect=FetchError("no", "x"))):
        assert _run(service.find_logo(["https://x/a.png"], "https://x/")) is None


# ── make one like this: per-version actions ─────────────────────────────────────

def _rec(db, status="done"):
    from app.agents.competitors import ads as A
    _run(db[A.RECREATIONS].insert_one({
        "recreation_id": "rec_1", "brand_id": "b1", "user_id": "u1", "ad_id": "cad_1", "count": 2, "status": status,
        "product": "Airport runs", "reference": {"analysis": {"angle": "offer"}},
        "results": [{"image_url": "https://v1", "headline": "One", "primary_text": "First.", "angle": "offer", "draft_id": "d1"},
                    {"image_url": "https://v2", "headline": "Two", "primary_text": "Second.", "angle": "problem", "draft_id": "d2"}]}))
    _run(db["content_drafts"].insert_many([{"id": "d1", "user_id": "u1", "content_source": "ad_recreate", "image_url": "https://v1"},
                                           {"id": "d2", "user_id": "u1", "content_source": "ad_recreate", "image_url": "https://v2"}]))


def test_editing_a_versions_words_updates_it_and_its_draft():
    from app.agents.competitors import ads as A
    db = _db()
    _rec(db)
    out = _run(A.edit_version(db, brand_id="b1", recreation_id="rec_1", index=1, headline="  Ride easy ", primary_text="Book now."))
    assert out["headline"] == "Ride easy"
    draft = _run(db["content_drafts"].find_one({"id": "d2"}))
    assert draft["headline"] == "Ride easy" and draft["content"] == "Book now."
    with pytest.raises(ValueError):
        _run(A.edit_version(db, brand_id="b1", recreation_id="rec_1", index=0, headline="x" * 41, primary_text="t"))
    with pytest.raises(LookupError):
        _run(A.edit_version(db, brand_id="b2", recreation_id="rec_1", index=0, headline="x", primary_text="t"))


def test_removing_a_version_takes_it_out_of_creatives_too():
    from app.agents.competitors import ads as A
    db = _db()
    _rec(db)
    out = _run(A.remove_version(db, brand_id="b1", recreation_id="rec_1", index=0))
    assert [r["headline"] for r in out["results"]] == ["Two"]
    assert _run(db["content_drafts"].find_one({"id": "d1"})) is None


def test_making_one_version_again_replaces_it_in_place_for_one_credit():
    from app.agents.competitors import ads as A
    from app.agents.jane_ads.models import AdCreative
    db = _db()
    _rec(db)

    class Credits:
        deducted = 0

        async def check_sufficient_credits(self, user_id, required=1):
            return True

        async def deduct_credit(self, user_id, **kw):
            Credits.deducted += 1

    # The design read for the first versions is reused: a redo keeps the same look.
    _run(db[A.RECREATIONS].update_one({"recreation_id": "rec_1"}, {"$set": {"reference.recipe": "Green poster.", "reference.recipe_v": A.RECIPE_VERSION}}))

    async def gen(*a, **kw):
        assert "clearly different from the last attempt" in a[3]
        assert "Green poster." in kw["image_layout"]
        return AdCreative(image_url="https://v2b", headline="Two again", primary_text="New second.")

    with patch("app.services.CreditService.credit_service", new=Credits()), \
         patch("app.agents.jane_ads.creative.generate_ad_creative", new=gen), \
         patch.object(A, "_profile", new=AsyncMock(return_value={"brand_name": "Magen"})):
        started = _run(A.redo_version(db, brand_id="b1", user_id="u1", recreation_id="rec_1", index=1))
        assert started["status"] == "running"
        with pytest.raises(ValueError):          # one at a time
            _run(A.redo_version(db, brand_id="b1", user_id="u1", recreation_id="rec_1", index=0))
        job = _run(queue.claim(db, [A.RECREATE_JOB]))

        class Ctx:
            async def step(self, *a, **k):
                pass

        _run(A.recreate_job(db, job, Ctx()))
        rec = _run(A.get_recreation(db, "b1", "rec_1"))
    assert [r["headline"] for r in rec["results"]] == ["One", "Two again"] and rec["status"] == "done"
    assert Credits.deducted == 1
    assert _run(db["content_drafts"].find_one({"id": "d2"}))["image_url"] == "https://v2b"


def test_every_new_version_is_kept_in_creatives_as_soon_as_its_made():
    from app.agents.competitors import ads as A
    from app.agents.jane_ads.models import AdCreative
    db = _db()
    _run(db[A.ADS].insert_one({"ad_id": "cad_1", "brand_id": "b1", "image_url": "https://x", "status": "ready",
                               "analysis": A.clean_analysis({"angle": "offer"})}))

    class Credits:
        async def check_sufficient_credits(self, user_id, required=1):
            return True

        async def deduct_credit(self, user_id, **kw):
            pass

    with patch("app.services.CreditService.credit_service", new=Credits()), \
         patch("app.agents.jane_ads.creative.generate_ad_creative",
               new=AsyncMock(return_value=AdCreative(image_url="https://v", headline="H", primary_text="T"))), \
         patch.object(A, "read_recipe", new=AsyncMock(return_value="")), \
         patch.object(A, "_profile", new=AsyncMock(return_value={"brand_name": "Magen"})):
        rec = _run(A.start_recreate(db, brand_id="b1", user_id="u1", ad_id="cad_1", count=1))
        job = _run(queue.claim(db, [A.RECREATE_JOB]))

        class Ctx:
            async def step(self, *a, **k):
                pass

        _run(A.recreate_job(db, job, Ctx()))
    out = _run(A.get_recreation(db, "b1", rec["id"]))
    draft = _run(db["content_drafts"].find_one({"id": out["results"][0]["draft_id"]}))
    assert draft and draft["has_image"] and draft["content_source"] == "ad_recreate"
    used = _run(A.use_version(db, brand_id="b1", user_id="u1", recreation_id=rec["id"], index=0))
    assert used["draft_id"] == draft["id"]                 # no duplicate draft when used


def test_thumbnails_are_small_copies():
    from app.agents.competitors import ads as A
    from app.agents.brand_intel.safe_fetch import Page
    captured = {}

    async def up(data, **kw):
        from PIL import Image
        captured["size"] = Image.open(io.BytesIO(data)).size
        return "https://s3/thumb.jpg"

    with patch("app.agents.brand_intel.safe_fetch.fetch", new=AsyncMock(return_value=Page("u", 200, "image/png", "", _png(1600, 1200)))), \
         patch("app.utils.s3_upload.upload_bytes", new=up):
        assert _run(A.make_thumbnail("https://cdn/ad.png")) == "https://s3/thumb.jpg"
    assert max(captured["size"]) == A.THUMB_PX
