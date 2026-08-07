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
        whatsapp_number="2348031234567",
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
    # delete draws from the same queue so a rollback's DELETE can be asserted.
    client.delete = AsyncMock(side_effect=_next)
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
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(_plan(), _auth()))

    assert result.campaign_id == "cmp_1"
    assert result.ad_ids == {"b1": "ad_1"}
    assert result.platforms == [Platform.META]

    # wa.me LINK ad, not Meta's native Click-to-WhatsApp: the native form needs each
    # number hand-linked to the Page inside Meta (no partner API), which rejected every
    # launch with subcode 1487246 and cannot scale across brands on one shared Page.
    campaign_json = mock_client.post.call_args_list[0].kwargs["json"]
    assert campaign_json["objective"] == "OUTCOME_TRAFFIC"
    adset_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert adset_json["optimization_goal"] == "LINK_CLICKS"
    # No native WhatsApp routing — that is precisely what required the linking.
    assert "destination_type" not in adset_json
    assert "promoted_object" not in adset_json
    creative_spec = mock_client.post.call_args_list[2].kwargs["json"]["object_story_spec"]
    assert creative_spec["page_id"] == "pg123"
    # The tap goes to a real chat with the brand's own number (it used to be a bare
    # "https://wa.me/" with no number, which went nowhere), pre-filled with an opener
    # (live-confirmed: an empty chat with an unfamiliar number got 186 clicks and zero
    # messages — see test_wa_link_prefills_an_opening_message below).
    assert creative_spec["link_data"]["link"].startswith("https://wa.me/2348031234567?text=")
    assert creative_spec["link_data"]["call_to_action"]["type"] == "WHATSAPP_MESSAGE"
    ad_json = mock_client.post.call_args_list[3].kwargs["json"]
    assert ad_json  # ad creation call still happens after creative

    record = _run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": "cmp_1"}))
    assert record["ad_id"] == "ad_1"
    assert record["business_id"] == "b1"
    assert record["last_conversation_count"] == 0


def test_launch_campaign_followers_goal_builds_engagement_not_whatsapp():
    # Confirmed live shape (Meta Marketing API docs): a followers/engagement campaign
    # uses the same OUTCOME_ENGAGEMENT objective, but POST_ENGAGEMENT optimization,
    # LIKE_PAGE creative CTA, and no WhatsApp routing at all.
    db = FakeDb()
    adapter = _adapter(db)
    responses = [
        {"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"},
    ]
    plan = _plan(goal=Goal.FOLLOWERS, whatsapp_number="")
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(plan, _auth()))

    assert result.campaign_id == "cmp_1"
    campaign_json = mock_client.post.call_args_list[0].kwargs["json"]
    assert campaign_json["objective"] == "OUTCOME_ENGAGEMENT"
    adset_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert adset_json["optimization_goal"] == "POST_ENGAGEMENT"
    assert "destination_type" not in adset_json and "promoted_object" not in adset_json
    creative_spec = mock_client.post.call_args_list[2].kwargs["json"]["object_story_spec"]
    assert creative_spec["link_data"]["call_to_action"] == {"type": "LIKE_PAGE", "value": {"page": "pg123"}}
    assert creative_spec["link_data"]["link"] == "https://www.facebook.com/pg123"


def test_launch_campaign_followers_goal_does_not_require_whatsapp_number():
    adapter = _adapter()
    plan = _plan(goal=Goal.FOLLOWERS, whatsapp_number="")
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(responses)
        result = _run(adapter.launch_campaign(plan, _auth()))
    assert result.campaign_id == "cmp_1"


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


# ── Partial-launch rollback ───────────────────────────────────────────────────
# Meta has no transaction across campaign→adset→creative→ad. Live-confirmed on the
# real ad account: six campaigns existed on Meta with NO row in our DB, left behind
# by launches that died at the ad-set step (an unlinked WhatsApp number). They were
# invisible in "My Campaigns" and unmanageable from the app.

def test_launch_campaign_deletes_the_campaign_when_a_later_step_fails():
    db = FakeDb()
    adapter = _adapter(db)
    responses = [
        {"id": "cmp_1"},                                                      # campaign created
        {"error": {"message": "WhatsApp number not linked", "code": 100,      # ad set REJECTED
                   "error_subcode": 1487246}},
        {"success": True},                                                    # rollback DELETE
    ]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(MetaAPIError, match="ad set creation"):
            _run(adapter.launch_campaign(_plan(), _auth()))

    # The orphan is cleaned up: the campaign we created is deleted on Meta.
    assert mock_client.delete.await_count == 1
    assert "cmp_1" in mock_client.delete.await_args.args[0]


