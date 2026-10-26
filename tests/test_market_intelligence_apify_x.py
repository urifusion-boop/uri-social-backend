"""
X through Apify: the query each keyword becomes, how a tweet becomes evidence,
and the start -> wait -> page-through flow against a fake Apify API (no money
spent, no network). Plus the Playbook starter topic's no-AI fallback.
"""
import asyncio
import json
from datetime import datetime

import httpx
import pytest

from app.agents.market_intelligence.adapters import apify_x as ax
from app.agents.market_intelligence.models import ScanStatus
from app.agents.market_intelligence import brand_topic


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


TWEET = {
    "type": "tweet", "id": "1846", "url": "https://x.com/amaka/status/1846",
    "text": "Please who sells original vitamin C serum in Lekki? Need it today",
    "createdAt": "Thu Oct 15 09:30:41 +0000 2026", "lang": "en",
    "likeCount": 4, "replyCount": 2, "retweetCount": 0, "isReply": False, "conversationId": "1846",
    "author": {"userName": "amaka", "location": "Lagos, Nigeria", "isBlueVerified": False},
}


class TestQuery:
    def test_each_keyword_carries_window_language_and_exclusions(self):
        terms = ax.build_search_terms(
            ["vitamin c serum", "Glowup", "glowup"], ["giveaway"],
            datetime(2026, 10, 9), datetime(2026, 10, 16), language="en")
        assert terms == [
            '(vitamin c serum) since:2026-10-09 until:2026-10-16 -filter:retweets lang:en -giveaway',
            'Glowup since:2026-10-09 until:2026-10-16 -filter:retweets lang:en -giveaway',
        ]  # duplicate keyword (any case) searched once

    def test_quoted_keyword_stays_exact_and_exclusion_phrase_is_exact(self):
        terms = ax.build_search_terms(['"glow up"'], ["free trial"], datetime(2026, 10, 9), datetime(2026, 10, 16))
        assert terms[0].startswith('"glow up" ') and terms[0].endswith('-"free trial"')

    def test_no_language_means_no_filter(self):
        terms = ax.build_search_terms(["skincare lagos"], [], datetime(2026, 10, 1), datetime(2026, 10, 8))
        assert "lang:" not in terms[0]


class TestToEvidence:
    def test_tweet_fields(self):
        ev = ax.to_evidence(TWEET, "x", datetime(2026, 10, 16))
        assert ev.source_id == "x:1846" and ev.platform == "x" and ev.provider == "x"
        assert ev.author_handle == "@amaka" and ev.url.endswith("/1846")
        assert ev.published_at == datetime(2026, 10, 15, 9, 30, 41)
        assert ev.raw_metrics == {"likes": 4, "replies": 2, "reposts": 0}
        # profile location is a source field, never the post's own stated location
        assert ev.geography.source_field_location == "Lagos, Nigeria"
        assert ev.geography.explicit_location is None
        assert ev.parent_id is None  # its own conversation

    def test_reply_points_at_its_thread(self):
        ev = ax.to_evidence({**TWEET, "id": "1900", "isReply": True, "conversationId": "1846"}, "x", datetime.utcnow())
        assert ev.content_type == "reply" and ev.parent_id == "1846"

    def test_placeholder_rows_are_skipped(self):
        assert ax.to_evidence({"noResults": True}, "x", datetime.utcnow()) is None
        assert ax.to_evidence({"id": "1", "text": ""}, "x", datetime.utcnow()) is None


