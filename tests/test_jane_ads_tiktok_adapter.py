"""
Unit tests for the TikTok Ads adapter (adapters/tiktok.py).

httpx is mocked throughout — these prove the adapter builds correct requests and
handles responses/validation/rollback correctly, not that TikTok's live API behaves
as documented (no credentials exist yet). Follows the exact convention established by
test_jane_ads_google_adapter.py: no pytest-asyncio, a manual _run() helper, a
hand-rolled FakeDb, unittest.mock.patch("httpx.AsyncClient") with a queued-JSON-
responses helper covering both .get and .post (TikTok's reporting calls are GET,
its mutation calls are POST).
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads.adapters.tiktok import TikTokAdsAdapter, TikTokAdsAPIError
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


class FakeIdentityCollection:
    """A single cached doc, looked up by advertiser_id — mirrors the shape
    _get_or_create_identity actually uses (jane_ads_tiktok_identity)."""

    def __init__(self):
        self.doc: dict | None = None

    async def find_one(self, query):
        return dict(self.doc) if self.doc else None

    async def update_one(self, query, update, upsert=False):
        self.doc = {**(self.doc or {}), **query, **update.get("$set", {})}


class FakeDb:
    def __init__(self):
        self._coll = FakeCollection()
        self._identity_coll = FakeIdentityCollection()

    def __getitem__(self, name):
        if name == "jane_ads_tiktok_identity":
            return self._identity_coll
        return self._coll


def _plan(**kw) -> CampaignPlan:
    base = dict(
        business_id="b1", goal=Goal.LEADS, behaviour=PurchaseBehaviour.DISCOVER,
        platforms=[PlatformPlan(platform=Platform.TIKTOK, budget_ngn=70_000, days=7,
                                variants=1, test_scope=ABTestScope.NONE,
                                objective=CampaignObjective.CONVERSATIONS)],
        per_business_cap_ngn=70_000, account_cap_ngn=70_000,
        whatsapp_number="2348031234567",
        creative=AdCreative(image_url="https://cdn.example.com/clip.mp4", is_video=True,
                            headline="Fresh Cuts Daily", primary_text="Book on WhatsApp today"),
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
    client.get = AsyncMock(side_effect=_next)
    return client


def _adapter(db=None) -> TikTokAdsAdapter:
    return TikTokAdsAdapter(db or FakeDb(), advertiser_id="adv123", access_token="tok")


# campaign, video upload, ad group, identity logo upload, identity create, ad — 6 calls total.
_HAPPY_RESPONSES = [
    {"code": 0, "message": "OK", "data": {"campaign_id": "111"}},
    {"code": 0, "message": "OK", "data": [{"video_id": "vid_999"}]},
    {"code": 0, "message": "OK", "data": {"adgroup_id": "222"}},
    {"code": 0, "message": "OK", "data": {"image_id": "img_888"}},
    {"code": 0, "message": "OK", "data": {"identity_id": "identity_777"}},
    {"code": 0, "message": "OK", "data": {"ad_ids": ["333"]}},
]


def test_requires_advertiser_id():
    with pytest.raises(TikTokAdsAPIError):
        TikTokAdsAdapter(FakeDb(), advertiser_id="", access_token="tok")


def test_requires_access_token():
    with pytest.raises(TikTokAdsAPIError):
        TikTokAdsAdapter(FakeDb(), advertiser_id="adv123", access_token="")


# ── launch_campaign ───────────────────────────────────────────────────────────────

def test_launch_campaign_happy_path_full_call_sequence():
    db = FakeDb()
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(_plan(), _auth()))

    assert result.campaign_id == "111"
    assert result.ad_ids == {"b1": "333"}
    assert result.platforms == [Platform.TIKTOK]
    assert mock_client.post.call_count == 6

    campaign_json = mock_client.post.call_args_list[0].kwargs["json"]
    assert campaign_json["operation_status"] == "DISABLE"
    assert campaign_json["objective_type"] == "TRAFFIC"

    video_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert video_json["upload_type"] == "UPLOAD_BY_URL"
    assert video_json["video_url"] == "https://cdn.example.com/clip.mp4"

    adgroup_json = mock_client.post.call_args_list[2].kwargs["json"]
    assert adgroup_json["operation_status"] == "DISABLE"
    assert adgroup_json["budget"] == 70_000
    assert adgroup_json["campaign_id"] == "111"
    assert adgroup_json["bid_type"] == "BID_TYPE_NO_BID"
    assert adgroup_json["pacing"] == "PACING_MODE_SMOOTH"

    image_json = mock_client.post.call_args_list[3].kwargs["json"]
    assert image_json["upload_type"] == "UPLOAD_BY_URL"
    assert "image_url" in image_json

    identity_json = mock_client.post.call_args_list[4].kwargs["json"]
    assert identity_json["display_name"]
    assert identity_json["image_uri"] == "img_888"

    ad_json = mock_client.post.call_args_list[5].kwargs["json"]
    assert ad_json["operation_status"] == "DISABLE"
    creative = ad_json["creatives"][0]
    assert creative["video_id"] == "vid_999"
    assert creative["landing_page_url"] == "https://wa.me/2348031234567"
    assert creative["identity_id"] == "identity_777"
    assert creative["identity_type"] == "CUSTOMIZED_USER"

    # A second launch must reuse the cached identity — no repeat upload/create calls.
    mock_client2 = _mock_client([
        {"code": 0, "message": "OK", "data": {"campaign_id": "444"}},
        {"code": 0, "message": "OK", "data": [{"video_id": "vid_000"}]},
        {"code": 0, "message": "OK", "data": {"adgroup_id": "555"}},
        {"code": 0, "message": "OK", "data": {"ad_ids": ["666"]}},
    ])
    with patch("httpx.AsyncClient") as MockClient2:
        MockClient2.return_value.__aenter__.return_value = mock_client2
        _run(adapter.launch_campaign(_plan(business_id="b2"), _auth()))
    assert mock_client2.post.call_count == 4
    ad_json_2 = mock_client2.post.call_args_list[3].kwargs["json"]
    assert ad_json_2["creatives"][0]["identity_id"] == "identity_777"

    record = _run(db["jane_ads_tiktok_campaigns"].find_one({"campaign_id": "111"}))
    assert record["ad_id"] == "333"
    assert record["business_id"] == "b1"
    assert record["last_click_count"] == 0


def test_launch_campaign_missing_whatsapp_raises_before_any_http_call():
    adapter = _adapter()
    plan = _plan(whatsapp_number="")
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(ValueError):
            _run(adapter.launch_campaign(plan, _auth()))
    assert mock_client.post.call_count == 0


def test_launch_campaign_missing_video_creative_raises_before_any_http_call():
    adapter = _adapter()
    plan = _plan(creative=AdCreative(image_url="https://cdn.example.com/photo.jpg", is_video=False))
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(ValueError):
            _run(adapter.launch_campaign(plan, _auth()))
    assert mock_client.post.call_count == 0


def test_launch_campaign_no_creative_at_all_raises():
    adapter = _adapter()
    plan = _plan(creative=None)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(ValueError):
            _run(adapter.launch_campaign(plan, _auth()))
    assert mock_client.post.call_count == 0


def test_launch_campaign_raises_on_tiktok_error():
    adapter = _adapter()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"code": 40001, "message": "Invalid budget"}])
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(TikTokAdsAPIError, match="Invalid budget"):
            _run(adapter.launch_campaign(_plan(), _auth()))


def test_launch_campaign_rolls_back_partial_launch_on_failure_and_preserves_original_error():
    adapter = _adapter()
    # Campaign + video upload succeed, ad group creation fails -> rollback should
    # DELETE the already-created campaign, and the ORIGINAL error (not a rollback
    # error) propagates.
    responses = [
        {"code": 0, "message": "OK", "data": {"campaign_id": "111"}},
        {"code": 0, "message": "OK", "data": [{"video_id": "vid_999"}]},
        {"code": 40002, "message": "ad group rejected: invalid location_ids"},
        {"code": 0, "message": "OK"},  # the rollback's own DELETE-status call
    ]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(TikTokAdsAPIError, match="ad group rejected"):
            _run(adapter.launch_campaign(_plan(), _auth()))

    rollback_json = mock_client.post.call_args_list[3].kwargs["json"]
    assert rollback_json["operation_status"] == "DELETE"
    assert rollback_json["campaign_ids"] == ["111"]


# ── fetch_per_ad_spend ──────────────────────────────────────────────────────────

def test_fetch_per_ad_spend_passes_through_ngn():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client([{
            "code": 0, "message": "OK",
            "data": {"currency": "NGN", "list": [{"dimensions": {"ad_id": "333"}, "metrics": {"spend": "12500"}}]},
        }])
        spend = _run(adapter.fetch_per_ad_spend("111"))
    assert spend[0].spend_ngn == 12500.0
    assert spend[0].platform == Platform.TIKTOK


def test_fetch_per_ad_spend_converts_usd_via_constants():
    from app.agents.jane_ads import constants as C
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client([{
            "code": 0, "message": "OK",
            "data": {"currency": "USD", "list": [{"dimensions": {"ad_id": "333"}, "metrics": {"spend": "10"}}]},
        }])
        spend = _run(adapter.fetch_per_ad_spend("111"))
    assert spend[0].spend_ngn == 10.0 * C.USD_TO_NGN


def test_fetch_per_ad_spend_no_rows_returns_zero():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client([{
            "code": 0, "message": "OK", "data": {"list": []},
        }])
        spend = _run(adapter.fetch_per_ad_spend("111"))
    assert spend[0].spend_ngn == 0.0


# ── poll_conversations (delta tracking) ──────────────────────────────────────────

def test_poll_conversations_returns_only_new_clicks_since_last_poll():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333", "last_click_count": 3}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client([{
            "code": 0, "message": "OK",
            "data": {"list": [{"metrics": {"clicks": "8", "spend": "4000"}}]},
        }])
        events = _run(adapter.poll_conversations("111"))
    assert len(events) == 5  # 8 - 3
    assert db["jane_ads_tiktok_campaigns"].docs["111"]["last_click_count"] == 8


def test_poll_conversations_returns_empty_when_no_new_clicks():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333", "last_click_count": 8}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client([{
            "code": 0, "message": "OK",
            "data": {"list": [{"metrics": {"clicks": "8", "spend": "4000"}}]},
        }])
        events = _run(adapter.poll_conversations("111"))
    assert events == []


# ── pause_ad ──────────────────────────────────────────────────────────────────────

def test_pause_ad_sends_disable_status():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333", "adgroup_id": "222"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"code": 0, "message": "OK"}])
        MockClient.return_value.__aenter__.return_value = mock_client
        ok = _run(adapter.pause_ad("111", "333"))
    assert ok is True
    sent = mock_client.post.call_args.kwargs["json"]
    assert sent["operation_status"] == "DISABLE"
    assert sent["ad_ids"] == ["333"]
    assert sent["adgroup_id"] == "222"


# ── fetch_campaign_summary / set_delivery / delete_campaign ─────────────────────
# Not part of AdPlatformAdapter's ABC — billing.py and the campaign-management
# router endpoints call these directly, mirroring the extra contract
# MetaAdPlatformAdapter exposes (see adapters/meta.py's own tests for the Meta
# equivalents of every case below).

def test_fetch_campaign_summary_active_campaign():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([
            {"code": 0, "message": "OK", "data": {"list": [{"operation_status": "ENABLE"}]}},
            {"code": 0, "message": "OK", "data": {"list": [{"metrics": {
                "impressions": "1000", "reach": "800", "clicks": "20", "spend": "5000",
            }}]}},
        ])
        MockClient.return_value.__aenter__.return_value = mock_client
        summary = _run(adapter.fetch_campaign_summary("111"))
    assert summary["delivery"] == "Active"
    assert summary["spend_ngn"] == 5000.0
    assert summary["impressions"] == 1000
    assert summary["reach"] == 800
    assert summary["conversations"] == 20
    assert summary["cost_per_conversation_ngn"] == 250.0


def test_fetch_campaign_summary_paused_campaign():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([
            {"code": 0, "message": "OK", "data": {"list": [{"operation_status": "DISABLE"}]}},
            {"code": 0, "message": "OK", "data": {"list": []}},
        ])
        MockClient.return_value.__aenter__.return_value = mock_client
        summary = _run(adapter.fetch_campaign_summary("111"))
    assert summary["delivery"] == "Paused"
    assert summary["spend_ngn"] == 0.0
    assert summary["conversations"] == 0
    assert summary["cost_per_conversation_ngn"] is None


def test_fetch_campaign_summary_no_status_rows_means_deleted():
    # TikTok no longer knows about this campaign at all — same "gone" signal
    # Meta gives via effective_status, just absent here instead of an explicit
    # value. billing.py and the campaign list both self-heal on "Deleted".
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([
            {"code": 0, "message": "OK", "data": {"list": []}},
            {"code": 0, "message": "OK", "data": {"list": []}},
        ])
        MockClient.return_value.__aenter__.return_value = mock_client
        summary = _run(adapter.fetch_campaign_summary("111"))
    assert summary["delivery"] == "Deleted"


def test_fetch_campaign_summary_unknown_campaign_raises():
    adapter = _adapter(FakeDb())
    with pytest.raises(TikTokAdsAPIError):
        _run(adapter.fetch_campaign_summary("does-not-exist"))


def test_set_delivery_enable_cascades_to_campaign_adgroup_and_ad():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333", "adgroup_id": "222"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([
            {"code": 0, "message": "OK"},
            {"code": 0, "message": "OK"},
            {"code": 0, "message": "OK"},
        ])
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.set_delivery("111", active=True))

    assert result["status"] == "ENABLE"
    assert result["updated"] == {"campaign": True, "adgroup": True, "ad": True}
    assert mock_client.post.call_count == 3

    campaign_json = mock_client.post.call_args_list[0].kwargs["json"]
    assert campaign_json["campaign_ids"] == ["111"]
    assert campaign_json["operation_status"] == "ENABLE"

    adgroup_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert adgroup_json["adgroup_ids"] == ["222"]
    assert adgroup_json["operation_status"] == "ENABLE"

    ad_json = mock_client.post.call_args_list[2].kwargs["json"]
    assert ad_json["ad_ids"] == ["333"]
    assert ad_json["adgroup_id"] == "222"
    assert ad_json["operation_status"] == "ENABLE"


def test_set_delivery_disable_sends_disable_to_every_level():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333", "adgroup_id": "222"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([
            {"code": 0, "message": "OK"},
            {"code": 0, "message": "OK"},
            {"code": 0, "message": "OK"},
        ])
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.set_delivery("111", active=False))
    assert result["status"] == "DISABLE"
    for call in mock_client.post.call_args_list:
        assert call.kwargs["json"]["operation_status"] == "DISABLE"


def test_set_delivery_skips_adgroup_and_ad_when_ids_missing():
    # A record saved before adgroup_id/ad_id were populated (shouldn't happen in
    # practice, but the cascade must not crash on a missing id).
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"code": 0, "message": "OK"}])
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.set_delivery("111", active=True))
    assert result["updated"] == {"campaign": True}
    assert mock_client.post.call_count == 1


def test_set_delivery_raises_on_campaign_level_error():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "ad_id": "333", "adgroup_id": "222"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"code": 40003, "message": "campaign not found"}])
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(TikTokAdsAPIError, match="campaign not found"):
            _run(adapter.set_delivery("111", active=True))


def test_delete_campaign_sends_delete_status():
    adapter = _adapter()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"code": 0, "message": "OK"}])
        MockClient.return_value.__aenter__.return_value = mock_client
        ok = _run(adapter.delete_campaign("111"))
    assert ok is True
    sent = mock_client.post.call_args.kwargs["json"]
    assert sent["campaign_ids"] == ["111"]
    assert sent["operation_status"] == "DELETE"


def test_delete_campaign_raises_on_error():
    adapter = _adapter()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"code": 40004, "message": "already deleted"}])
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(TikTokAdsAPIError, match="already deleted"):
            _run(adapter.delete_campaign("111"))
