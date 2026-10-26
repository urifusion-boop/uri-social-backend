"""
Regression tests for the TikTok issues found in the first real staging launch
(2026-10-26): an ad that came up ENABLED under a paused campaign, a Lagos campaign that
went Nigeria-wide, report calls TikTok rejected, and a carousel launched under Video Views.

These answer by ENDPOINT, not by call order. The older launch tests in
test_jane_ads_tiktok_adapter.py queue responses in a fixed sequence, which broke when the
adapter's call order changed on 2026-10-20 (identity lookup, adgroup before upload), so they
cannot say anything about launch behaviour any more.
"""
import json
import re
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads.adapters.tiktok import _report_campaign_filter, _tiktok_delivery_display
from app.agents.jane_ads.models import CampaignObjective, GeoMode, GeoPin, GeoPlan
from tests.test_jane_ads_tiktok_adapter import (
    FakeDb,
    _adapter,
    _auth,
    _carousel_plan,
    _plan,
    _run,
)

# TikTok's location tree, province -> city. It stops at city level, which is why
# neighbourhoods such as Ikeja GRA or Lekki Phase 1 never resolve.
_REGION_TREE = [
    {"location_id": "2328926", "name": "Nigeria", "region_code": "NG", "next_level_ids": ["2332453"]},
    {"location_id": "2332453", "name": "Lagos", "next_level_ids": ["9001"]},
    {"location_id": "9001", "name": "Ikorodu", "next_level_ids": []},
]

_DEFAULT_ANSWERS = {
    "/campaign/create/": {"code": 0, "message": "OK", "data": {"campaign_id": "111"}},
    "/adgroup/create/": {"code": 0, "message": "OK", "data": {"adgroup_id": "222"}},
    "/file/video/ad/upload/": {"code": 0, "message": "OK", "data": [
        {"video_id": "vid_1", "video_cover_url": "https://cdn.example.com/cover.jpg"}]},
    "/file/image/ad/upload/": {"code": 0, "message": "OK", "data": {"image_id": "img_1"}},
    "/identity/get/": {"code": 0, "message": "OK", "data": {"identity_list": [
        {"identity_id": "identity_1", "identity_authorized_bc_id": "bc_1", "available_status": "AVAILABLE"}]}},
    "/ad/create/": {"code": 0, "message": "OK", "data": {"ad_ids": ["333"]}},
    "/ad/status/update/": {"code": 0, "message": "OK", "data": {}},
    "/tool/region/": {"code": 0, "message": "OK", "data": {"region_info": _REGION_TREE}},
    "/tool/interest_category/": {"code": 0, "message": "OK", "data": {"interest_categories": []}},
    "/file/music/get/": {"code": 0, "message": "OK", "data": {"musics": [{"music_id": "music_1"}]}},
    "/report/integrated/get/": {"code": 0, "message": "OK", "data": {"list": []}},
}


class _RoutedClient:
    """A fake httpx client that answers by endpoint and records every call."""

    def __init__(self, answers=None):
        self.answers = {**_DEFAULT_ANSWERS, **(answers or {})}
        self.calls: list[tuple[str, str, dict]] = []
        self.head = AsyncMock(return_value=AsyncMock())

    def _respond(self, method, url, kwargs):
        endpoint = re.sub(r"^.*?/open_api/[^/]+", "", url)
        payload = kwargs.get("json") if method == "POST" else kwargs.get("params")
        self.calls.append((method, endpoint, payload or {}))
        answer = self.answers[endpoint]
        response = AsyncMock()
        response.json = lambda: answer
        return response

    async def post(self, url, **kwargs):
        return self._respond("POST", url, kwargs)

    async def get(self, url, **kwargs):
        return self._respond("GET", url, kwargs)

    def sent(self, endpoint):
        return [payload for _, ep, payload in self.calls if ep == endpoint]


def _launch(plan, answers=None):
    client = _RoutedClient(answers)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = client
        result = _run(_adapter().launch_campaign(plan, _auth()))
    return client, result


