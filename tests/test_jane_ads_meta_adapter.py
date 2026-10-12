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
    CampaignObjective,
    ABTestScope,
    AdCreative,
    CampaignObjective,
    CampaignPlan,
    GeoMode,
    GeoPin,
    GeoPlan,
    Goal,
    Platform,
    PlatformPlan,
    PurchaseBehaviour,
    SpendAuthorization,
)


@pytest.fixture(autouse=True)
def _no_named_geo_by_default(monkeypatch):
    """Default every test to coordinate pins.

    launch_campaign now asks Meta to resolve each pocket to a NAMED location, so Ads
    Manager shows "Ikeja, Lagos State" rather than "(6.6018, 3.3515)". That is a real
    Graph call, and these tests mock httpx globally — without this it would eat a
    response from each test's canned queue. The named path has its own tests below,
    which override this.
    """
    async def _unresolved(name, expected_region, access_token="", timeout=8.0):
        return None

    monkeypatch.setattr("app.agents.jane_ads.geo_names.region_for", _fixed_region)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _unresolved)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def whatsapp_only(monkeypatch):
    """Exercise the WhatsApp-only routing.

    Launches try Meta's multi-inbox messaging format first (as the manual campaign on
    the account is set up) and fall back to this path when Meta refuses it for a
    Page. These tests cover that fallback path itself, so they start on it.
    """
    monkeypatch.setattr("app.agents.jane_ads.adapters.meta._wants_multi_destination",
                        lambda *a, **k: False)


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
    # The campaign objective is derived from the goal exactly as the decision engine
    # does, so these plans carry what a real one would rather than the enum default —
    # otherwise a followers ad tests as if it optimised for link clicks.
    from app.agents.jane_ads.decision_engine import default_objective_for
    goal = kw.get("goal", Goal.MESSAGES)
    kw.setdefault("objective", default_objective_for(goal))
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


# Meta's rejection of the native Click-to-WhatsApp form. Whether the brand's number
# is linked to the Page is only knowable by asking Meta, and this is how it answers:
# code=100/subcode=1487246 on the ad set create. The adapter retries as a wa.me link
# ad, which is safe because a failed ad set create leaves nothing behind.
_NATIVE_REJECTED = {
    "error": {
        "message": "Invalid parameter",
        "code": 100,
        "error_subcode": 1487246,
        "error_user_msg": "This WhatsApp phone number is not linked to your account",
    }
}


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

    # A WhatsApp-destination plan ATTEMPTS Meta's native Click-to-WhatsApp first —
    # it's the only form that fires messaging_conversation_started. Meta accepting the
    # ad set (as mocked here) is what proves the number is linked; the wa.me link ad is
    # the fallback for when it rejects with subcode 1487246, not the default.
    campaign_json = mock_client.post.call_args_list[0].kwargs["json"]
    assert campaign_json["objective"] == "OUTCOME_ENGAGEMENT"
    # The manual campaign's routing: one ad set covering Messenger, Instagram DMs and
    # WhatsApp, Meta opening whichever inbox each person uses.
    adset_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert adset_json["optimization_goal"] == "CONVERSATIONS"
    assert adset_json["destination_type"] == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER_WHATSAPP"
    assert adset_json["promoted_object"] == {"page_id": "pg123", "smart_pse_enabled": False}
    creative_json = mock_client.post.call_args_list[2].kwargs["json"]
    creative_spec = creative_json["object_story_spec"]
    assert creative_spec["page_id"] == "pg123"
    # The button on the story routes to Messenger, as Ads Manager sets it; the asset
    # feed carries one call-to-action per inbox.
    assert creative_spec["link_data"]["call_to_action"]["value"]["app_destination"] == "MESSENGER"
    assert "page_welcome_message" in creative_spec["link_data"]
    assert creative_json["asset_feed_spec"]["optimization_type"] == "DOF_MESSAGING_DESTINATION"
    assert creative_json["degrees_of_freedom_spec"]["creative_features_spec"][
        "enhance_cta"] == {"enroll_status": "OPT_IN"}
    assert creative_json["contextual_multi_ads"] == {"enroll_status": "OPT_IN"}
    ad_json = mock_client.post.call_args_list[3].kwargs["json"]
    assert ad_json  # ad creation call still happens after creative

    record = _run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": "cmp_1"}))
    assert record["ad_id"] == "ad_1"
    assert record["business_id"] == "b1"
    assert record["last_conversation_count"] == 0
    assert record["conversations_measurable"] is True


