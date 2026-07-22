"""
Unit tests for the real Meta Marketing API adapter (split-doc 2.2).

httpx is mocked throughout — these prove the adapter builds correct requests and
handles responses/errors correctly, not that Meta's live API behaves as documented.
A real (paused, zero-spend) call against the actual Ad Account is how that gets
verified — see the session notes; this suite is the regression safety net.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads.adapters.meta import MetaAdPlatformAdapter, MetaAPIError
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
        business_id="b1", goal=Goal.MESSAGES, behaviour=PurchaseBehaviour.DISCOVER,
        platforms=[PlatformPlan(platform=Platform.META, budget_ngn=10_000, days=7,
                                variants=1, test_scope=ABTestScope.NONE,
                                objective=CampaignObjective.CONVERSATIONS)],
        per_business_cap_ngn=10_000, account_cap_ngn=10_000, page_id="pg123",
        creative=AdCreative(image_url="https://cdn/ad.jpg", headline="h", primary_text="p"),
    )
    base.update(kw)
    return CampaignPlan(**base)


def _auth(funded=10_000.0) -> SpendAuthorization:
    return SpendAuthorization(business_id="b1", funded_amount_ngn=funded, account_cap_ngn=funded)


def _mock_client(responses):
    """responses: list of dicts, consumed in order across POST/GET calls."""
    client = AsyncMock()
    resp_iter = iter(responses)

    async def _next(*a, **kw):
        r = AsyncMock()
        r.json = lambda: next(resp_iter)
        return r

    client.post = AsyncMock(side_effect=_next)
    client.get = AsyncMock(side_effect=_next)
    return client


def _adapter(db=None) -> MetaAdPlatformAdapter:
    return MetaAdPlatformAdapter(db or FakeDb(), ad_account_id="123", access_token="tok")


def test_requires_ad_account_id():
    # Explicit ad_account_id="" falls back to settings.META_AD_ACCOUNT_ID, which may be
    # genuinely set in this environment's .env — patch it out to test the guard itself.
    with patch("app.agents.jane_ads.adapters.meta.settings") as mock_settings:
        mock_settings.META_AD_ACCOUNT_ID = ""
        with pytest.raises(MetaAPIError):
            MetaAdPlatformAdapter(FakeDb(), ad_account_id="", access_token="tok")


def test_requires_access_token():
    # Explicit access_token="" falls back to settings.META_SYSTEM_TOKEN, which may be
    # genuinely set in this environment's .env — patch it out to test the guard itself.
    with patch("app.agents.jane_ads.adapters.meta.settings") as mock_settings:
        mock_settings.META_SYSTEM_TOKEN = ""
        with pytest.raises(MetaAPIError):
            MetaAdPlatformAdapter(FakeDb(), ad_account_id="123", access_token="")


def test_launch_campaign_requires_meta_platform():
    adapter = _adapter()
    plan = _plan(platforms=[PlatformPlan(platform=Platform.GOOGLE, budget_ngn=10_000, days=7,
                                         variants=1, test_scope=ABTestScope.NONE)])
    with pytest.raises(ValueError, match="only handles Platform.META"):
        _run(adapter.launch_campaign(plan, _auth()))


def test_launch_campaign_requires_page_id():
    adapter = _adapter()
    plan = _plan(page_id="")
    with pytest.raises(ValueError, match="page_id is required"):
        _run(adapter.launch_campaign(plan, _auth()))


def test_launch_campaign_requires_creative_image():
    adapter = _adapter()
    plan = _plan(creative=None)
    with pytest.raises(ValueError, match="creative.image_url is required"):
        _run(adapter.launch_campaign(plan, _auth()))


def test_launch_campaign_requires_creative_image_even_when_creative_present():
    adapter = _adapter()
    plan = _plan(creative=AdCreative(image_url="", headline="h"))
    with pytest.raises(ValueError, match="creative.image_url is required"):
        _run(adapter.launch_campaign(plan, _auth()))


def test_launch_campaign_uploads_video_and_builds_video_data_creative():
    db = FakeDb()
    adapter = _adapter(db)
    plan = _plan(creative=AdCreative(image_url="https://cdn/ad.mp4", is_video=True, headline="h", primary_text="p"))
    responses = [
        {"id": "vid_1"},                                                    # video upload
        {"status": {"video_status": "ready"}},                              # first poll — ready immediately
        {"data": [{"uri": "https://thumb/1.jpg", "is_preferred": True}]},   # thumbnails
        {"id": "cmp_1"},      # campaign
        {"id": "adset_1"},    # ad set
        {"id": "creative_1"}, # creative
        {"id": "ad_1"},       # ad
    ]
    with patch("httpx.AsyncClient") as MockClient, \
         patch("app.agents.jane_ads.adapters.meta.asyncio.sleep", new=AsyncMock()):
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(plan, _auth()))

    assert result.campaign_id == "cmp_1"
    # POST call order: advideos, campaigns, adsets, adcreatives, ads — the
    # ad-creative call must carry video_data, not link_data, with the uploaded
    # video_id and fetched thumbnail.
    creative_call = mock_client.post.call_args_list[3]
    spec = creative_call.kwargs["json"]["object_story_spec"]
    assert "video_data" in spec
    assert spec["video_data"]["video_id"] == "vid_1"
    assert spec["video_data"]["image_url"] == "https://thumb/1.jpg"


def test_launch_campaign_raises_when_video_processing_errors():
    adapter = _adapter()
    plan = _plan(creative=AdCreative(image_url="https://cdn/ad.mp4", is_video=True))
    responses = [
        {"id": "vid_1"},
        {"status": {"video_status": "error"}},
    ]
    with patch("httpx.AsyncClient") as MockClient, \
         patch("app.agents.jane_ads.adapters.meta.asyncio.sleep", new=AsyncMock()):
        MockClient.return_value.__aenter__.return_value = _mock_client(responses)
        with pytest.raises(MetaAPIError, match="failed to process"):
            _run(adapter.launch_campaign(plan, _auth()))


def test_launch_campaign_creates_full_chain_and_stores_record():
    db = FakeDb()
    adapter = _adapter(db)
    responses = [
        {"id": "cmp_1"},      # campaign
        {"id": "adset_1"},    # ad set
        {"id": "creative_1"}, # creative
        {"id": "ad_1"},       # ad
    ]
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(responses)
        result = _run(adapter.launch_campaign(_plan(), _auth()))

    assert result.campaign_id == "cmp_1"
    assert result.ad_ids == {"b1": "ad_1"}
    assert result.platforms == [Platform.META]

    record = _run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": "cmp_1"}))
    assert record["ad_id"] == "ad_1"
    assert record["business_id"] == "b1"
    assert record["last_conversation_count"] == 0


def test_launch_campaign_raises_on_meta_error():
    adapter = _adapter()
    responses = [{"error": {"message": "Invalid parameter", "code": 100}}]
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(responses)
        with pytest.raises(MetaAPIError, match="campaign creation"):
            _run(adapter.launch_campaign(_plan(), _auth()))


def test_fetch_per_ad_spend_returns_cumulative_totals():
    db = FakeDb()
    _run(db["jane_ads_meta_campaigns"].update_one(
        {"campaign_id": "cmp_1"},
        {"$set": {"campaign_id": "cmp_1", "ad_id": "ad_1", "business_id": "b1",
                   "last_conversation_count": 0}},
    ))
    adapter = _adapter(db)
    responses = [{"data": [{"ad_id": "ad_1", "spend": "1234.50"}]}]
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(responses)
        spends = _run(adapter.fetch_per_ad_spend("cmp_1"))

    assert len(spends) == 1
    assert spends[0].spend_ngn == 1234.50
    assert spends[0].business_id == "b1"


def test_fetch_per_ad_spend_unknown_campaign_raises():
    adapter = _adapter()
    with pytest.raises(MetaAPIError, match="No stored record"):
        _run(adapter.fetch_per_ad_spend("unknown_cmp"))


def test_poll_conversations_returns_only_the_delta():
    db = FakeDb()
    _run(db["jane_ads_meta_campaigns"].update_one(
        {"campaign_id": "cmp_1"},
        {"$set": {"campaign_id": "cmp_1", "ad_id": "ad_1", "business_id": "b1",
                   "last_conversation_count": 3}},
    ))
    adapter = _adapter(db)
    responses = [{"data": [{"spend": "1000", "actions": [
        {"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "5"},
    ]}]}]
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(responses)
        convos = _run(adapter.poll_conversations("cmp_1"))

    # 5 total reported, 3 already seen -> only 2 NEW events, not 5.
    assert len(convos) == 2
    assert all(c.business_id == "b1" and c.ad_id == "ad_1" for c in convos)

    record = _run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": "cmp_1"}))
    assert record["last_conversation_count"] == 5


def test_poll_conversations_returns_nothing_when_no_new_activity():
    db = FakeDb()
    _run(db["jane_ads_meta_campaigns"].update_one(
        {"campaign_id": "cmp_1"},
        {"$set": {"campaign_id": "cmp_1", "ad_id": "ad_1", "business_id": "b1",
                   "last_conversation_count": 5}},
    ))
    adapter = _adapter(db)
    responses = [{"data": [{"spend": "1000", "actions": [
        {"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "5"},
    ]}]}]
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(responses)
        convos = _run(adapter.poll_conversations("cmp_1"))
    assert convos == []


def test_pause_ad_success():
    adapter = _adapter()
    responses = [{"success": True}]
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(responses)
        assert _run(adapter.pause_ad("cmp_1", "ad_1")) is True


def test_pause_ad_raises_on_error():
    adapter = _adapter()
    responses = [{"error": {"message": "Ad not found", "code": 100}}]
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(responses)
        with pytest.raises(MetaAPIError, match="pause ad"):
            _run(adapter.pause_ad("cmp_1", "ad_1"))