def _lagos_plan(**kw):
    geo = GeoPlan(
        mode=GeoMode.WATERING_HOLE, city="Lagos",
        pins=[GeoPin(name="Ikeja GRA", reason="x"), GeoPin(name="Lekki Phase 1", reason="x")],
    )
    return _plan(geo=geo, **kw)


# ── The new ad is switched off ────────────────────────────────────────────────

def test_new_ad_is_explicitly_disabled_after_creation():
    # ad/create's top-level operation_status was not honoured: the ad came up ENABLED
    # under a paused campaign and ad group.
    client, result = _launch(_plan())

    assert result.ad_ids == {"b1": "333"}
    updates = client.sent("/ad/status/update/")
    assert updates == [{
        "advertiser_id": "adv123", "adgroup_id": "222", "ad_ids": ["333"], "operation_status": "DISABLE",
    }]
    endpoints = [ep for _, ep, _ in client.calls]
    assert endpoints.index("/ad/status/update/") > endpoints.index("/ad/create/")


def test_a_failed_disable_does_not_fail_the_launch():
    # The campaign and ad group are already DISABLE, so an ad left on cannot deliver.
    # Failing a launch that otherwise worked would be worse than the mismatch.
    client, result = _launch(_plan(), {
        "/ad/status/update/": {"code": 40002, "message": "temporary failure", "data": {}},
    })

    assert result.launched is True
    assert result.ad_ids == {"b1": "333"}
    assert client.sent("/campaign/create/")[0]["operation_status"] == "DISABLE"
    assert client.sent("/adgroup/create/")[0]["operation_status"] == "DISABLE"


# ── Locations: fall back to the city, not the whole country ───────────────────

def test_unresolvable_neighbourhoods_fall_back_to_the_plans_city():
    # "Ikeja GRA" and "Lekki Phase 1" are not in TikTok's province/city tree.
    client, _ = _launch(_lagos_plan())

    assert client.sent("/adgroup/create/")[0]["location_ids"] == ["2332453"]  # Lagos, not Nigeria


def test_resolvable_areas_are_used_and_the_city_is_not_added():
    geo = GeoPlan(mode=GeoMode.WATERING_HOLE, city="Lagos",
                  pins=[GeoPin(name="Ikorodu", reason="x")])
    client, _ = _launch(_plan(geo=geo))

    assert client.sent("/adgroup/create/")[0]["location_ids"] == ["9001"]


def test_nigeria_wide_only_when_neither_the_areas_nor_the_city_resolve():
    geo = GeoPlan(mode=GeoMode.WATERING_HOLE, city="Atlantis",
                  pins=[GeoPin(name="Ikeja GRA", reason="x")])
    client, _ = _launch(_plan(geo=geo))

    assert client.sent("/adgroup/create/")[0]["location_ids"] == ["2328926"]


def test_a_plan_with_no_geo_stays_nigeria_wide():
    client, _ = _launch(_plan())

    assert client.sent("/adgroup/create/")[0]["location_ids"] == ["2328926"]


# ── Report filters ────────────────────────────────────────────────────────────

def test_report_filter_value_is_a_json_array_string():
    # TikTok: "when filter_type = IN, filter_value needs to be a valid JSON array
    # character string". A bare id failed every summary, spend and click report.
    filtering = json.loads(_report_campaign_filter("1880117055659538"))

    assert filtering == [{
        "field_name": "campaign_ids",
        "filter_type": "IN",
        "filter_value": '["1880117055659538"]',
    }]
    assert json.loads(filtering[0]["filter_value"]) == ["1880117055659538"]


def _seed_campaign(adapter):
    _run(adapter._db["jane_ads_tiktok_campaigns"].update_one(
        {"campaign_id": "111"},
        {"$set": {"campaign_id": "111", "adgroup_id": "222", "ad_id": "333", "business_id": "b1",
                  "last_click_count": 0}},
        upsert=True,
    ))