def test_launch_campaign_stores_no_record_when_a_later_step_fails():
    db = FakeDb()
    adapter = _adapter(db)
    responses = [
        {"id": "cmp_1"},
        {"error": {"message": "WhatsApp number not linked", "code": 100}},
        {"success": True},
    ]
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(responses)
        with pytest.raises(MetaAPIError):
            _run(adapter.launch_campaign(_plan(), _auth()))

    assert _run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": "cmp_1"})) is None


def test_rollback_never_masks_the_original_error():
    # The user needs Meta's real reason ("WhatsApp number not linked"), not a
    # cleanup failure — so a failing rollback must stay silent about itself.
    adapter = _adapter()
    responses = [
        {"id": "cmp_1"},
        {"error": {"message": "WhatsApp number not linked", "code": 100}},
    ]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        mock_client.delete = AsyncMock(side_effect=RuntimeError("network down"))
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(MetaAPIError, match="WhatsApp number not linked"):
            _run(adapter.launch_campaign(_plan(), _auth()))


def test_no_rollback_attempted_when_the_very_first_call_fails():
    # Nothing was created yet, so there is nothing to undo.
    adapter = _adapter()
    responses = [{"error": {"message": "Invalid parameter", "code": 100}}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(MetaAPIError, match="campaign creation"):
            _run(adapter.launch_campaign(_plan(), _auth()))
    assert mock_client.delete.await_count == 0


def test_successful_launch_never_rolls_anything_back():
    db = FakeDb()
    adapter = _adapter(db)
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(_plan(), _auth()))
    assert result.campaign_id == "cmp_1"
    assert mock_client.delete.await_count == 0


def test_followers_goal_keeps_engagement_objective_and_page_link():
    # A followers campaign is genuine on-Page engagement — it must NOT be switched to
    # traffic/wa.me, since there is no WhatsApp destination involved at all.
    db = FakeDb()
    adapter = _adapter(db)
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    plan = _plan(goal=Goal.FOLLOWERS, whatsapp_number="")
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(adapter.launch_campaign(plan, _auth()))

    assert mock_client.post.call_args_list[0].kwargs["json"]["objective"] == "OUTCOME_ENGAGEMENT"
    assert mock_client.post.call_args_list[1].kwargs["json"]["optimization_goal"] == "POST_ENGAGEMENT"
    link_data = mock_client.post.call_args_list[2].kwargs["json"]["object_story_spec"]["link_data"]
    assert link_data["link"] == "https://www.facebook.com/pg123"
    assert link_data["call_to_action"]["type"] == "LIKE_PAGE"


def test_video_creative_carries_the_wa_link_on_the_cta():
    # video_data has no link field of its own, so the wa.me destination has to ride on
    # the call_to_action or the tap would go nowhere. Live-confirmed against the real
    # Meta API: WHATSAPP_MESSAGE rejects a "link" in its value ("Too many parameters
    # in Call to Action", code=105, subcode=1815630) — LEARN_MORE is the CTA type that
    # actually accepts one for a video creative.
    db = FakeDb()
    adapter = _adapter(db)
    responses = [
        {"id": "vid_1"},                                                    # video upload
        {"status": {"video_status": "ready"}},                              # poll
        {"data": [{"uri": "https://thumb/1.jpg", "is_preferred": True}]},   # thumbnails
        {"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"},
    ]
    plan = _plan(creative=AdCreative(image_url="https://cdn/ad.mp4", is_video=True,
                                     headline="h", primary_text="p"))
    with patch("httpx.AsyncClient") as MockClient, \
         patch("app.agents.jane_ads.adapters.meta.asyncio.sleep", new=AsyncMock()):
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(adapter.launch_campaign(plan, _auth()))

    video_data = mock_client.post.call_args_list[-2].kwargs["json"]["object_story_spec"]["video_data"]
    assert video_data["call_to_action"]["type"] == "LEARN_MORE"
    assert video_data["call_to_action"]["value"]["link"].startswith("https://wa.me/2348031234567?text=")


def test_wa_link_prefills_an_opening_message():
    # Live-confirmed regression: a bare "https://wa.me/<number>" opens an EMPTY chat
    # with a number the person has never messaged — a real campaign got 186 link
    # clicks and zero WhatsApp messages. wa.me's own `text` param pre-fills the
    # message box so there's something to just tap send on.
    db = FakeDb()
    adapter = _adapter(db)
    responses = [
        {"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"},
    ]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(adapter.launch_campaign(_plan(), _auth()))

    creative_spec = mock_client.post.call_args_list[2].kwargs["json"]["object_story_spec"]
    link = creative_spec["link_data"]["link"]
    assert link.startswith("https://wa.me/2348031234567?text=")
    from urllib.parse import unquote
    assert unquote(link.split("?text=", 1)[1]).strip()