def test_adset_always_declares_the_advantage_audience_flag():
    # Meta made targeting_automation.advantage_audience mandatory 2026-09-05 — without
    # it EVERY ad set create fails with "Advantage audience flag required"
    # (code=100, subcode=1870227). 1, with geo as an individual Advantage+ setting —
    # exactly as the manual campaign on the account has it, which out-performed every
    # Jane campaign. The plan card says the interests steer rather than fence.
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(), _auth()))
    targeting = mock_client.post.call_args_list[1].kwargs["json"]["targeting"]
    assert targeting["targeting_automation"] == {"advantage_audience": 1,
                                                 "individual_setting": {"geo": 1}}


def test_launch_campaign_with_no_geo_targets_all_of_nigeria():
    # A plan with no geo pins (never geocoded, or nothing validated) must still
    # launch, broadly — the pre-pins behaviour, kept as the fallback.
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(), _auth()))
    adset_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert adset_json["targeting"]["geo_locations"] == {"countries": ["NG"]}


def test_launch_campaign_targets_the_plans_actual_geo_pins(monkeypatch):
    # Live-reported: the real launched ad set always targeted all of Nigeria no
    # matter what Jane's geo reasoning (pins around Ikeja, a 2km radius, etc.) had
    # decided — the pins were computed and shown in the plan card/estimate, but
    # never reached the actual Meta ad set. The real launch must use them.
    async def _resolved(name, expected_region, access_token="", timeout=8.0):
        return {"type": "city", "key": "1625140", "name": "Ikeja", "region": "Lagos State"}

    monkeypatch.setattr("app.agents.jane_ads.geo_names.region_for", _fixed_region)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _resolved)
    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, city="Ikeja", pins=[
        GeoPin(name="Ikeja", lat=6.6018, lng=3.3515, radius_km=3.0),
    ])
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(geo=geo), _auth()))
    adset_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert adset_json["targeting"]["geo_locations"] == {"cities": [{"key": "1625140", "radius": 25, "distance_unit": "mile"}]}


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
    # PAGE_LIKES, not POST_ENGAGEMENT: Meta rejects the latter on a Page-promoting
    # ad set ("Performance goal isn't available", subcode 2446286).
    assert adset_json["optimization_goal"] == "PAGE_LIKES"
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
        # Deliberately NOT subcode 1487246 — that one is retried as a wa.me link ad
        # rather than failing, so it would never reach the rollback this asserts.
        {"error": {"message": "Invalid bid strategy", "code": 100,             # ad set REJECTED
                   "error_subcode": 2490487}},
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


def test_launch_campaign_stores_no_record_when_a_later_step_fails(whatsapp_only):
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


def test_rollback_never_masks_the_original_error(whatsapp_only):
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
    assert mock_client.post.call_args_list[1].kwargs["json"]["optimization_goal"] == "PAGE_LIKES"
    link_data = mock_client.post.call_args_list[2].kwargs["json"]["object_story_spec"]["link_data"]
    assert link_data["link"] == "https://www.facebook.com/pg123"
    assert link_data["call_to_action"]["type"] == "LIKE_PAGE"


def test_video_creative_carries_the_wa_link_on_the_cta(whatsapp_only):
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
        {"id": "cmp_1"},          # campaign
        _NATIVE_REJECTED,         # native ad set refused — number not linked
        {"id": "adset_1"},        # retried as a wa.me link ad
        {"id": "creative_1"}, {"id": "ad_1"},
    ]
    plan = _plan(creative=AdCreative(image_url="https://cdn/ad.mp4", is_video=True,
                                     headline="h", primary_text="p"))
    with patch("httpx.AsyncClient") as MockClient, \
         patch("app.agents.jane_ads.adapters.meta.asyncio.sleep", new=AsyncMock()):
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(adapter.launch_campaign(plan, _auth()))

    video_data = mock_client.post.call_args_list[-2].kwargs["json"]["object_story_spec"]["video_data"]
    assert video_data["call_to_action"]["type"] == "CONTACT_US"
    assert video_data["call_to_action"]["value"]["link"].startswith("https://wa.me/2348031234567?text=")