def test_every_report_call_the_adapter_makes_sends_the_array_filter():
    adapter = _adapter(FakeDb())
    _seed_campaign(adapter)
    client = _RoutedClient({
        "/report/integrated/get/": {"code": 0, "message": "OK", "data": {"list": [
            {"dimensions": {"campaign_id": "111", "ad_id": "333"},
             "metrics": {"spend": "10.0", "clicks": "4", "impressions": "100", "reach": "90"}},
        ]}},
        "/campaign/get/": {"code": 0, "message": "OK", "data": {"list": [
            {"campaign_id": "111", "operation_status": "DISABLE"}]}},
        "/adgroup/get/": {"code": 0, "message": "OK", "data": {"list": []}},
    })

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = client
        for call in (adapter.fetch_per_ad_spend, adapter.poll_conversations,
                     adapter.fetch_campaign_summary):
            try:
                _run(call("111"))
            except Exception:
                # What each method does with the rows is not under test here, only
                # the request it sent.
                pass

    reports = client.sent("/report/integrated/get/")
    assert len(reports) == 3, f"expected the three report calls, saw {len(reports)}"
    for params in reports:
        (condition,) = json.loads(params["filtering"])
        assert condition["field_name"] == "campaign_ids"
        assert json.loads(condition["filter_value"]) == ["111"]


# ── Objective vs creative, through a real launch ──────────────────────────────

def test_engagement_carousel_launches_under_traffic():
    # Engagement maps to VIDEO_VIEWS, which TikTok will not take a carousel under
    # ("ad creation: Objective type not supported", live 2026-10-26).
    # plan.objective (the client's ObjectivePicker choice) is what the adapter maps; the
    # PlatformPlan.objective is a different field and is not read here.
    client, result = _launch(_carousel_plan(objective=CampaignObjective.ENGAGEMENT))

    assert result.launched is True
    assert client.sent("/campaign/create/")[0]["objective_type"] == "TRAFFIC"
    assert client.sent("/ad/create/")[0]["creatives"][0]["ad_format"] == "CAROUSEL_ADS"


def test_engagement_video_still_launches_under_video_views():
    client, _ = _launch(_plan(objective=CampaignObjective.ENGAGEMENT))

    assert client.sent("/campaign/create/")[0]["objective_type"] == "VIDEO_VIEWS"


# ── What the client sees while a switched-on campaign is not delivering ────────
# Live 2026-10-26: TikTok Ads Manager said "Not delivered" while URI said "Active",
# because URI only read operation_status (ENABLE/DISABLE).

@pytest.mark.parametrize("ad_status,adgroup_status,label", [
    ("AD_STATUS_AUDIT", "ADGROUP_STATUS_DELIVERY_OK", "In review"),
    ("AD_STATUS_REAUDIT", "", "In review"),
    ("AD_STATUS_AUDIT_DENY", "ADGROUP_STATUS_DELIVERY_OK", "Needs changes"),
    ("AD_STATUS_DELIVERY_OK", "ADGROUP_STATUS_NOT_START", "Scheduled"),
    ("AD_STATUS_DELIVERY_OK", "ADGROUP_STATUS_TIME_DONE", "Ended"),
    ("AD_STATUS_DELIVERY_OK", "ADGROUP_STATUS_BALANCE_EXCEED", "Needs billing info"),
    ("AD_STATUS_DELIVERY_OK", "ADGROUP_STATUS_BUDGET_EXCEED", "Budget used"),
    ("AD_STATUS_DISABLE", "ADGROUP_STATUS_DELIVERY_OK", "Ad paused"),
    ("AD_STATUS_SOMETHING_NEW", "ADGROUP_STATUS_DELIVERY_OK", "Needs attention"),
])
def test_delivery_status_is_explained_in_the_clients_words(ad_status, adgroup_status, label):
    shown, note = _tiktok_delivery_display(ad_status, adgroup_status)

    assert shown == label
    assert note


