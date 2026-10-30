import asyncio
from types import SimpleNamespace

from app.agents.jane_ads import preflight


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _plan(dest="whatsapp", is_video=False, url="https://res.cloudinary.com/x/video/upload/v1/uri-ads/clip.mp4"):
    return SimpleNamespace(destination_type=dest, page_id="p1", whatsapp_number="2348012345678",
                           creative=SimpleNamespace(is_video=is_video, image_url=url))


def _linked_as(monkeypatch, answer):
    async def fake(page_id, token):
        return answer
    monkeypatch.setattr(preflight, "page_has_whatsapp_linked", fake)


def test_meta_saying_not_linked_warns_firmly_with_the_steps(monkeypatch):
    _linked_as(monkeypatch, False)
    out = _run(preflight.whatsapp_warning(_plan()))
    assert len(out) == 1 and out[0].startswith("Meta says no WhatsApp number")
    assert "Linked accounts → WhatsApp" in out[0] and "+2348012345678" in out[0]


def test_unverifiable_link_is_a_soft_check_not_a_claim(monkeypatch):
    _linked_as(monkeypatch, None)
    out = _run(preflight.whatsapp_warning(_plan()))
    assert len(out) == 1 and "can't see that link" in out[0] and "ignore this" in out[0]
    assert "doesn't show" not in out[0] and "Meta says no" not in out[0]


def test_linked_or_non_whatsapp_gives_no_warning(monkeypatch):
    _linked_as(monkeypatch, True)
    assert _run(preflight.whatsapp_warning(_plan())) == []
    assert _run(preflight.whatsapp_warning(_plan(dest="website"))) == []


def test_public_id_is_read_from_a_cloudinary_video_url():
    assert preflight._public_id("https://res.cloudinary.com/x/video/upload/v123/uri-ads/clip.mp4") == "uri-ads/clip"
    assert preflight._public_id("https://res.cloudinary.com/x/video/upload/c_fill,ar_9:16/v1/uri-ads/clip.mp4") == "uri-ads/clip"
    assert preflight._public_id("https://example.com/a.mp4") == ""


def test_video_problems():
    ok = {"width": 1080, "height": 1920, "duration": 20, "format": "mp4"}
    assert preflight.video_problems(ok) == []
    wide = preflight.video_problems({**ok, "width": 1920, "height": 1080})
    assert wide and "1920×1080" in wide[0]
    assert preflight.video_problems({**ok, "duration": 90})
    assert preflight.video_problems({**ok, "format": "avi"})
    assert preflight.video_problems({**ok, "width": 1080, "height": 1350}) == []


def test_bad_video_warns_and_unknown_video_does_not(monkeypatch):
    async def wide(url):
        return {"width": 1920, "height": 1080, "duration": 10, "format": "mp4"}
    monkeypatch.setattr(preflight, "video_facts", wide)
    out = _run(preflight.video_warning(_plan(is_video=True)))
    assert len(out) == 1 and "9:16" in out[0]

    async def unknown(url):
        return None
    monkeypatch.setattr(preflight, "video_facts", unknown)
    assert _run(preflight.video_warning(_plan(is_video=True))) == []
    assert _run(preflight.video_warning(_plan(is_video=False))) == []


def test_a_failing_check_never_breaks_the_plan(monkeypatch):
    async def boom(plan):
        raise RuntimeError("x")
    monkeypatch.setattr(preflight, "whatsapp_warning", boom)
    assert _run(preflight.meta_warnings(_plan(dest="website"))) == []


def test_a_tiktok_plan_says_it_runs_nationwide():
    from app.agents.jane_ads.models import GeoMode, GeoPlan
    from app.agents.jane_ads.preflight import tiktok_location_warning
    w = tiktok_location_warning(GeoPlan(mode=GeoMode.WATERING_HOLE, city="Lagos, Abuja"))
    assert "all of Nigeria" in w and "Lagos and Abuja" in w