def test_wa_link_prefills_an_opening_message(whatsapp_only):
    # Live-confirmed regression: a bare "https://wa.me/<number>" opens an EMPTY chat
    # with a number the person has never messaged — a real campaign got 186 link
    # clicks and zero WhatsApp messages. wa.me's own `text` param pre-fills the
    # message box so there's something to just tap send on.
    db = FakeDb()
    adapter = _adapter(db)
    responses = [
        {"id": "cmp_1"},          # campaign
        _NATIVE_REJECTED,         # native ad set refused — number not linked
        {"id": "adset_1"},        # retried as a wa.me link ad
        {"id": "creative_1"}, {"id": "ad_1"},
    ]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(adapter.launch_campaign(_plan(), _auth()))

    creative_spec = mock_client.post.call_args_list[3].kwargs["json"]["object_story_spec"]
    link = creative_spec["link_data"]["link"]
    assert link.startswith("https://wa.me/2348031234567?text=")
    from urllib.parse import unquote
    assert unquote(link.split("?text=", 1)[1]).strip()


# ── Native Click-to-WhatsApp, and the fallback when Meta says the number isn't linked ──
#
# There is no pre-flight Page-field check any more. GET /{page}?fields=whatsapp_number,
# has_whatsapp_number is not a usable oracle — it returns neither field for a Page that
# Meta accepts native ads on (live-verified 2026-09-09 on page 203213912878798, whose
# ad set validated clean), so it downgraded campaigns that should have gone native.
# Meta itself decides, by accepting or rejecting the real ad set.


def test_linked_page_builds_a_native_click_to_whatsapp_ad(whatsapp_only):
    """Native is the only form that fires messaging_conversation_started — a wa.me link
    ad can never report a conversation, which is why those campaigns showed
    "WhatsApp conversations 0" while genuinely delivering clicks."""
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(), _auth()))

    campaign = mock_client.post.call_args_list[0].kwargs["json"]
    adset = mock_client.post.call_args_list[1].kwargs["json"]
    spec = mock_client.post.call_args_list[2].kwargs["json"]["object_story_spec"]

    # CONVERSATIONS optimisation isn't available under OUTCOME_TRAFFIC.
    assert campaign["objective"] == "OUTCOME_ENGAGEMENT"
    assert adset["optimization_goal"] == "CONVERSATIONS"
    assert adset["destination_type"] == "WHATSAPP"
    assert adset["promoted_object"] == {
        "page_id": "pg123", "whatsapp_phone_number": "2348031234567",
    }
    # The button carries no link — the ad set owns the routing, and WHATSAPP_MESSAGE
    # refuses a value.link, which is exactly why the link-ad path can't use it.
    assert spec["link_data"]["call_to_action"] == {"type": "WHATSAPP_MESSAGE"}
    assert "wa.me" not in spec["link_data"]["link"]


def test_an_unlinked_page_falls_back_to_the_wa_me_link_ad(whatsapp_only):
    """Unlinked Pages must keep launching. Meta refuses the native ad set with subcode
    1487246 and creates nothing, so retrying as a wa.me link ad is what stops a
    campaign failing to launch at all."""
    responses = [
        {"id": "cmp_1"},          # campaign
        _NATIVE_REJECTED,         # native ad set refused
        {"id": "adset_1"},        # retried as a wa.me link ad
        {"id": "creative_1"}, {"id": "ad_1"},
    ]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(), _auth()))

    native_adset = mock_client.post.call_args_list[1].kwargs["json"]
    retried_adset = mock_client.post.call_args_list[2].kwargs["json"]
    spec = mock_client.post.call_args_list[3].kwargs["json"]["object_story_spec"]

    # The native form was genuinely attempted — that attempt IS the check.
    assert native_adset["destination_type"] == "WHATSAPP"
    # The retry drops every trace of it, or Meta would refuse it again.
    assert "destination_type" not in retried_adset
    assert "promoted_object" not in retried_adset
    assert retried_adset["optimization_goal"] == "LINK_CLICKS"
    # The campaign was already created as OUTCOME_ENGAGEMENT and is not recreated;
    # LINK_CLICKS is valid under it (live-verified against the real API 2026-09-09).
    assert mock_client.post.call_args_list[0].kwargs["json"]["objective"] == "OUTCOME_ENGAGEMENT"
    assert spec["link_data"]["link"].startswith("https://wa.me/")
    assert spec["link_data"]["call_to_action"]["type"] == "CONTACT_US"


