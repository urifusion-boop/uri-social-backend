"""
Google video ads (google_video.py): an uploaded video goes to YouTube through Google Ads, a pasted
YouTube link runs as it is, and the owner sees what Google will make of a video before using it.
"""
import asyncio
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.agents.jane_ads import google_video as V
from app.agents.jane_ads.adapters import google_visual as gv


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── links and rules ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("link", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ?si=abc",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
    "https://m.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ",
])
def test_every_kind_of_youtube_link_gives_the_video_id(link):
    assert V.youtube_id(link) == "dQw4w9WgXcQ"


def test_a_link_that_isnt_youtube_gives_nothing():
    assert V.youtube_id("https://vimeo.com/123") == "" and V.youtube_id("") == ""


def _tones(out):
    return [(f["tone"], f["text"]) for f in out["facts"]]


def test_length_is_judged_by_where_the_ad_runs():
    assert not V.facts_for(4, 1080, 1920, True, "demand_gen")["ok"]              # Google's 5-second floor
    assert not V.facts_for(8, 1080, 1920, True, "pmax")["ok"]                    # Everywhere needs 10
    short = V.facts_for(8, 1080, 1920, True, "demand_gen")
    assert short["ok"] and short["facts"][0]["tone"] == "warn" and "not before other" in short["facts"][0]["text"]
    assert V.facts_for(24, 1080, 1920, True, "pmax")["facts"][0] == {"tone": "ok", "text": "24 seconds"}


def test_shape_and_sound_are_explained():
    upright = V.facts_for(24, 1080, 1920, True, "demand_gen")
    assert upright["upright"] and ("ok", "Upright, so it also runs in YouTube Shorts") in _tones(upright)
    assert ("ok", "Has sound") in _tones(upright)
    sideways = V.facts_for(24, 1920, 1080, False, "demand_gen")
    assert any(t == "warn" and "won't run in Shorts" in x for t, x in _tones(sideways))
    assert any(t == "warn" and "No sound" in x for t, x in _tones(sideways))
    long_upright = V.facts_for(90, 1080, 1920, True, "demand_gen")
    assert any("over a minute" in x for _, x in _tones(long_upright))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_a_real_video_is_read_for_length_shape_and_sound(tmp_path):
    path = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=pink:s=360x640:d=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-shortest", "-pix_fmt", "yuv420p",
                    str(path)], check=True)
    meta = _run(V.probe(str(path)))
    assert 5.5 < meta["duration"] < 6.5 and (meta["width"], meta["height"]) == (360, 640) and meta["has_audio"]


