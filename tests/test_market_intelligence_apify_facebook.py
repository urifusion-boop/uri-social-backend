"""
Facebook through Apify: the flat term list build_search_terms produces, how
a post becomes evidence, and the start -> wait -> page-through flow against
a fake Apify API (no money spent, no network).
"""
import asyncio
import json
from datetime import datetime

import httpx
import pytest

from app.agents.market_intelligence.adapters import apify_facebook as fb
from app.agents.market_intelligence.models import ScanStatus


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


POST = {
    "postId": "fb123", "text": "Anyone know a trusted vendor for original vitamin C serum in Lekki?",
    "url": "https://facebook.com/posts/fb123", "author": "Amaka Okafor",
    "time": "2026-10-15T09:30:41Z", "likes": 6, "comments": 3, "shares": 1,
}


class TestQuery:
    def test_terms_are_flat_deduped_and_hashtag_stripped(self):
        terms = fb.build_search_terms(["vitamin c serum", "#Glowup", "glowup"], ["giveaway"],
                                      datetime(2026, 10, 9), datetime(2026, 10, 16))
        assert terms == ["vitamin c serum", "Glowup"]  # no boolean operators, no exclusion syntax

    def test_blank_terms_are_dropped(self):
        assert fb.build_search_terms(["", "  ", "wig"], [], datetime(2026, 10, 9), datetime(2026, 10, 16)) == ["wig"]


class TestToEvidence:
    def test_post_fields(self):
        ev = fb.to_evidence(POST, "facebook", datetime(2026, 10, 16))
        assert ev.source_id == "facebook:fb123" and ev.platform == "facebook" and ev.provider == "facebook"
        assert ev.author_handle == "Amaka Okafor"
        assert ev.url == "https://facebook.com/posts/fb123"
        assert ev.published_at == datetime(2026, 10, 15, 9, 30, 41)
        assert ev.raw_metrics == {"likes": 6, "replies": 3, "reposts": 1}

    def test_rows_with_no_text_or_id_are_skipped(self):
        assert fb.to_evidence({"postId": "1", "text": ""}, "facebook", datetime.utcnow()) is None
        assert fb.to_evidence({"text": "hello"}, "facebook", datetime.utcnow()) is None  # no postId


def _fake_apify(n_items: int, statuses=("RUNNING", "SUCCEEDED"), usage=0.09):
    calls = {"start": [], "status": 0, "pages": []}
    items = [{**POST, "postId": str(1000 + i), "text": f"post {i}"} for i in range(n_items)]

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
        monkeypatch.setattr(fb, "POLL_SECONDS", 0)
        factory, calls = _fake_apify(n_items=220)
        adapter = fb.ApifyFacebookAdapter(fb.TEST_PROFILE, token="apify_api_test", client_factory=factory)

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
        assert len(got) == 220 and pages == 2 and calls["pages"] == [0, 200]
        path, params, body = calls["start"][0]
        assert path == "/v2/acts/scrapeflow~facebook-posts-search-scraper/runs"
        assert body["searchQueries"] == ["serum", "skincare"]
        assert body["maxPostsPerQuery"] == 30 and body["recentPostsOnly"] is True
        assert int(params["maxItems"]) == 60  # 30 per query x 2 queries
        assert cost == pytest.approx(0.09)

    def test_pro_profile_uses_a_higher_cap(self):
        factory, calls = _fake_apify(n_items=0, statuses=("SUCCEEDED",))
        adapter = fb.ApifyFacebookAdapter(fb.PRO_PROFILE, token="apify_api_test", client_factory=factory)
        _run(adapter.start_collection(["serum"], [], datetime(2026, 10, 9), datetime(2026, 10, 16)))
        path, _, body = calls["start"][0]
        assert path.endswith("/scrapeflow~facebook-posts-search-scraper/runs")
        assert body["maxPostsPerQuery"] == 50

    def test_failed_run_is_reported_not_hidden(self, monkeypatch):
        monkeypatch.setattr(fb, "POLL_SECONDS", 0)
        factory, _ = _fake_apify(n_items=3, statuses=("FAILED",))
        adapter = fb.ApifyFacebookAdapter(fb.TEST_PROFILE, token="apify_api_test", client_factory=factory)

        async def go():
            run_id = await adapter.start_collection(["serum"], [], datetime(2026, 10, 9), datetime(2026, 10, 16))
            return await adapter.fetch_page(run_id)
        page = _run(go())
        assert len(page.evidence) == 3
        assert any("FAILED" in g for g in page.gaps)

    def test_status_mapping(self):
        factory, _ = _fake_apify(n_items=0, statuses=("RUNNING",))
        adapter = fb.ApifyFacebookAdapter(fb.TEST_PROFILE, token="apify_api_test", client_factory=factory)
        assert _run(adapter.get_status("run1")) == ScanStatus.COLLECTING

    def test_no_token_says_so(self, monkeypatch):
        monkeypatch.setattr(fb.settings, "APIFY_API_TOKEN", None)
        adapter = fb.ApifyFacebookAdapter(fb.TEST_PROFILE)
        with pytest.raises(RuntimeError, match="APIFY_API_TOKEN"):
            _run(adapter.start_collection(["x"], [], datetime(2026, 10, 9), datetime(2026, 10, 16)))

    def test_estimate_matches_the_cap(self):
        adapter = fb.ApifyFacebookAdapter(fb.TEST_PROFILE, token="apify_api_test")
        est = _run(adapter.estimate_cost(["serum", "skincare", "wig"], 7))
        assert est == pytest.approx(3 * 30 * 3.99 / 1000 + 0.01)