def test_only_the_not_linked_error_falls_back_every_other_one_surfaces(whatsapp_only):
    """The retry exists for exactly one Meta error. Treating any ad set failure as
    "must not be linked" would silently ship a lesser ad — and hide a real bug — so
    anything but subcode 1487246 has to fail the launch and roll the campaign back."""
    responses = [
        {"id": "cmp_1"},                                                  # campaign
        {"error": {"message": "Invalid parameter", "code": 100,           # a DIFFERENT error
                   "error_subcode": 1885183}},
        {"success": True},                                                # rollback DELETE
    ]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(MetaAPIError, match="ad set creation"):
            _run(_adapter().launch_campaign(_plan(), _auth()))

    # Exactly one ad set attempt — no retry, no downgrade.
    adset_calls = [c for c in mock_client.post.call_args_list if "adsets" in c.args[0]]
    assert len(adset_calls) == 1
    assert mock_client.delete.await_count == 1


def test_a_website_destination_never_goes_native():
    """Native routing is only right when the client actually chose WhatsApp. A website
    campaign must not even ATTEMPT the native form — no destination_type, and so no
    chance of the 1487246 retry — however linked the Page is."""
    plan = _plan(destination_type="website", destination_link="https://example.com/shop")
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(plan, _auth()))

    adset = mock_client.post.call_args_list[1].kwargs["json"]
    spec = mock_client.post.call_args_list[2].kwargs["json"]["object_story_spec"]
    assert adset["optimization_goal"] == "LINK_CLICKS"
    assert "destination_type" not in adset
    assert spec["link_data"]["link"] == "https://example.com/shop"


async def _fixed_region(place, access_token="", timeout=8.0):
    """geo resolves the campaign's STATE before guarding pockets against it. Tests
    pin it so they exercise the matching, not Meta's gazetteer."""
    return "Lagos State"


# ── Named locations (DASH: "show exact locations, not longitude and latitude") ──

def test_a_verified_pocket_is_sent_as_a_named_location(monkeypatch):
    """A raw coordinate makes the one thing a client most wants to check — where their
    money is going — unreadable in Ads Manager. A pocket Meta can name is sent by key
    so it renders as its real name plus the radius."""
    async def _resolved(name, expected_region, access_token="", timeout=8.0):
        return {"type": "city", "key": "1625140", "name": "Ikeja", "region": "Lagos State"}

    monkeypatch.setattr("app.agents.jane_ads.geo_names.region_for", _fixed_region)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _resolved)
    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, city="Lagos", pins=[
        GeoPin(name="Ikeja", lat=6.6018, lng=3.3515, radius_km=3.0),
    ])
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(geo=geo), _auth()))

    geo_locations = mock_client.post.call_args_list[1].kwargs["json"]["targeting"]["geo_locations"]
    # NO radius: Meta rejects a radius on a named key outright, because the named
    # location carries its own boundary (live-verified 2026-09-17).
    assert geo_locations == {"cities": [{"key": "1625140", "radius": 25, "distance_unit": "mile"}]}
    assert "custom_locations" not in geo_locations


def test_an_unresolvable_pocket_is_dropped_never_sent_as_coordinates(monkeypatch):
    """Meta has no key for some gazetteer pockets ("Computer Village", "Admiralty
    Way"). Those used to be sent as lat/lng, which renders in Ads Manager as
    "(6.5960, 3.3420) + 1.5 km" — unverifiable to the client paying for it. The
    pocket is dropped and targeting widens to the named city instead."""
    async def _only_the_city(name, expected_region, access_token="", timeout=8.0):
        if name == "Lagos":
            return {"type": "city", "key": "1234", "name": "Lagos", "region": "Lagos State"}
        return None

    monkeypatch.setattr("app.agents.jane_ads.geo_names.region_for", _fixed_region)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _only_the_city)
    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, city="Lagos", pins=[
        GeoPin(name="Computer Village", lat=6.5960, lng=3.3420, radius_km=1.5),
    ])
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(geo=geo), _auth()))

    geo_locations = mock_client.post.call_args_list[1].kwargs["json"]["targeting"]["geo_locations"]
    assert geo_locations == {"cities": [{"key": "1234", "radius": 25, "distance_unit": "mile"}]}
    assert "custom_locations" not in geo_locations