def test_a_file_that_isnt_a_video_is_refused_in_plain_words(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_text("not a video")
    with pytest.raises(ValueError, match="couldn't read that video"):
        _run(V.probe(str(bad)))


# ── a pasted YouTube link ─────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text

    def json(self):
        return self._body


def _youtube(monkeypatch, oembed_status, page=""):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            if "oembed" in url:
                return _Resp(oembed_status, {"title": "How we deliver", "author_name": "Magen"})
            return _Resp(200, text=page)

    monkeypatch.setattr(V.httpx, "AsyncClient", _Client)


def test_a_youtube_link_is_found_with_its_title_length_and_picture(monkeypatch):
    _youtube(monkeypatch, 200, page='..."lengthSeconds":"31",...')
    out = _run(V.check_youtube("https://youtu.be/dQw4w9WgXcQ", "pmax"))
    assert out["ok"] and out["video_id"] == "dQw4w9WgXcQ" and out["title"] == "How we deliver"
    assert ("ok", "31 seconds") in _tones(out) and out["stills"] == [V.thumbnail_for("dQw4w9WgXcQ")]


def test_a_private_or_missing_youtube_video_says_how_to_fix_it(monkeypatch):
    _youtube(monkeypatch, 401)
    with pytest.raises(ValueError, match="private"):
        _run(V.check_youtube("https://youtu.be/dQw4w9WgXcQ", "demand_gen"))
    _youtube(monkeypatch, 404)
    with pytest.raises(ValueError, match="can't find"):
        _run(V.check_youtube("https://youtu.be/dQw4w9WgXcQ", "demand_gen"))
    with pytest.raises(ValueError, match="doesn't look like a YouTube link"):
        _run(V.check_youtube("https://example.com/video", "demand_gen"))


# ── an uploaded video onto YouTube, through Google Ads ─────────────────────────────

class _Adapter:
    customer_id = "123"

    def __init__(self, states):
        self.states, self.uploads = list(states), []

    async def upload_youtube_video(self, url, title):
        self.uploads.append((url, title))
        return "customers/123/youTubeVideoUploads/9"

    async def youtube_upload_state(self, resource):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def _settle():
    for _ in range(20):
        _run(asyncio.sleep(0))


def test_an_upload_is_launched_only_once_youtube_has_processed_it(monkeypatch):
    monkeypatch.setattr(V, "POLL_SECONDS", 0)
    db, adapter = AsyncMongoMockClient()["gv"], _Adapter([("UPLOADED", ""), ("PROCESSED", "abcDEF12345")])
    with pytest.raises(V.VideoNotReady):                                   # nothing started it yet: it starts now
        _run(V.ensure_ready(db, adapter, "https://cdn/v.mp4", "Weekend <offer>"))
    _settle()
    assert adapter.uploads == [("https://cdn/v.mp4", "Weekend <offer>")]
    assert _run(V.ensure_ready(db, adapter, "https://cdn/v.mp4")) == "abcDEF12345"
    # The same video again is not uploaded twice.
    _run(V.prepare(db, adapter, "https://cdn/v.mp4"))
    assert len(adapter.uploads) == 1


def test_a_video_youtube_refuses_is_reported_and_not_retried_forever(monkeypatch):
    monkeypatch.setattr(V, "POLL_SECONDS", 0)
    db, adapter = AsyncMongoMockClient()["gv2"], _Adapter([("REJECTED", "")])
    _run(V.prepare(db, adapter, "https://cdn/bad.mp4"))
    _settle()
    with pytest.raises(V.VideoFailed, match="rejected"):
        _run(V.ensure_ready(db, adapter, "https://cdn/bad.mp4"))


def test_the_launch_asks_google_itself_when_the_worker_that_was_waiting_went_away():
    db, adapter = AsyncMongoMockClient()["gv3"], _Adapter([("PROCESSED", "zzzYYY11111")])
    _run(db[V.UPLOADS].insert_one({"upload_id": "gvu_1", "customer_id": "123", "video_url": "https://cdn/v.mp4",
                                   "status": "processing", "resource_name": "customers/123/youTubeVideoUploads/9",
                                   "created_at": V._now(), "updated_at": V._now()}))
    assert _run(V.ensure_ready(db, adapter, "https://cdn/v.mp4")) == "zzzYYY11111"


# ── the ads Google gets ───────────────────────────────────────────────────────────

COPY = {"headlines": ["Fresh bread daily"], "long_headline": "Fresh bread, baked every morning in Lekki",
        "descriptions": ["Order by 9am"], "business_name": "Magen Bakery"}
IMAGES = {"landscape": "L", "square": "S", "portrait": "P"}


def _ops(placement, images, video="abcDEF12345"):
    return gv.build_operations(placement, "123", name="Magen", daily_micros="1000", geo_constant="geoTargetConstants/1",
                               final_url="https://magen.ng", copy=COPY, images=images, logo_b64="LOGO",
                               youtube_video_id=video)


def _assets(ops):
    return [o["assetOperation"]["create"] for o in ops if "assetOperation" in o]


def test_youtube_and_discover_runs_the_video_as_a_video_ad_without_pictures():
    ops = _ops("demand_gen", None)
    assert [a["youtubeVideoAsset"]["youtubeVideoId"] for a in _assets(ops) if "youtubeVideoAsset" in a] == ["abcDEF12345"]
    assert [a["name"].split()[-2] for a in _assets(ops) if "imageAsset" in a] == ["logo"]   # only the logo
    ad = next(o for o in ops if "adGroupAdOperation" in o)["adGroupAdOperation"]["create"]["ad"]
    video_ad = ad["demandGenVideoResponsiveAd"]
    assert video_ad["businessName"] == {"text": "Magen Bakery"}
    assert video_ad["longHeadlines"] == [{"text": COPY["long_headline"]}] and len(video_ad["videos"]) == 1
    assert ad["finalUrls"] == ["https://magen.ng"]


def test_display_and_everywhere_run_the_video_beside_the_pictures():
    display = next(o for o in _ops("display", IMAGES) if "adGroupAdOperation" in o)
    rda = display["adGroupAdOperation"]["create"]["ad"]["responsiveDisplayAd"]
    assert rda["youtubeVideos"] and rda["marketingImages"]
    pmax = _ops("pmax", IMAGES)
    fields = [o["assetGroupAssetOperation"]["create"]["fieldType"] for o in pmax if "assetGroupAssetOperation" in o]
    assert "YOUTUBE_VIDEO" in fields and "MARKETING_IMAGE" in fields
    with pytest.raises(ValueError):
        _ops("pmax", None)                                    # Everywhere still needs a picture


def test_without_a_video_the_picture_ads_are_unchanged():
    ops = _ops("demand_gen", IMAGES, video="")
    assert not any("youtubeVideoAsset" in a for a in _assets(ops))
    ad = next(o for o in ops if "adGroupAdOperation" in o)["adGroupAdOperation"]["create"]["ad"]
    assert "demandGenMultiAssetAd" in ad


def test_the_launch_says_its_still_getting_the_video_ready(monkeypatch):
    """The wizard waits on this exact answer and launches again (409 + these words)."""
    from fastapi import HTTPException
    from app.agents.jane_ads import router as R

    class _A:
        async def launch_campaign(self, plan, auth):
            raise V.VideoNotReady(V.PREPARING)

    async def _adapter(db, ctx):
        return _A(), {}

    class _Wallet:
        def __init__(self, *a):
            pass

        async def authorization_for(self, *a, **k):
            return None

    monkeypatch.setattr("app.agents.jane_ads.google_flow.adapter_for_launch", _adapter)
    monkeypatch.setattr("app.agents.jane_ads.wallet.WalletService", _Wallet)
    from app.agents.jane_ads.models import Platform
    built = SimpleNamespace(plan=SimpleNamespace(platforms=[SimpleNamespace(platform=Platform.GOOGLE)]),
                            req=SimpleNamespace(budget_ngn=10_000), business_id="b1")
    with pytest.raises(HTTPException) as e:
        _run(R._do_launch(built, "", "", {"brand_id": "b1"}, AsyncMongoMockClient()["gv4"]))
    assert e.value.status_code == 409 and "Getting your video ready on YouTube" in e.value.detail