def _fake_apify(n_items: int, statuses=("RUNNING", "SUCCEEDED"), usage=0.031):
    calls = {"start": [], "status": 0, "pages": []}
    items = [{**TWEET, "id": str(1000 + i), "text": f"tweet {i}"} for i in range(n_items)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer apify_api_test"
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            calls["start"].append((path, dict(request.url.params), json.loads(request.content)))
            return httpx.Response(201, json={"data": {"id": "run1", "status": "READY", "defaultDatasetId": "ds1"}})
        if path == "/v2/actor-runs/run1":
            st = statuses[min(calls["status"], len(statuses) - 1)]
            calls["status"] += 1
            return httpx.Response(200, json={"data": {"id": "run1", "status": st, "defaultDatasetId": "ds1",
                                                      "usageTotalUsd": usage}})
        if path == "/v2/datasets/ds1/items":
            off, lim = int(request.url.params["offset"]), int(request.url.params["limit"])
            calls["pages"].append(off)
            return httpx.Response(200, json=items[off:off + lim])
        return httpx.Response(404)

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return factory, calls


class TestFlow:
    def test_start_wait_and_page_through_everything(self, monkeypatch):
        monkeypatch.setattr(ax, "POLL_SECONDS", 0)
        factory, calls = _fake_apify(n_items=450)
        adapter = ax.ApifyXAdapter(ax.TEST_PROFILE, token="apify_api_test", client_factory=factory)

        async def go():
            run_id = await adapter.start_collection(["serum", "skincare"], [], datetime(2026, 10, 9), datetime(2026, 10, 16))
            got, cursor, pages = [], None, 0
            while True:
                page = await adapter.fetch_page(run_id, cursor)
                got += page.evidence
                pages += 1
                if not page.has_more:
                    break
                cursor = page.next_cursor
            return run_id, got, pages, await adapter.actual_cost(run_id)

        run_id, got, pages, cost = _run(go())
        assert run_id == "run1"
        assert len(got) == 450 and pages == 3 and calls["pages"] == [0, 200, 400]
        path, params, body = calls["start"][0]
        assert path == "/v2/acts/kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest/runs"
        assert body["searchTerms"][0].startswith("serum since:2026-10-09")
        assert body["maxItems"] == 100 and body["queryType"] == "Latest"
        assert float(params["maxTotalChargeUsd"]) <= 0.15   # hard spend cap on every run
        assert int(params["maxItems"]) == 200
        assert cost == pytest.approx(0.031)

    def test_pro_actor_input(self, monkeypatch):
        factory, calls = _fake_apify(n_items=0, statuses=("SUCCEEDED",))
        adapter = ax.ApifyXAdapter(ax.PRO_PROFILE, token="apify_api_test", client_factory=factory)
        _run(adapter.start_collection(["serum"], [], datetime(2026, 10, 9), datetime(2026, 10, 16)))
        path, _, body = calls["start"][0]
        assert path.endswith("/apidojo~tweet-scraper/runs")
        assert body["maxItems"] == 100 and body["sort"] == "Latest"

    def test_location_near_and_radius_are_passed_through_on_the_test_actor(self, monkeypatch):
        factory, calls = _fake_apify(n_items=0, statuses=("SUCCEEDED",))
        adapter = ax.ApifyXAdapter(ax.TEST_PROFILE, token="apify_api_test", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], datetime(2026, 10, 9), datetime(2026, 10, 16),
                                      location={"country": "NG", "x_near": "Lagos, Nigeria", "x_radius_km": 30}))
        _, _, body = calls["start"][0]
        assert body["near"] == "Lagos, Nigeria" and body["within"] == "30km"

    def test_location_uses_geotaggedNear_on_the_pro_actor(self, monkeypatch):
        factory, calls = _fake_apify(n_items=0, statuses=("SUCCEEDED",))
        adapter = ax.ApifyXAdapter(ax.PRO_PROFILE, token="apify_api_test", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], datetime(2026, 10, 9), datetime(2026, 10, 16),
                                      location={"country": "NG", "x_near": "Lagos, Nigeria", "x_radius_km": 30}))
        _, _, body = calls["start"][0]
        assert body["geotaggedNear"] == "Lagos, Nigeria" and body["withinRadius"] == "30km"

    def test_no_location_means_no_geo_fields_sent(self, monkeypatch):
        factory, calls = _fake_apify(n_items=0, statuses=("SUCCEEDED",))
        adapter = ax.ApifyXAdapter(ax.TEST_PROFILE, token="apify_api_test", client_factory=factory)
        _run(adapter.start_collection(["wig"], [], datetime(2026, 10, 9), datetime(2026, 10, 16)))
        _, _, body = calls["start"][0]
        assert "near" not in body and "within" not in body

    def test_failed_run_is_reported_not_hidden(self, monkeypatch):
        monkeypatch.setattr(ax, "POLL_SECONDS", 0)
        factory, _ = _fake_apify(n_items=3, statuses=("FAILED",))
        adapter = ax.ApifyXAdapter(ax.TEST_PROFILE, token="apify_api_test", client_factory=factory)

        async def go():
            run_id = await adapter.start_collection(["serum"], [], datetime(2026, 10, 9), datetime(2026, 10, 16))
            return await adapter.fetch_page(run_id)
        page = _run(go())
        assert len(page.evidence) == 3
        assert any("FAILED" in g for g in page.gaps)

    def test_status_mapping(self):
        factory, _ = _fake_apify(n_items=0, statuses=("RUNNING",))
        adapter = ax.ApifyXAdapter(ax.TEST_PROFILE, token="apify_api_test", client_factory=factory)
        assert _run(adapter.get_status("run1")) == ScanStatus.COLLECTING

    def test_no_token_says_so(self, monkeypatch):
        monkeypatch.setattr(ax.settings, "APIFY_API_TOKEN", None)
        adapter = ax.ApifyXAdapter(ax.TEST_PROFILE)
        with pytest.raises(RuntimeError, match="APIFY_API_TOKEN"):
            _run(adapter.start_collection(["x"], [], datetime(2026, 10, 9), datetime(2026, 10, 16)))

    def test_estimate_matches_the_cap(self):
        adapter = ax.ApifyXAdapter(ax.TEST_PROFILE, token="t")
        assert _run(adapter.estimate_cost(["a", "b", "c"], 7)) == pytest.approx(300 * 0.25 / 1000 + 0.01)


class TestBrandTopic:
    PROFILE = {
        "brand_name": "Glowup", "industry": "Skincare", "key_products_services": ["vitamin C serum", "sunscreen"],
        "customer_location": "Lagos, Nigeria", "competitor_handles": ["@skinbyzara", "Nivea"],
        "languages": ["English", "Pidgin"],
    }

    def test_built_only_from_the_playbook_when_ai_is_down(self, monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("no AI")
        monkeypatch.setattr(brand_topic.AIService, "structured_chat_completion", boom)
        out = _run(brand_topic.suggest_brand_topic(self.PROFILE))
        assert "Lagos, Nigeria" in out["question"]
        assert out["keywords"][0] == "Glowup" and "vitamin C serum" in out["keywords"]
        assert out["geographic_scope"] == "Lagos, Nigeria"
        assert out["competitors"] == ["skinbyzara", "Nivea"]
        assert out["languages"] == ["en", "pcm"]
        assert out["keep_updating"] is True and out["requested_days"] == 7

    def test_empty_playbook_gives_nothing_to_invent_from(self):
        assert _run(brand_topic.suggest_brand_topic({})) is None