def test_an_unnamed_pocket_never_drags_coordinates_in_beside_a_named_one(monkeypatch):
    """A plan mixes both. The named pocket is targeted; the unnamed one is dropped
    rather than travelling alongside it as a coordinate. This narrows the targeted
    area, which is the accepted cost of never showing a client a raw lat/lng."""
    async def _resolve_only_ikeja(name, expected_region, access_token="", timeout=8.0):
        if name == "Ikeja":
            return {"type": "city", "key": "1625140", "name": "Ikeja", "region": "Lagos State"}
        return None

    monkeypatch.setattr("app.agents.jane_ads.geo_names.region_for", _fixed_region)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _resolve_only_ikeja)
    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, city="Lagos", pins=[
        GeoPin(name="Ikeja", lat=6.6018, lng=3.3515, radius_km=3.0),
        GeoPin(name="Computer Village", lat=6.5960, lng=3.3420, radius_km=1.5),
    ])
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(geo=geo), _auth()))

    geo_locations = mock_client.post.call_args_list[1].kwargs["json"]["targeting"]["geo_locations"]
    assert geo_locations == {"cities": [{"key": "1625140", "radius": 25, "distance_unit": "mile"}]}
    assert "custom_locations" not in geo_locations


def test_when_nothing_can_be_named_it_widens_to_the_state_not_a_pin(monkeypatch):
    """City lookup can fail too. The next step is the state — still named, still
    readable in Ads Manager — and never the coordinates we started from."""
    async def _nothing(name, expected_region, access_token="", timeout=8.0):
        return None

    async def _region(region_name, access_token="", timeout=8.0):
        return {"type": "region", "key": "3871", "name": "Lagos State", "region": "Lagos State"}

    monkeypatch.setattr("app.agents.jane_ads.geo_names.region_for", _fixed_region)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _nothing)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_region", _region)
    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, city="Lagos", pins=[
        GeoPin(name="Admiralty Way", lat=6.4419, lng=3.4726, radius_km=1.5),
    ])
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(geo=geo), _auth()))

    geo_locations = mock_client.post.call_args_list[1].kwargs["json"]["targeting"]["geo_locations"]
    assert geo_locations == {"regions": [{"key": "3871"}]}


def test_with_every_lookup_dead_it_falls_back_to_the_country(monkeypatch):
    """Meta search down, or no token. Targeting must still be valid — an empty
    geo_locations is rejected outright — and must still carry no coordinates."""
    async def _boom(*a, **kw):
        raise RuntimeError("meta search unavailable")

    monkeypatch.setattr("app.agents.jane_ads.geo_names.region_for", _fixed_region)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _boom)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_region", _boom)
    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, city="Lagos", pins=[
        GeoPin(name="Computer Village", lat=6.5960, lng=3.3420, radius_km=1.5),
    ])
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(geo=geo), _auth()))

    geo_locations = mock_client.post.call_args_list[1].kwargs["json"]["targeting"]["geo_locations"]
    assert geo_locations == {"countries": ["NG"]}
    assert "custom_locations" not in geo_locations


def test_a_city_is_resolved_to_its_state_before_pockets_are_matched(monkeypatch):
    """Live-reported: the region guard was fed the campaign's CITY ("Ikeja"), but Meta
    reports every hit's region as the STATE ("Lagos State"). Opebi and Alausa — both
    real, targetable locations — were rejected, the city fallback failed for the same
    reason, and the campaign silently dropped to all of Nigeria."""
    seen = {}

    async def _region_for(place, access_token="", timeout=8.0):
        seen["asked"] = place
        return "Lagos State"

    async def _resolve(name, expected_region, access_token="", timeout=8.0):
        seen.setdefault("guards", []).append(expected_region)
        if expected_region == "Lagos State" and name in ("Opebi", "Alausa"):
            return {"type": "neighborhood", "key": f"k_{name}", "name": name,
                    "region": "Lagos State"}
        return None

    monkeypatch.setattr("app.agents.jane_ads.geo_names.region_for", _region_for)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _resolve)
    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, city="Ikeja", pins=[
        GeoPin(name="Opebi", lat=6.6, lng=3.35),
        GeoPin(name="Alausa", lat=6.61, lng=3.36),
    ])
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(_plan(geo=geo), _auth()))

    geo_locations = mock_client.post.call_args_list[1].kwargs["json"]["targeting"]["geo_locations"]
    assert seen["asked"] == "Ikeja"                 # the city went in
    assert seen["guards"] == ["Lagos State", "Lagos State"]   # the STATE guarded
    assert geo_locations["neighborhoods"] == [{"key": "k_Opebi"}, {"key": "k_Alausa"}]
    assert "countries" not in geo_locations         # never silently nationwide


