"""
Unit tests for the Google Ads adapter (adapters/google.py).

httpx is mocked throughout — these prove the adapter builds correct requests and
handles responses/validation/rollback correctly, not that Google's live API behaves
as documented (no credentials exist yet — see the Phase-1 plan). Follows the exact
convention established by test_jane_ads_meta_adapter.py: no pytest-asyncio, a manual
_run() helper, a hand-rolled FakeDb, unittest.mock.patch("httpx.AsyncClient") with a
queued-JSON-responses helper. The keyword-generation OpenAI call is mocked the same
way test_jane_ads_creative.py mocks creative.py's _call_ad_copy_model — by patching
the module-level helper function directly, not the OpenAI client.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads.adapters.google import (
    GoogleAdsAdapter,
    GoogleAdsAPIError,
    LowClicksWarning,
    _validate_keywords,
    estimate_clicks_per_day,
    translate_keywords,
)
from app.agents.jane_ads.models import (
    ABTestScope,
    AdCreative,
    CampaignObjective,
    CampaignPlan,
    Goal,
    Platform,
    PlatformPlan,
    PurchaseBehaviour,
    SpendAuthorization,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _developer_token(monkeypatch):
    """The constructor always reads GOOGLE_ADS_DEVELOPER_TOKEN from real settings
    (it's a global property of URI's own Google Cloud project, not per-brand, so it's
    deliberately not a constructor parameter) — this environment's real .env has no
    value for it, so every test needs one patched in."""
    monkeypatch.setattr("app.agents.jane_ads.adapters.google.settings.GOOGLE_ADS_DEVELOPER_TOKEN", "dev_tok_test")


class FakeCollection:
    def __init__(self):
        self.docs: dict = {}

    async def find_one(self, query):
        cid = query.get("campaign_id")
        return dict(self.docs[cid]) if cid in self.docs else None

    async def update_one(self, query, update, upsert=False):
        cid = query["campaign_id"]
        existing = self.docs.get(cid, {})
        existing.update(update.get("$set", {}))
        self.docs[cid] = existing


class FakeDb:
    def __init__(self):
        self._coll = FakeCollection()

    def __getitem__(self, name):
        return self._coll


def _plan(**kw) -> CampaignPlan:
    base = dict(
        business_id="b1", goal=Goal.LEADS, behaviour=PurchaseBehaviour.SEARCH,
        platforms=[PlatformPlan(platform=Platform.GOOGLE, budget_ngn=70_000, days=7,
                                variants=1, test_scope=ABTestScope.NONE,
                                objective=CampaignObjective.CONVERSATIONS)],
        per_business_cap_ngn=70_000, account_cap_ngn=70_000,
        whatsapp_number="2348031234567",
        google_keyword_context={"category": "plumbing", "description": "Plumbing repairs and installs", "geo": "Surulere"},
    )
    base.update(kw)
    return CampaignPlan(**base)


def _auth(funded=70_000.0) -> SpendAuthorization:
    return SpendAuthorization(business_id="b1", funded_amount_ngn=funded, account_cap_ngn=funded)


def _mock_client(responses):
    client = AsyncMock()
    resp_iter = iter(responses)

    async def _next(*a, **kw):
        r = AsyncMock()
        r.json = lambda: next(resp_iter)
        return r

    client.post = AsyncMock(side_effect=_next)
    return client


def _adapter(db=None) -> GoogleAdsAdapter:
    return GoogleAdsAdapter(db or FakeDb(), customer_id="1234567890", login_customer_id="9999999999", access_token="tok")


_KEYWORD_SET = {
    "head_terms": ["plumbing repair", "pipe installation"],
    "geo_terms": ["plumber Surulere"],
    "intent_terms": ["emergency plumber"],
    "negatives": ["jobs", "salary", "training", "free", "diy", "wholesale"],
}

# campaign budget, campaign, geo, ad group, keywords, ad — 6 calls total.
_HAPPY_RESPONSES = [
    {"results": [{"resourceName": "customers/1234567890/campaignBudgets/1"}]},
    {"results": [{"resourceName": "customers/1234567890/campaigns/111"}]},
    {"results": [{"resourceName": "customers/1234567890/campaignCriteria/222"}]},
    {"results": [{"resourceName": "customers/1234567890/adGroups/333"}]},
    {"results": [{"resourceName": "customers/1234567890/adGroupCriteria/444"}]},
    {"results": [{"resourceName": "customers/1234567890/adGroupAds/333~555"}]},
]


def test_requires_customer_id():
    with pytest.raises(GoogleAdsAPIError):
        GoogleAdsAdapter(FakeDb(), customer_id="", login_customer_id="9999999999", access_token="tok")


# ── estimate_clicks_per_day (pure) ──────────────────────────────────────────────

def test_estimate_clicks_per_day_arithmetic():
    assert estimate_clicks_per_day(10_000, 1_000) == 10.0
    assert estimate_clicks_per_day(9_000, 1_000) == 9.0
    assert estimate_clicks_per_day(100, 0) == float("inf")
    assert estimate_clicks_per_day(100, -5) == float("inf")


# ── _validate_keywords (pure) ────────────────────────────────────────────────────

def test_validate_keywords_flags_missing_standing_negatives():
    issues = _validate_keywords({"head_terms": ["plumber"], "negatives": []}, "plumbing")
    assert any("negative" in i for i in issues)


def test_validate_keywords_exempts_category_matching_a_standing_negative():
    # A training business shouldn't be forced to negative "training".
    issues = _validate_keywords(
        {"head_terms": ["plumbing training"], "negatives": ["jobs", "salary", "free", "diy", "wholesale"]},
        "plumbing training courses",
    )
    assert not any("training" in i for i in issues)


def test_validate_keywords_empty_head_terms_fails():
    issues = _validate_keywords({"head_terms": [], "negatives": sorted({"jobs", "salary", "training", "free", "diy", "wholesale"})}, "plumbing")
    assert any("head_terms" in i for i in issues)


# ── translate_keywords retry ─────────────────────────────────────────────────────

def test_translate_keywords_valid_first_try_no_retry():
    mock_call = AsyncMock(return_value=dict(_KEYWORD_SET))
    with patch("app.agents.jane_ads.adapters.google._call_keyword_model", new=mock_call):
        result = _run(translate_keywords("plumbing", "Plumbing repairs", "Surulere"))
    assert mock_call.call_count == 1
    assert result["head_terms"] == _KEYWORD_SET["head_terms"]


def test_translate_keywords_retries_once_on_validation_failure():
    bad = {"head_terms": [], "geo_terms": [], "intent_terms": [], "negatives": []}
    good = dict(_KEYWORD_SET)
    mock_call = AsyncMock(side_effect=[bad, good])
    with patch("app.agents.jane_ads.adapters.google._call_keyword_model", new=mock_call):
        result = _run(translate_keywords("plumbing", "Plumbing repairs", "Surulere"))
    assert mock_call.call_count == 2
    # The correction prompt (second call's argument) names the specific failure.
    second_prompt = mock_call.call_args_list[1].args[0]
    assert "head_terms" in second_prompt
    assert result["head_terms"] == _KEYWORD_SET["head_terms"]


def test_translate_keywords_always_includes_standing_negatives_even_on_total_failure():
    mock_call = AsyncMock(return_value=None)
    with patch("app.agents.jane_ads.adapters.google._call_keyword_model", new=mock_call):
        result = _run(translate_keywords("plumbing", "Plumbing repairs", "Surulere"))
    assert set(result["negatives"]) >= {"jobs", "salary", "training", "free", "diy", "wholesale"}


# ── launch_campaign ───────────────────────────────────────────────────────────────

def _patched_keywords():
    return patch("app.agents.jane_ads.adapters.google.translate_keywords", new=AsyncMock(return_value=dict(_KEYWORD_SET)))


def test_launch_campaign_happy_path_full_call_sequence():
    db = FakeDb()
    adapter = _adapter(db)
    with _patched_keywords(), patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(_plan(), _auth()))

    assert result.campaign_id == "111"
    assert result.ad_ids == {"b1": "555"}
    assert result.platforms == [Platform.GOOGLE]
    assert mock_client.post.call_count == 6

    campaign_json = mock_client.post.call_args_list[1].kwargs["json"]["operations"][0]["create"]
    assert campaign_json["status"] == "PAUSED"
    assert campaign_json["advertisingChannelType"] == "SEARCH"
    assert campaign_json["networkSettings"]["targetGoogleSearch"] is True
    assert campaign_json["networkSettings"]["targetSearchNetwork"] is False

    adgroup_json = mock_client.post.call_args_list[3].kwargs["json"]["operations"][0]["create"]
    assert adgroup_json["status"] == "PAUSED"

    keyword_ops = mock_client.post.call_args_list[4].kwargs["json"]["operations"]
    match_types = [op["create"]["keyword"]["matchType"] for op in keyword_ops]
    assert "BROAD" not in match_types
    assert set(match_types) <= {"PHRASE", "EXACT"}
    negative_terms = {op["create"]["keyword"]["text"] for op in keyword_ops if op["create"].get("negative")}
    assert {"jobs", "salary", "training", "free", "diy", "wholesale"} <= negative_terms

    ad_json = mock_client.post.call_args_list[5].kwargs["json"]["operations"][0]["create"]
    assert ad_json["status"] == "PAUSED"
    assert ad_json["ad"]["finalUrls"] == ['https://wa.me/2348031234567?text=Hi%21%20I%20saw%20your%20ad%20and%20I%27m%20interested%20%E2%80%94%20tell%20me%20more%3F']

    record = _run(db["jane_ads_google_campaigns"].find_one({"campaign_id": "111"}))
    assert record["ad_id"] == "555"
    assert record["business_id"] == "b1"
    assert record["last_click_count"] == 0


def test_launch_campaign_missing_whatsapp_raises_before_any_http_call():
    adapter = _adapter()
    plan = _plan(whatsapp_number="")
    with _patched_keywords(), patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(ValueError):
            _run(adapter.launch_campaign(plan, _auth()))
    assert mock_client.post.call_count == 0


def test_launch_campaign_without_creative_uses_keyword_fallback_headline():
    adapter = _adapter()
    plan = _plan(creative=None)
    with _patched_keywords(), patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(plan, _auth()))
    assert result.launched is True
    ad_json = mock_client.post.call_args_list[5].kwargs["json"]["operations"][0]["create"]
    headlines = [h["text"] for h in ad_json["ad"]["responsiveSearchAd"]["headlines"]]
    # Derived from _KEYWORD_SET's head_terms, not a hardcoded default.
    assert any("Plumbing" in h or "Pipe" in h for h in headlines)


def test_launch_campaign_with_creative_uses_its_headline():
    adapter = _adapter()
    plan = _plan(creative=AdCreative(headline="Fast Plumbing Fixes", primary_text="Call us today"))
    with _patched_keywords(), patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(adapter.launch_campaign(plan, _auth()))
    ad_json = mock_client.post.call_args_list[5].kwargs["json"]["operations"][0]["create"]
    headlines = [h["text"] for h in ad_json["ad"]["responsiveSearchAd"]["headlines"]]
    assert headlines == ["Fast Plumbing Fixes"]


def test_launch_campaign_raises_low_clicks_warning_before_any_http_call():
    adapter = _adapter()
    # ₦100/day at an assumed ₦250 CPC = 0.4 clicks/day, well under the 10/day floor.
    plan = _plan(platforms=[PlatformPlan(
        platform=Platform.GOOGLE, budget_ngn=700, days=7, variants=1,
        test_scope=ABTestScope.NONE, objective=CampaignObjective.CONVERSATIONS,
    )])
    with _patched_keywords(), patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(LowClicksWarning) as exc_info:
            _run(adapter.launch_campaign(plan, _auth(funded=700)))
    assert mock_client.post.call_count == 0
    assert exc_info.value.estimated_clicks_per_day < 10


def test_launch_campaign_raises_on_google_error():
    adapter = _adapter()
    with _patched_keywords(), patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"error": {"message": "Invalid budget"}}])
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(GoogleAdsAPIError):
            _run(adapter.launch_campaign(_plan(), _auth()))


def test_launch_campaign_rolls_back_partial_launch_on_failure_and_preserves_original_error():
    adapter = _adapter()
    # Budget + campaign succeed, geo targeting fails -> rollback should REMOVE the
    # already-created campaign, and the ORIGINAL error (not a rollback error) propagates.
    responses = [
        {"results": [{"resourceName": "customers/1234567890/campaignBudgets/1"}]},
        {"results": [{"resourceName": "customers/1234567890/campaigns/111"}]},
        {"error": {"message": "geo targeting rejected: invalid geoTargetConstant"}},
        {"success": True},  # the rollback's own REMOVED-status call
    ]
    with _patched_keywords(), patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(GoogleAdsAPIError, match="geo targeting"):
            _run(adapter.launch_campaign(_plan(), _auth()))

    rollback_json = mock_client.post.call_args_list[3].kwargs["json"]["operations"][0]
    assert rollback_json["update"]["status"] == "REMOVED"
    assert rollback_json["update"]["resourceName"] == "customers/1234567890/campaigns/111"


# ── fetch_per_ad_spend ──────────────────────────────────────────────────────────

def test_fetch_per_ad_spend_converts_micros_to_ngn():
    db = FakeDb()
    db["jane_ads_google_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "555"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client([{
            "results": [{
                "adGroupAd": {"ad": {"id": "555"}},
                "metrics": {"costMicros": "12500000"},
                "customer": {"currencyCode": "NGN"},
            }],
        }])
        spend = _run(adapter.fetch_per_ad_spend("111"))
    assert spend[0].spend_ngn == 12.5
    assert spend[0].platform == Platform.GOOGLE


def test_fetch_per_ad_spend_converts_usd_via_constants():
    from app.agents.jane_ads import constants as C
    db = FakeDb()
    db["jane_ads_google_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "555"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client([{
            "results": [{
                "adGroupAd": {"ad": {"id": "555"}},
                "metrics": {"costMicros": "1000000"},
                "customer": {"currencyCode": "USD"},
            }],
        }])
        spend = _run(adapter.fetch_per_ad_spend("111"))
    assert spend[0].spend_ngn == 1.0 * C.USD_TO_NGN


# ── poll_conversations (delta tracking) ──────────────────────────────────────────

def test_poll_conversations_returns_only_new_clicks_since_last_poll():
    db = FakeDb()
    db["jane_ads_google_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "555", "last_click_count": 3}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client([{
            "results": [{"metrics": {"clicks": "8", "costMicros": "4000000"}}],
        }])
        events = _run(adapter.poll_conversations("111"))
    assert len(events) == 5  # 8 - 3
    assert db["jane_ads_google_campaigns"].docs["111"]["last_click_count"] == 8


def test_poll_conversations_returns_empty_when_no_new_clicks():
    db = FakeDb()
    db["jane_ads_google_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "555", "last_click_count": 8}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client([{
            "results": [{"metrics": {"clicks": "8", "costMicros": "4000000"}}],
        }])
        events = _run(adapter.poll_conversations("111"))
    assert events == []


# ── pause_ad ──────────────────────────────────────────────────────────────────────

def test_pause_ad_sends_update_mask():
    db = FakeDb()
    db["jane_ads_google_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "555", "ad_group_id": "customers/1234567890/adGroups/333"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"results": [{"resourceName": "..."}]}])
        MockClient.return_value.__aenter__.return_value = mock_client
        ok = _run(adapter.pause_ad("111", "555"))
    assert ok is True
    sent = mock_client.post.call_args.kwargs["json"]["operations"][0]
    assert sent["update"]["status"] == "PAUSED"
    assert sent["updateMask"] == "status"