def test_a_rejection_outranks_a_review_and_everything_else():
    # Several levels can disagree; the client needs the one that needs them to act.
    assert _tiktok_delivery_display("AD_STATUS_AUDIT_DENY", "ADGROUP_STATUS_AUDIT")[0] == "Needs changes"
    assert _tiktok_delivery_display("AD_STATUS_AUDIT", "ADGROUP_STATUS_NOT_START")[0] == "In review"


@pytest.mark.parametrize("ad_status,adgroup_status", [
    ("AD_STATUS_DELIVERY_OK", "ADGROUP_STATUS_DELIVERY_OK"),
    ("", ""),
    ("AD_STATUS_DELIVERY_OK", ""),
])
def test_a_delivering_or_unknown_campaign_gets_no_override(ad_status, adgroup_status):
    assert _tiktok_delivery_display(ad_status, adgroup_status) == ("", "")


def test_an_unrecognised_status_is_shown_in_tiktoks_own_words():
    label, note = _tiktok_delivery_display("AD_STATUS_FROZEN_BY_ADMIN", "ADGROUP_STATUS_DELIVERY_OK")

    assert label == "Needs attention"
    assert "frozen by admin" in note


def _summary(campaign_status, ad_status="", adgroup_status=""):
    adapter = _adapter(FakeDb())
    _seed_campaign(adapter)
    client = _RoutedClient({
        "/campaign/get/": {"code": 0, "message": "OK", "data": {"list": [
            {"campaign_id": "111", "operation_status": campaign_status}]}},
        "/ad/get/": {"code": 0, "message": "OK", "data": {"list": [
            {"ad_id": "333", "secondary_status": ad_status}]}},
        "/adgroup/get/": {"code": 0, "message": "OK", "data": {"list": [
            {"adgroup_id": "222", "secondary_status": adgroup_status}]}},
    })
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = client
        return client, _run(adapter.fetch_campaign_summary("111"))


def test_a_switched_on_campaign_in_review_stays_active_for_billing_but_shows_in_review():
    client, summary = _summary("ENABLE", "AD_STATUS_AUDIT", "ADGROUP_STATUS_DELIVERY_OK")

    # billing.py and monitoring.py treat "Active" as running — that must not change.
    assert summary["delivery"] == "Active"
    assert summary["delivery_display"] == "In review"
    assert "reviewing" in summary["delivery_note"]
    assert len(client.sent("/ad/get/")) == 1 and len(client.sent("/adgroup/get/")) == 1


def test_a_delivering_campaign_has_no_note():
    _, summary = _summary("ENABLE", "AD_STATUS_DELIVERY_OK", "ADGROUP_STATUS_DELIVERY_OK")

    assert summary["delivery"] == "Active"
    assert summary["delivery_display"] == ""
    assert summary["delivery_note"] == ""


def test_a_paused_campaign_costs_no_extra_calls():
    client, summary = _summary("DISABLE")

    assert summary["delivery"] == "Paused"
    assert summary["delivery_display"] == ""
    assert client.sent("/ad/get/") == [] and client.sent("/adgroup/get/") == []


def test_a_failed_status_lookup_never_costs_the_client_their_numbers():
    adapter = _adapter(FakeDb())
    _seed_campaign(adapter)
    client = _RoutedClient({
        "/campaign/get/": {"code": 0, "message": "OK", "data": {"list": [
            {"campaign_id": "111", "operation_status": "ENABLE"}]}},
        "/ad/get/": {"code": 40100, "message": "temporary failure", "data": {}},
        "/adgroup/get/": {"code": 40100, "message": "temporary failure", "data": {}},
        "/report/integrated/get/": {"code": 0, "message": "OK", "data": {"list": [
            {"metrics": {"spend": "6000.0", "clicks": "9", "impressions": "15304", "reach": "9000"}}]}},
    })
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = client
        summary = _run(adapter.fetch_campaign_summary("111"))

    assert summary["delivery"] == "Active"
    assert summary["delivery_display"] == ""
    assert summary["spend_ngn"] == 6000.0
    assert summary["impressions"] == 15304