# ── An audience Meta will not deliver to ──────────────────────────────────────

def test_a_too_narrow_audience_widens_instead_of_failing_the_launch(monkeypatch):
    """Live-reported: Opebi + Allen Avenue + Ogba as NAMED neighbourhoods estimated a
    reach of 1,000 and Meta refused the ad set outright (subcode 2446395). Named areas
    carry their real boundaries, far smaller than the radius pins they replaced."""
    async def _region_for(place, access_token="", timeout=8.0):
        return "Lagos State"

    async def _resolve(name, expected_region, access_token="", timeout=8.0):
        kind = "city" if name == "Ikeja" else "neighborhood"
        return {"type": kind, "key": f"k_{name}", "name": name, "region": "Lagos State"}

    monkeypatch.setattr("app.agents.jane_ads.geo_names.region_for", _region_for)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _resolve)

    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, city="Ikeja",
                  pins=[GeoPin(name="Opebi"), GeoPin(name="Ogba")])
    too_narrow = {"error": {"code": 100, "error_subcode": 2446395,
                            "message": "The configured audience is not valid"}}
    responses = [{"id": "cmp_1"}, too_narrow, {"id": "adset_1"},
                 {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        out = _run(_adapter().launch_campaign(_plan(geo=geo), _auth()))

    assert out.campaign_id == "cmp_1"          # the launch SUCCEEDS
    first = mock_client.post.call_args_list[1].kwargs["json"]["targeting"]["geo_locations"]
    second = mock_client.post.call_args_list[2].kwargs["json"]["targeting"]["geo_locations"]
    assert "neighborhoods" in first            # tried the tight pockets first
    assert second == {"cities": [{"key": "k_Ikeja"}]}   # widened one rung, not to NG


def test_widening_keeps_everything_that_is_not_geography(monkeypatch):
    """Only the geography widens. Losing the interests or the age range while chasing
    reach would change the audience the client actually approved."""
    from app.agents.jane_ads.geo import widen_targeting

    async def _region_for(place, access_token="", timeout=8.0):
        return "Lagos State"

    async def _resolve(name, expected_region, access_token="", timeout=8.0):
        return {"type": "city", "key": "k_city", "name": name, "region": "Lagos State"}

    monkeypatch.setattr("app.agents.jane_ads.geo_names.region_for", _region_for)
    monkeypatch.setattr("app.agents.jane_ads.geo_names.resolve_named_location", _resolve)
    out = _run(widen_targeting(
        {"geo_locations": {"neighborhoods": [{"key": "1"}]},
         "age_min": 25, "genders": [2], "flexible_spec": [{"interests": [{"id": "9"}]}]},
        "Ikeja"))
    assert out["age_min"] == 25
    assert out["genders"] == [2]
    assert out["flexible_spec"] == [{"interests": [{"id": "9"}]}]
    assert out["geo_locations"] == {"cities": [{"key": "k_city"}]}


def test_widening_stops_at_the_country(monkeypatch):
    """Nothing is broader than nationwide, so it must give up rather than loop."""
    from app.agents.jane_ads.geo import widen_targeting

    assert _run(widen_targeting({"geo_locations": {"countries": ["NG"]}}, "Ikeja")) is None


# ── Multi-inbox messaging, and falling back from it ──────────────────────────

_MULTI_REFUSED = {"error": {"message": "Instagram account not connected to this Page",
                            "code": 100, "error_subcode": 2490001}}


def test_a_refused_multi_inbox_ad_falls_back_to_the_whatsapp_only_ad_set():
    """A Page that hasn't connected an inbox can't run the multi-inbox format. That
    must degrade to the WhatsApp-only ad set that already worked, not fail the launch."""
    responses = [{"id": "cmp_1"}, _MULTI_REFUSED, {"id": "adset_1"},
                 {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(_adapter().launch_campaign(_plan(), _auth()))
    assert result.campaign_id == "cmp_1"
    multi = mock_client.post.call_args_list[1].kwargs["json"]
    fallback = mock_client.post.call_args_list[2].kwargs["json"]
    assert multi["destination_type"] == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER_WHATSAPP"
    assert fallback["destination_type"] == "WHATSAPP"
    creative = mock_client.post.call_args_list[3].kwargs["json"]
    assert "asset_feed_spec" not in creative            # a WhatsApp-only ad, not half of one


def test_a_non_routing_failure_on_the_multi_inbox_ad_surfaces_as_itself():
    """A bad bid fails the WhatsApp-only create identically. Retrying would cost a call
    and log a misleading reason — it surfaces, and the campaign is rolled back."""
    responses = [{"id": "cmp_1"},
                 {"error": {"message": "Invalid bid strategy", "code": 100,
                            "error_subcode": 2490487}},
                 {"success": True}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(MetaAPIError, match="ad set creation"):
            _run(_adapter().launch_campaign(_plan(), _auth()))
    assert mock_client.post.await_count == 2              # campaign + ONE ad set attempt
    assert mock_client.delete.await_count == 1


def test_the_multi_inbox_video_creative_carries_a_headline_and_welcome_message():
    plan = _plan(creative=AdCreative(image_url="https://cdn/ad.mp4", is_video=True,
                                     headline="Book your spot", primary_text="p"))
    responses = [
        {"id": "vid_1"}, {"status": {"video_status": "ready"}},
        {"data": [{"uri": "https://thumb/1.jpg", "is_preferred": True}]},
        {"id": "cmp_1"}, {"id": "adset_1"}, {"id": "creative_1"}, {"id": "ad_1"},
    ]
    with patch("httpx.AsyncClient") as MockClient, \
         patch("app.agents.jane_ads.adapters.meta.asyncio.sleep", new=AsyncMock()):
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(_adapter().launch_campaign(plan, _auth()))
    video = mock_client.post.call_args_list[3].kwargs["json"]["object_story_spec"]["video_data"]
    assert video["title"] == "Book your spot"
    assert video["call_to_action"]["value"]["app_destination"] == "MESSENGER"
    assert "ice_breakers" in video["page_welcome_message"]


def test_a_refused_optional_creative_piece_is_dropped_not_fatal():
    """The ads system user is not always granted the Page's Instagram account, and
    Meta refuses the whole creative for it (code 200). The ad launches without that
    one piece rather than not at all."""
    refused = {"error": {"message": "Permission error", "code": 200, "error_subcode": 1487194}}
    responses = [{"id": "cmp_1"}, {"id": "adset_1"}, refused, {"id": "creative_1"}, {"id": "ad_1"}]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(_adapter().launch_campaign(_plan(), _auth()))
    assert result.campaign_id == "cmp_1"
    retried = mock_client.post.call_args_list[3].kwargs["json"]
    assert "instagram_user_id" not in retried["object_story_spec"]
    assert "asset_feed_spec" in retried          # only the first piece was shed


def test_a_page_without_messaging_access_relaunches_as_the_whatsapp_only_ad():
    """A client Page shared for ADVERTISE only can't carry a Messenger-destination ad
    — Meta refuses the creative as a permission error however much is shed. That must
    end as the WhatsApp-only ad that worked before, never a failed launch."""
    refused = {"error": {"message": "Permission error", "code": 200, "error_subcode": 1487194}}
    responses = [
        {"id": "cmp_1"}, {"id": "adset_1"},          # multi-inbox ad set accepted
        refused, refused, refused, refused, refused, refused,   # creative + 5 sheds refused
        {"success": True},                           # rollback: the campaign (no creative exists)
        {"id": "cmp_2"}, {"id": "adset_2"}, {"id": "creative_2"}, {"id": "ad_2"},
    ]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(_adapter().launch_campaign(_plan(), _auth()))
    assert result.campaign_id == "cmp_2"
    relaunch_adset = [c.kwargs["json"] for c in mock_client.post.call_args_list
                      if c.kwargs.get("json", {}).get("campaign_id") == "cmp_2"][0]
    assert relaunch_adset["destination_type"] == "WHATSAPP"
