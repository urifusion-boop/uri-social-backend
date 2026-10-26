"""
Phase F — per-source geo targeting on the three adapters with a real,
verified upstream lever (TikTok's native `location` field, Google News's
`edition`, Reddit's subreddit list) plus the no-op adapters that
intentionally ignore `location` (Instagram/YouTube/Facebook/TikTok-pro).
Against a fake Apify API; no money spent, no network.
"""
import asyncio
import json
from datetime import datetime

import httpx
import pytest

from app.agents.market_intelligence.adapters import apify_news as an
from app.agents.market_intelligence.adapters import apify_reddit as ar
from app.agents.market_intelligence.adapters import apify_tiktok as at
from app.agents.market_intelligence.adapters import apify_instagram as ai
from app.agents.market_intelligence.adapters import apify_youtube as ay
from app.agents.market_intelligence.adapters import apify_facebook as af


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_apify():
    calls = {"start": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/runs"):
            calls["start"].append((request.url.path, json.loads(request.content)))
            return httpx.Response(201, json={"data": {"id": "run1", "status": "READY", "defaultDatasetId": "ds1"}})
        return httpx.Response(404)

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return factory, calls


SINCE, UNTIL = datetime(2026, 10, 9), datetime(2026, 10, 16)


class TestTikTok:
    def test_cheap_profile_sets_native_location_field(self):
        factory, calls = _fake_apify()
        adapter = at.ApifyTikTokAdapter(at.TEST_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL, location={"country": "NG"}))
        _, body = calls["start"][0]
        assert body["location"] == "NG"

    def test_cheap_profile_uses_place_url_when_a_known_place_has_one(self):
        factory, calls = _fake_apify()
        adapter = at.ApifyTikTokAdapter(at.TEST_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL,
                                      location={"country": "NG", "tiktok_place_url": "https://www.tiktok.com/place/Lagos-123"}))
        _, body = calls["start"][0]
        assert body["startUrls"] == ["https://www.tiktok.com/place/Lagos-123"]

    def test_pro_profile_only_gets_proxy_country_not_native_location(self):
        factory, calls = _fake_apify()
        adapter = at.ApifyTikTokAdapter(at.PRO_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL, location={"country": "NG"}))
        _, body = calls["start"][0]
        assert body.get("proxyCountryCode") == "NG" and "location" not in body

    def test_no_location_sets_nothing(self):
        factory, calls = _fake_apify()
        adapter = at.ApifyTikTokAdapter(at.TEST_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL))
        _, body = calls["start"][0]
        assert "location" not in body and "proxyCountryCode" not in body


class TestNews:
    def test_edition_is_set_from_location(self):
        factory, calls = _fake_apify()
        adapter = an.ApifyNewsAdapter(an.TEST_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL, location={"country": "NG", "news_edition": "NG:en"}))
        _, body = calls["start"][0]
        assert body["edition"] == "NG:en"

    def test_no_location_means_no_edition_field(self):
        factory, calls = _fake_apify()
        adapter = an.ApifyNewsAdapter(an.TEST_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL))
        _, body = calls["start"][0]
        assert "edition" not in body


class TestReddit:
    def test_subreddits_from_location_are_merged_in(self):
        factory, calls = _fake_apify()
        adapter = ar.ApifyRedditAdapter(ar.TEST_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL,
                                      location={"country": "NG", "reddit_subreddits": ["lagos", "Nigeria"]}))
        _, body = calls["start"][0]
        assert body["subreddits"] == ["lagos", "Nigeria"]

    def test_no_subreddits_in_location_leaves_body_unchanged(self):
        factory, calls = _fake_apify()
        adapter = ar.ApifyRedditAdapter(ar.TEST_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL, location={"country": "NG"}))
        _, body = calls["start"][0]
        assert "subreddits" not in body


class TestNoOpAdapters:
    """No real upstream lever exists for these — `location` must be accepted
    (interface compatibility) but never raise, and never silently change
    anything it can't actually honor."""

    def test_instagram_accepts_and_ignores_location(self):
        factory, calls = _fake_apify()
        adapter = ai.ApifyInstagramAdapter(ai.TEST_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL, location={"country": "NG", "x_near": "Lagos"}))
        assert len(calls["start"]) == 1  # didn't raise

    def test_youtube_accepts_and_ignores_location(self):
        factory, calls = _fake_apify()
        adapter = ay.ApifyYouTubeAdapter(ay.TEST_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL, location={"country": "NG"}))
        assert len(calls["start"]) == 1

    def test_facebook_accepts_and_ignores_location(self):
        factory, calls = _fake_apify()
        adapter = af.ApifyFacebookAdapter(af.TEST_PROFILE, token="t", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], SINCE, UNTIL, location={"country": "NG"}))
        assert len(calls["start"]) == 1
