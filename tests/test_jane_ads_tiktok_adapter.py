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
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads.adapters.tiktok import (
    TikTokAdsAdapter,
    TikTokAdsAPIError,
    _force_jpg_delivery,
    _force_tiktok_video_ratio,
    _resolve_tiktok_interests,
    _resolve_tiktok_locations,
    _tiktok_age_groups_for,
    _tiktok_age_range_from_groups,
    _tiktok_gender_choice,
    _tiktok_gender_for,
    _tiktok_interest_names,
    _tiktok_location_names,
    _tiktok_schedule_days,
    _tiktok_schedule_has_ended,
    _video_thumbnail_url,
)
from app.agents.jane_ads.models import (
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
    _get_authorized_identity actually uses (jane_ads_tiktok_identity)."""

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
    # Separate from the sequenced POST/GET responses above — the video-warm-up
    # HEAD call doesn't consume from that list, its return value is never read.
    client.head = AsyncMock(return_value=AsyncMock())
    return client


def _adapter(db=None) -> TikTokAdsAdapter:
    return TikTokAdsAdapter(db or FakeDb(), advertiser_id="adv123", access_token="tok")


# campaign(POST), video upload(POST), cover image upload(POST), ad group(POST),
# identity lookup(GET), ad(POST) — 6 calls total. Identity lookup replaced the
# old logo-upload+identity-create POST pair after TikTok deprecated Custom
# Identity (see adapters/tiktok.py's module comment) — now a single
# GET /identity/get/ call. Cover image upload is new: confirmed live
# (2026-09-16) ad/create rejects a SINGLE_VIDEO creative without an
# image_ids entry ("You must upload an image."), even though it's video-only.
_HAPPY_RESPONSES = [
    {"code": 0, "message": "OK", "data": {"campaign_id": "111"}},
    {"code": 0, "message": "OK", "data": [{"video_id": "vid_999", "video_cover_url": "https://cdn.example.com/cover.jpg"}]},
    {"code": 0, "message": "OK", "data": {"image_id": "img_888"}},
    {"code": 0, "message": "OK", "data": {"adgroup_id": "222"}},
    {"code": 0, "message": "OK", "data": {"identity_list": [
        {"identity_id": "identity_777", "identity_authorized_bc_id": "bc_555", "available_status": "AVAILABLE",
         "username": "uri.creative", "display_name": "uricreative"},
    ]}},
    {"code": 0, "message": "OK", "data": {"ad_ids": ["333"]}},
]


# ── Real audience targeting: gender/age mapping ─────────────────────────────────
# Confirmed 2026-09-25 against TikTok's own Enumerations reference.

def test_gender_maps_metas_codes_to_tiktoks_enum():
    assert _tiktok_gender_for({}) == "GENDER_UNLIMITED"
    assert _tiktok_gender_for({"genders": []}) == "GENDER_UNLIMITED"
    assert _tiktok_gender_for({"genders": [1]}) == "GENDER_MALE"
    assert _tiktok_gender_for({"genders": [2]}) == "GENDER_FEMALE"


def test_age_groups_covers_every_bucket_that_overlaps_the_range():
    # 25-45 genuinely overlaps three buckets (25-34, 35-44, 45-54 all contain
    # part of that range) — picking just one would be arbitrary.
    assert _tiktok_age_groups_for({"age_min": 25, "age_max": 45}) == [
        "AGE_25_34", "AGE_35_44", "AGE_45_54",
    ]


def test_age_groups_defaults_to_every_bucket_when_unspecified():
    assert _tiktok_age_groups_for({}) == [
        "AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100",
    ]


def test_age_groups_single_bucket_for_a_narrow_range():
    assert _tiktok_age_groups_for({"age_min": 55, "age_max": 65}) == ["AGE_55_100"]


# ── Real audience targeting: location resolution ────────────────────────────────

_REGION_TREE_RESPONSE = {
    "code": 0, "message": "OK",
    "data": {
        "region_info": [
            {"location_id": "2328926", "name": "Nigeria", "parent_id": "0",
             "region_code": "NG", "level": "COUNTRY", "next_level_ids": ["1001", "1002"]},
            {"location_id": "1001", "name": "Lagos", "parent_id": "2328926",
             "level": "PROVINCE", "next_level_ids": ["2001"]},
            {"location_id": "1002", "name": "Abuja", "parent_id": "2328926",
             "level": "PROVINCE", "next_level_ids": []},
            {"location_id": "2001", "name": "Ikeja", "parent_id": "1001",
             "level": "CITY", "next_level_ids": []},
        ],
    },
}


def test_resolve_tiktok_locations_matches_exact_and_partial_names():
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        resp = AsyncMock()
        resp.json = lambda: _REGION_TREE_RESPONSE
        mock_client.get = AsyncMock(return_value=resp)
        MockClient.return_value.__aenter__.return_value = mock_client
        resolved, rejected = _run(_resolve_tiktok_locations(["Ikeja", "Lagos"], "adv123", "tok"))
    assert resolved == [
        {"name": "Ikeja", "location_id": "2001"},
        {"name": "Lagos", "location_id": "1001"},
    ]
    assert rejected == []


def test_resolve_tiktok_locations_reports_unmatched_names():
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        resp = AsyncMock()
        resp.json = lambda: _REGION_TREE_RESPONSE
        mock_client.get = AsyncMock(return_value=resp)
        MockClient.return_value.__aenter__.return_value = mock_client
        resolved, rejected = _run(_resolve_tiktok_locations(["Nowhereville"], "adv123", "tok"))
    assert resolved == []
    assert rejected == ["Nowhereville"]


def test_resolve_tiktok_locations_is_a_noop_on_no_names():
    resolved, rejected = _run(_resolve_tiktok_locations([], "adv123", "tok"))
    assert resolved == [] and rejected == []


def test_resolve_tiktok_locations_falls_back_cleanly_on_api_error():
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        resp = AsyncMock()
        resp.json = lambda: {"code": 40001, "message": "invalid advertiser_id"}
        mock_client.get = AsyncMock(return_value=resp)
        MockClient.return_value.__aenter__.return_value = mock_client
        resolved, rejected = _run(_resolve_tiktok_locations(["Ikeja"], "adv123", "tok"))
    # Nothing resolved, but every requested name comes back as "rejected" rather
    # than raising — the caller (launch_campaign) falls back to Nigeria-wide.
    assert resolved == []
    assert rejected == ["Ikeja"]


# ── Interests ─────────────────────────────────────────────────────────────────

_INTEREST_CATEGORIES_RESPONSE = {
    "code": 0, "message": "OK",
    "data": {
        "interest_categories": [
            {"interest_category_id": "10", "interest_category_name": "Education",
             "level": 1, "sub_category_ids": ["10100"], "placements": [], "special_industries": []},
            {"interest_category_id": "11", "interest_category_name": "Vehicles & Transportation",
             "level": 1, "sub_category_ids": [], "placements": [], "special_industries": []},
            {"interest_category_id": "10100", "interest_category_name": "Online Courses",
             "level": 2, "sub_category_ids": [], "placements": [], "special_industries": []},
        ],
    },
}


def test_resolve_tiktok_interests_matches_exact_and_partial_names():
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        resp = AsyncMock()
        resp.json = lambda: _INTEREST_CATEGORIES_RESPONSE
        mock_client.get = AsyncMock(return_value=resp)
        MockClient.return_value.__aenter__.return_value = mock_client
        resolved, rejected = _run(_resolve_tiktok_interests(
            ["Education", "vehicles"], "adv123", "tok"))
    assert resolved == [
        {"name": "Education", "interest_category_id": "10"},
        {"name": "Vehicles & Transportation", "interest_category_id": "11"},
    ]
    assert rejected == []


def test_resolve_tiktok_interests_reports_unmatched_names():
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        resp = AsyncMock()
        resp.json = lambda: _INTEREST_CATEGORIES_RESPONSE
        mock_client.get = AsyncMock(return_value=resp)
        MockClient.return_value.__aenter__.return_value = mock_client
        resolved, rejected = _run(_resolve_tiktok_interests(["Astrology"], "adv123", "tok"))
    assert resolved == []
    assert rejected == ["Astrology"]


def test_resolve_tiktok_interests_is_a_noop_on_no_names():
    resolved, rejected = _run(_resolve_tiktok_interests([], "adv123", "tok"))
    assert resolved == [] and rejected == []


def test_resolve_tiktok_interests_falls_back_cleanly_on_api_error():
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        resp = AsyncMock()
        resp.json = lambda: {"code": 40001, "message": "invalid advertiser_id"}
        mock_client.get = AsyncMock(return_value=resp)
        MockClient.return_value.__aenter__.return_value = mock_client
        resolved, rejected = _run(_resolve_tiktok_interests(["Education"], "adv123", "tok"))
    assert resolved == []
    assert rejected == ["Education"]


def test_tiktok_interest_names_resolves_ids_back_to_names():
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        resp = AsyncMock()
        resp.json = lambda: _INTEREST_CATEGORIES_RESPONSE
        mock_client.get = AsyncMock(return_value=resp)
        MockClient.return_value.__aenter__.return_value = mock_client
        names = _run(_tiktok_interest_names(["10", "11"], "adv123", "tok"))
    assert names == ["Education", "Vehicles & Transportation"]


def test_tiktok_interest_names_falls_back_to_raw_ids_on_lookup_failure():
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        resp = AsyncMock()
        resp.json = lambda: {"code": 40001, "message": "nope"}
        mock_client.get = AsyncMock(return_value=resp)
        MockClient.return_value.__aenter__.return_value = mock_client
        names = _run(_tiktok_interest_names(["10"], "adv123", "tok"))
    assert names == ["10"]


def test_tiktok_interest_names_is_a_noop_on_no_ids():
    assert _run(_tiktok_interest_names([], "adv123", "tok")) == []


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
    assert mock_client.post.call_count == 5
    assert mock_client.get.call_count == 1

    campaign_json = mock_client.post.call_args_list[0].kwargs["json"]
    assert campaign_json["operation_status"] == "DISABLE"
    assert campaign_json["objective_type"] == "TRAFFIC"

    # No transformation applies to this non-Cloudinary URL, so there's nothing to
    # warm up — the HEAD pre-fetch must not fire needlessly.
    assert mock_client.head.call_count == 0

    video_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert video_json["upload_type"] == "UPLOAD_BY_URL"
    assert video_json["video_url"] == "https://cdn.example.com/clip.mp4"

    cover_json = mock_client.post.call_args_list[2].kwargs["json"]
    assert cover_json["upload_type"] == "UPLOAD_BY_URL"
    assert cover_json["image_url"] == "https://cdn.example.com/cover.jpg"

    adgroup_json = mock_client.post.call_args_list[3].kwargs["json"]
    assert adgroup_json["operation_status"] == "DISABLE"
    assert adgroup_json["budget"] == 70_000
    assert adgroup_json["campaign_id"] == "111"
    assert adgroup_json["bid_type"] == "BID_TYPE_NO_BID"
    assert adgroup_json["pacing"] == "PACING_MODE_SMOOTH"
    # No geo pins and no audience_targeting on this default plan — the honest
    # fallback (Nigeria-wide, no gender/age restriction) rather than an empty
    # or fabricated targeting. No extra /tool/region/ call either, since there
    # was nothing to resolve (get.call_count stays 1 below, identity only).
    assert adgroup_json["location_ids"] == ["2328926"]
    assert adgroup_json["gender"] == "GENDER_UNLIMITED"
    assert adgroup_json["age_groups"] == [
        "AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100",
    ]

    identity_params = mock_client.get.call_args_list[0].kwargs["params"]
    assert identity_params["identity_type"] == "BC_AUTH_TT"
    assert identity_params["advertiser_id"] == "adv123"

    ad_json = mock_client.post.call_args_list[4].kwargs["json"]
    assert ad_json["operation_status"] == "DISABLE"
    creative = ad_json["creatives"][0]
    assert creative["video_id"] == "vid_999"
    assert creative["image_ids"] == ["img_888"]
    assert creative["landing_page_url"] == 'https://wa.me/2348031234567?text=Hi%21%20I%20saw%20your%20ad%20and%20I%27m%20interested%20%E2%80%94%20tell%20me%20more%3F'
    assert creative["identity_id"] == "identity_777"
    assert creative["identity_type"] == "BC_AUTH_TT"
    assert creative["identity_authorized_bc_id"] == "bc_555"

    # A second launch must reuse the cached identity — no repeat lookup call.
    mock_client2 = _mock_client([
        {"code": 0, "message": "OK", "data": {"campaign_id": "444"}},
        {"code": 0, "message": "OK", "data": [{"video_id": "vid_000", "video_cover_url": "https://cdn.example.com/cover2.jpg"}]},
        {"code": 0, "message": "OK", "data": {"image_id": "img_000"}},
        {"code": 0, "message": "OK", "data": {"adgroup_id": "555"}},
        {"code": 0, "message": "OK", "data": {"ad_ids": ["666"]}},
    ])
    with patch("httpx.AsyncClient") as MockClient2:
        MockClient2.return_value.__aenter__.return_value = mock_client2
        _run(adapter.launch_campaign(_plan(business_id="b2"), _auth()))
    assert mock_client2.post.call_count == 5
    assert mock_client2.get.call_count == 0
    ad_json_2 = mock_client2.post.call_args_list[4].kwargs["json"]
    assert ad_json_2["creatives"][0]["identity_id"] == "identity_777"
    assert ad_json_2["creatives"][0]["identity_authorized_bc_id"] == "bc_555"

    record = _run(db["jane_ads_tiktok_campaigns"].find_one({"campaign_id": "111"}))
    assert record["ad_id"] == "333"
    assert record["business_id"] == "b1"
    assert record["last_click_count"] == 0


def test_launch_campaign_uses_real_audience_targeting_when_the_plan_has_it():
    # campaign(POST), video(POST), cover(POST), region lookup(GET), interest
    # lookup(GET), adgroup(POST), identity(GET), ad(POST) — the two new lookups
    # land between the creative upload and adgroup/create, exactly where
    # they're inserted in launch_campaign, since _mock_client's .get/.post
    # share one response queue.
    responses = [
        {"code": 0, "message": "OK", "data": {"campaign_id": "111"}},
        {"code": 0, "message": "OK", "data": [{"video_id": "vid_999", "video_cover_url": "https://cdn.example.com/cover.jpg"}]},
        {"code": 0, "message": "OK", "data": {"image_id": "img_888"}},
        _REGION_TREE_RESPONSE,
        _INTEREST_CATEGORIES_RESPONSE,
        {"code": 0, "message": "OK", "data": {"adgroup_id": "222"}},
        {"code": 0, "message": "OK", "data": {"identity_list": [
            {"identity_id": "identity_777", "identity_authorized_bc_id": "bc_555", "available_status": "AVAILABLE",
             "username": "uri.creative", "display_name": "uricreative"},
        ]}},
        {"code": 0, "message": "OK", "data": {"ad_ids": ["333"]}},
    ]
    db = FakeDb()
    adapter = _adapter(db)
    plan = _plan(
        geo=GeoPlan(mode=GeoMode.OWN_RADIUS, city="Lagos", pins=[GeoPin(name="Ikeja", lat=6.6, lng=3.35)]),
        audience_targeting={
            "age_min": 25, "age_max": 45, "genders": [2],
            "flexible_spec": [{"interests": [{"id": "meta-id-1", "name": "Education"}]}],
        },
    )
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(plan, _auth()))

    assert result.campaign_id == "111"
    assert mock_client.post.call_count == 5
    assert mock_client.get.call_count == 3  # region lookup + interest lookup + identity lookup

    adgroup_json = mock_client.post.call_args_list[3].kwargs["json"]
    assert adgroup_json["location_ids"] == ["2001"]  # Ikeja, resolved
    assert adgroup_json["gender"] == "GENDER_FEMALE"
    assert adgroup_json["age_groups"] == ["AGE_25_34", "AGE_35_44", "AGE_45_54"]
    # Meta's own interest id ("meta-id-1") is irrelevant here — only the name
    # was read, then resolved fresh against TikTok's own catalogue.
    assert adgroup_json["interest_category_ids"] == ["10"]


def test_launch_campaign_omits_interest_category_ids_when_none_given():
    # No flexible_spec on this plan's audience_targeting at all — must not send
    # an interest lookup call, and must not send the key rather than an empty list.
    responses = [
        {"code": 0, "message": "OK", "data": {"campaign_id": "111"}},
        {"code": 0, "message": "OK", "data": [{"video_id": "vid_999", "video_cover_url": "https://cdn.example.com/cover.jpg"}]},
        {"code": 0, "message": "OK", "data": {"image_id": "img_888"}},
        {"code": 0, "message": "OK", "data": {"adgroup_id": "222"}},
        {"code": 0, "message": "OK", "data": {"identity_list": [
            {"identity_id": "identity_777", "identity_authorized_bc_id": "bc_555", "available_status": "AVAILABLE",
             "username": "uri.creative", "display_name": "uricreative"},
        ]}},
        {"code": 0, "message": "OK", "data": {"ad_ids": ["333"]}},
    ]
    adapter = _adapter()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(_plan(), _auth()))

    assert result.campaign_id == "111"
    assert mock_client.get.call_count == 1  # identity lookup only
    adgroup_json = mock_client.post.call_args_list[3].kwargs["json"]
    assert "interest_category_ids" not in adgroup_json


# ── Carousel Ads (image-only path, no video) ────────────────────────────────────────
# TikTok has no single-static-image ad unit — a photo-only business reaches TikTok via
# a Carousel Ad (2+ images) instead. campaign(POST), image upload x2(POST), ad
# group(POST), identity lookup(GET), music lookup(GET), ad(POST) — same 5 POST as the
# video happy path (carousel's 2 image uploads replace video's video+cover uploads),
# plus a 2nd GET: TikTok's own /ad/create/ reference confirms music_id is REQUIRED
# for a Standard Carousel Non-Spark Ad (our exact scenario) — live-caught 2026-09-22
# as "The source of this post is invalid" before this was added.
_CAROUSEL_RESPONSES = [
    {"code": 0, "message": "OK", "data": {"campaign_id": "111"}},
    {"code": 0, "message": "OK", "data": {"image_id": "img_a"}},
    {"code": 0, "message": "OK", "data": {"image_id": "img_b"}},
    {"code": 0, "message": "OK", "data": {"adgroup_id": "222"}},
    {"code": 0, "message": "OK", "data": {"identity_list": [
        {"identity_id": "identity_777", "identity_authorized_bc_id": "bc_555", "available_status": "AVAILABLE",
         "username": "uri.creative", "display_name": "uricreative"},
    ]}},
    {"code": 0, "message": "OK", "data": {"musics": [{"music_id": "music_999", "name": "Upbeat Track"}]}},
    {"code": 0, "message": "OK", "data": {"ad_ids": ["333"]}},
]


def _carousel_plan(**kw) -> CampaignPlan:
    kw.setdefault(
        "creative",
        AdCreative(
            image_url="", is_video=False,
            carousel_image_urls=["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"],
            headline="Fresh Cuts Daily", primary_text="Book on WhatsApp today",
        ),
    )
    return _plan(**kw)


def test_launch_campaign_carousel_happy_path():
    adapter = _adapter()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_CAROUSEL_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(_carousel_plan(), _auth()))

    assert result.campaign_id == "111"
    assert mock_client.post.call_count == 5
    assert mock_client.get.call_count == 2  # identity lookup + music lookup

    img1_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert img1_json["image_url"] == "https://cdn.example.com/a.jpg"
    img2_json = mock_client.post.call_args_list[2].kwargs["json"]
    assert img2_json["image_url"] == "https://cdn.example.com/b.jpg"

    music_params = mock_client.get.call_args_list[1].kwargs["params"]
    assert music_params["music_scene"] == "CAROUSEL_ADS"
    assert music_params["advertiser_id"] == "adv123"

    ad_json = mock_client.post.call_args_list[4].kwargs["json"]
    creative = ad_json["creatives"][0]
    assert creative["ad_format"] == "CAROUSEL_ADS"
    assert creative["image_ids"] == ["img_a", "img_b"]
    assert creative["music_id"] == "music_999"
    assert "video_id" not in creative


def test_launch_campaign_carousel_forces_jpg_on_cloudinary_urls():
    # Live-caught 2026-09-22: a real WEBP upload got "Invalid params: image cannot
    # be decoded" from TikTok, which only accepts JPG/PNG for Carousel Ads. Every
    # slide's URL sent to TikTok must carry Cloudinary's f_jpg delivery
    # transformation, regardless of the stored file's real format.
    adapter = _adapter()
    plan = _carousel_plan(
        creative=AdCreative(
            image_url="",
            is_video=False,
            carousel_image_urls=[
                "https://res.cloudinary.com/demo/image/upload/v1700000000/uri-ads/a.webp",
                "https://res.cloudinary.com/demo/image/upload/v1700000000/uri-ads/b.webp",
            ],
        )
    )
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_CAROUSEL_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(adapter.launch_campaign(plan, _auth()))

    img1_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert img1_json["image_url"] == "https://res.cloudinary.com/demo/image/upload/f_jpg/v1700000000/uri-ads/a.webp"
    img2_json = mock_client.post.call_args_list[2].kwargs["json"]
    assert img2_json["image_url"] == "https://res.cloudinary.com/demo/image/upload/f_jpg/v1700000000/uri-ads/b.webp"


def test_force_jpg_delivery_inserts_transformation_on_cloudinary_urls():
    assert _force_jpg_delivery("https://res.cloudinary.com/demo/image/upload/v1/uri-ads/x.webp") == (
        "https://res.cloudinary.com/demo/image/upload/f_jpg/v1/uri-ads/x.webp"
    )


def test_force_jpg_delivery_is_a_noop_on_non_cloudinary_urls():
    # Must never raise or mangle a URL shape it doesn't recognise.
    assert _force_jpg_delivery("https://cdn.example.com/a.jpg") == "https://cdn.example.com/a.jpg"
    assert _force_jpg_delivery("") == ""


def test_force_tiktok_video_ratio_inserts_transformation_on_cloudinary_urls():
    assert _force_tiktok_video_ratio("https://res.cloudinary.com/demo/video/upload/v1/uri-ads/clip.mp4") == (
        "https://res.cloudinary.com/demo/video/upload/c_fill,ar_9:16,g_auto/v1/uri-ads/clip.mp4"
    )


def test_force_tiktok_video_ratio_is_a_noop_on_non_cloudinary_urls():
    # Must never raise or mangle a URL shape it doesn't recognise.
    assert _force_tiktok_video_ratio("https://cdn.example.com/clip.mp4") == "https://cdn.example.com/clip.mp4"
    assert _force_tiktok_video_ratio("") == ""


def test_video_thumbnail_url_swaps_extension_to_jpg_on_cloudinary_urls():
    assert _video_thumbnail_url("https://res.cloudinary.com/demo/video/upload/v1/uri-ads/clip.mp4") == (
        "https://res.cloudinary.com/demo/video/upload/v1/uri-ads/clip.jpg"
    )


def test_video_thumbnail_url_is_a_noop_on_non_cloudinary_or_extensionless_urls():
    # Must never raise or mangle a URL shape it doesn't recognise.
    assert _video_thumbnail_url("https://cdn.example.com/clip.mp4") == "https://cdn.example.com/clip.mp4"
    assert _video_thumbnail_url("https://res.cloudinary.com/demo/video/upload/v1/uri-ads/clip") == (
        "https://res.cloudinary.com/demo/video/upload/v1/uri-ads/clip"
    )
    assert _video_thumbnail_url("") == ""


def test_launch_campaign_forces_tiktok_ratio_on_cloudinary_video():
    # Live-caught 2026-09-22: a real uploaded video (480x848 - close to but not
    # exactly 9:16) got "Cannot be delivered to TikTok: Video ratio must be
    # 16:9/1:1/9:16" in TikTok Ads Manager, even though the ad itself was created
    # successfully. The video URL sent to TikTok must carry Cloudinary's
    # c_fill,ar_9:16,g_auto delivery transformation, regardless of the source
    # file's real dimensions.
    adapter = _adapter()
    plan = _plan(creative=AdCreative(
        image_url="https://res.cloudinary.com/demo/video/upload/v1700000000/uri-ads/clip.mp4",
        is_video=True, headline="Fresh Cuts Daily", primary_text="Book on WhatsApp today",
    ))
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(adapter.launch_campaign(plan, _auth()))

    # Live-caught 2026-09-22, part 2: TikTok's own fetch then failed with "video
    # upload: Failed to fetch url data" — Cloudinary transcodes a video
    # transformation on its first request, which can outrun TikTok's fetch
    # timeout for a derivative nobody has requested before. The adapter must warm
    # the EXACT SAME transformed URL itself first, before TikTok ever sees it.
    assert mock_client.head.call_count == 1
    warmed_url = mock_client.head.call_args_list[0].args[0]
    assert warmed_url == "https://res.cloudinary.com/demo/video/upload/c_fill,ar_9:16,g_auto/v1700000000/uri-ads/clip.mp4"

    video_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert video_json["video_url"] == (
        "https://res.cloudinary.com/demo/video/upload/c_fill,ar_9:16,g_auto/v1700000000/uri-ads/clip.mp4"
    )


def test_launch_campaign_video_warm_up_timeout_does_not_block_the_launch():
    # Best-effort: a slow/failed warm-up must not sink an otherwise-real launch —
    # TikTok's own fetch may still land on a transcode that finished moments later.
    import httpx as httpx_module

    adapter = _adapter()
    plan = _plan(creative=AdCreative(
        image_url="https://res.cloudinary.com/demo/video/upload/v1700000000/uri-ads/clip.mp4",
        is_video=True, headline="Fresh Cuts Daily", primary_text="Book on WhatsApp today",
    ))
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        mock_client.head = AsyncMock(side_effect=httpx_module.TimeoutException("timed out"))
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.launch_campaign(plan, _auth()))

    assert result.campaign_id == "111"
    assert mock_client.head.call_count == 1


def test_launch_campaign_carousel_raises_clearly_when_no_music_found():
    adapter = _adapter()
    responses = list(_CAROUSEL_RESPONSES)
    responses[5] = {"code": 0, "message": "OK", "data": {"musics": []}}  # the music lookup, empty
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(TikTokAdsAPIError, match="No usable TikTok music found"):
            _run(adapter.launch_campaign(_carousel_plan(), _auth()))


def test_launch_campaign_single_carousel_image_still_raises():
    # TikTok's Carousel Ads need 2+ images — one lone photo is exactly the case
    # neither the video path nor the carousel path can serve.
    adapter = _adapter()
    plan = _carousel_plan(creative=AdCreative(
        image_url="", is_video=False, carousel_image_urls=["https://cdn.example.com/a.jpg"],
    ))
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_CAROUSEL_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(ValueError):
            _run(adapter.launch_campaign(plan, _auth()))
    assert mock_client.post.call_count == 0


def test_campaign_name_is_unique_across_launches_with_the_same_business_and_goal():
    # Live-caught 2026-09-21: TikTok rejected a second launch with "Campaign name
    # already exists" because the name had no uniqueness suffix at all.
    adapter = _adapter()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(list(_HAPPY_RESPONSES))
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(adapter.launch_campaign(_plan(), _auth()))
    name_1 = mock_client.post.call_args_list[0].kwargs["json"]["campaign_name"]

    with patch("httpx.AsyncClient") as MockClient2:
        mock_client_2 = _mock_client(list(_HAPPY_RESPONSES))
        MockClient2.return_value.__aenter__.return_value = mock_client_2
        _run(adapter.launch_campaign(_plan(), _auth()))
    name_2 = mock_client_2.post.call_args_list[0].kwargs["json"]["campaign_name"]

    assert name_1 != name_2
    assert name_1.startswith("JaneAds-b1-")


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
    # Campaign + video + cover image upload succeed, ad group creation fails ->
    # rollback should DELETE the already-created campaign, and the ORIGINAL
    # error (not a rollback error) propagates.
    responses = [
        {"code": 0, "message": "OK", "data": {"campaign_id": "111"}},
        {"code": 0, "message": "OK", "data": [{"video_id": "vid_999", "video_cover_url": "https://cdn.example.com/cover.jpg"}]},
        {"code": 0, "message": "OK", "data": {"image_id": "img_888"}},
        {"code": 40002, "message": "ad group rejected: invalid location_ids"},
        {"code": 0, "message": "OK"},  # the rollback's own DELETE-status call
    ]
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client(responses)
        MockClient.return_value.__aenter__.return_value = mock_client
        with pytest.raises(TikTokAdsAPIError, match="ad group rejected"):
            _run(adapter.launch_campaign(_plan(), _auth()))

    rollback_json = mock_client.post.call_args_list[4].kwargs["json"]
    assert rollback_json["operation_status"] == "DELETE"
    assert rollback_json["campaign_ids"] == ["111"]
    # Live-caught 2026-09-22: the URL itself had campaign/update/status/ — a real
    # TikTok 404, since the correct path is campaign/status/update/ (confirmed live
    # earlier this session deleting real diagnostic campaigns). The JSON-body
    # assertions above would pass against either path, which is exactly how this
    # shipped unnoticed — assert the actual URL too.
    rollback_url = mock_client.post.call_args_list[4].args[0]
    assert rollback_url.endswith("/campaign/status/update/")


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

    # Live-caught 2026-09-23: campaign and adgroup status updates had the same
    # swapped-segment bug already found (and fixed) in the rollback path —
    # .../update/status/ 404s, the real endpoint is .../status/update/. Assert
    # the URLs directly so a regression here fails loudly instead of just
    # silently 404ing against a real account again.
    campaign_url = mock_client.post.call_args_list[0].args[0]
    assert campaign_url.endswith("/campaign/status/update/")
    campaign_json = mock_client.post.call_args_list[0].kwargs["json"]
    assert campaign_json["campaign_ids"] == ["111"]
    assert campaign_json["operation_status"] == "ENABLE"

    adgroup_url = mock_client.post.call_args_list[1].args[0]
    assert adgroup_url.endswith("/adgroup/status/update/")
    adgroup_json = mock_client.post.call_args_list[1].kwargs["json"]
    assert adgroup_json["adgroup_ids"] == ["222"]
    assert adgroup_json["operation_status"] == "ENABLE"

    ad_url = mock_client.post.call_args_list[2].args[0]
    assert ad_url.endswith("/ad/status/update/")
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


# ── Live targeting edit: reverse-mapping helpers ─────────────────────────────────

def test_gender_choice_reverses_gender_for():
    assert _tiktok_gender_choice("GENDER_MALE") == "men"
    assert _tiktok_gender_choice("GENDER_FEMALE") == "women"
    assert _tiktok_gender_choice("GENDER_UNLIMITED") == "all"
    assert _tiktok_gender_choice("") == "all"


def test_age_range_from_groups_reverses_age_groups_for():
    # A range that started as 25-45 became AGE_25_34+AGE_35_44+AGE_45_54 going
    # forward (per _tiktok_age_groups_for) — the reverse reconstructs the full
    # 25-54 span those three buckets cover, not the original 25-45 exactly
    # (TikTok only ever reports back whole buckets, never the original range).
    assert _tiktok_age_range_from_groups(["AGE_25_34", "AGE_35_44", "AGE_45_54"]) == (25, 54)


def test_age_range_from_groups_defaults_to_full_span_when_empty():
    assert _tiktok_age_range_from_groups([]) == (18, 100)


def test_tiktok_location_names_resolves_ids_back_to_names():
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        resp = AsyncMock()
        resp.json = lambda: _REGION_TREE_RESPONSE
        mock_client.get = AsyncMock(return_value=resp)
        MockClient.return_value.__aenter__.return_value = mock_client
        names = _run(_tiktok_location_names(["2001", "1001"], "adv123", "tok"))
    assert names == ["Ikeja", "Lagos"]


def test_tiktok_location_names_falls_back_to_raw_ids_on_lookup_failure():
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        resp = AsyncMock()
        resp.json = lambda: {"code": 40001, "message": "nope"}
        mock_client.get = AsyncMock(return_value=resp)
        MockClient.return_value.__aenter__.return_value = mock_client
        names = _run(_tiktok_location_names(["2001"], "adv123", "tok"))
    assert names == ["2001"]


def test_tiktok_location_names_is_a_noop_on_no_ids():
    assert _run(_tiktok_location_names([], "adv123", "tok")) == []


# ── Live targeting edit: fetch/update adgroup targeting ─────────────────────────

def test_fetch_adgroup_targeting_reads_the_live_adgroup():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "adgroup_id": "222"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{
            "code": 0, "message": "OK",
            "data": {"list": [{
                "adgroup_id": "222", "operation_status": "ENABLE",
                "gender": "GENDER_FEMALE", "age_groups": ["AGE_25_34", "AGE_35_44"],
                "location_ids": ["2001"],
            }]},
        }])
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.fetch_adgroup_targeting("111"))
    assert result["adgroup_id"] == "222"
    assert result["targeting"]["gender"] == "GENDER_FEMALE"
    assert result["targeting"]["age_groups"] == ["AGE_25_34", "AGE_35_44"]
    assert result["targeting"]["location_ids"] == ["2001"]
    params = mock_client.get.call_args.kwargs["params"]
    assert params["advertiser_id"] == "adv123"


def test_fetch_adgroup_targeting_raises_when_no_adgroup_recorded():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1"}
    adapter = _adapter(db)
    with pytest.raises(TikTokAdsAPIError, match="no ad group recorded"):
        _run(adapter.fetch_adgroup_targeting("111"))


def test_update_adgroup_targeting_writes_then_reads_back():
    adapter = _adapter()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([
            {"code": 0, "message": "OK", "data": {}},
            {"code": 0, "message": "OK", "data": {"list": [
                {"gender": "GENDER_MALE", "age_groups": ["AGE_18_24"], "location_ids": ["1001"]},
            ]}},
        ])
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.update_adgroup_targeting("222", {"gender": "GENDER_MALE"}))
    assert result == {"applied": True, "targeting": {
        "gender": "GENDER_MALE", "age_groups": ["AGE_18_24"], "location_ids": ["1001"],
        "interest_category_ids": [],
    }}
    write_json = mock_client.post.call_args.kwargs["json"]
    assert write_json["adgroup_id"] == "222"
    assert write_json["gender"] == "GENDER_MALE"
    # Only the changed key was sent — a partial update, not the whole ad group.
    assert "age_groups" not in write_json
    assert "location_ids" not in write_json


def test_update_adgroup_targeting_validate_only_makes_no_network_call():
    adapter = _adapter()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([])
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.update_adgroup_targeting("222", {"gender": "GENDER_MALE"}, validate_only=True))
    assert result == {"validated": True}
    assert mock_client.post.call_count == 0
    assert mock_client.get.call_count == 0


def test_delete_campaign_sends_delete_status():
    adapter = _adapter()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"code": 0, "message": "OK"}])
        MockClient.return_value.__aenter__.return_value = mock_client
        ok = _run(adapter.delete_campaign("111"))
    assert ok is True
    # Live-caught 2026-09-25: same swapped-segment bug already found (and
    # fixed) twice elsewhere in this file — .../campaign/update/status/ 404s,
    # the real endpoint is .../campaign/status/update/. Assert the URL
    # directly, not just the JSON body, which is exactly how this shipped
    # unnoticed the first two times.
    assert mock_client.post.call_args.args[0].endswith("/campaign/status/update/")
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


# ── "Keep it running": schedule helpers ──────────────────────────────────────

def test_schedule_days_counts_whole_days_between_tiktok_timestamps():
    assert _tiktok_schedule_days("2026-09-20 10:00:00", "2026-09-27 10:00:00") == 7


def test_schedule_days_is_zero_on_missing_or_unparseable_timestamps():
    assert _tiktok_schedule_days(None, "2026-09-27 10:00:00") == 0
    assert _tiktok_schedule_days("2026-09-20 10:00:00", None) == 0
    assert _tiktok_schedule_days("not a date", "2026-09-27 10:00:00") == 0


def test_schedule_has_ended_true_for_a_past_end_time():
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert _tiktok_schedule_has_ended(past) is True


def test_schedule_has_ended_false_for_a_future_end_time():
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert _tiktok_schedule_has_ended(future) is False


def test_schedule_has_ended_false_on_missing_or_unparseable_end_time():
    assert _tiktok_schedule_has_ended(None) is False
    assert _tiktok_schedule_has_ended("not a date") is False


# ── "Keep it running": fetch_adgroup_schedule / extend_adgroup ──────────────────

def test_fetch_adgroup_schedule_derives_daily_rate_from_the_total_budget():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1", "adgroup_id": "222", "ad_id": "333"}
    adapter = _adapter(db)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{
            "code": 0, "message": "OK",
            "data": {"list": [{
                "adgroup_id": "222", "operation_status": "ENABLE",
                "budget": 217000.0, "budget_mode": "BUDGET_MODE_TOTAL",
                "schedule_start_time": "2026-09-20 10:00:00",
                "schedule_end_time": "2026-09-27 10:00:00",
            }]},
        }])
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.fetch_adgroup_schedule("111"))
    assert result["adgroup_id"] == "222"
    assert result["budget_ngn"] == 217000.0
    assert result["daily_ngn"] == 31000.0  # 217,000 / 7 days
    assert result["has_ended"] is False


def test_fetch_adgroup_schedule_raises_when_no_adgroup_recorded():
    db = FakeDb()
    db["jane_ads_tiktok_campaigns"].docs["111"] = {"business_id": "b1"}
    adapter = _adapter(db)
    with pytest.raises(TikTokAdsAPIError, match="no ad group recorded"):
        _run(adapter.fetch_adgroup_schedule("111"))


def test_extend_adgroup_writes_new_budget_and_end_date_then_reads_back():
    adapter = _adapter()
    new_end = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([
            {"code": 0, "message": "OK", "data": {}},
            {"code": 0, "message": "OK", "data": {"list": [
                {"budget": 248000.0, "schedule_end_time": "2026-10-04 10:00:00"},
            ]}},
        ])
        MockClient.return_value.__aenter__.return_value = mock_client
        result = _run(adapter.extend_adgroup("222", 248000.0, new_end))
    assert result == {"budget_ngn": 248000.0, "end_time": "2026-10-04 10:00:00"}
    write_json = mock_client.post.call_args.kwargs["json"]
    assert write_json["adgroup_id"] == "222"
    assert write_json["budget"] == 248000.0
    assert write_json["schedule_end_time"] == "2026-10-04 10:00:00"
