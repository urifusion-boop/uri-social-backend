"""
The messaging ad, built the way Ads Manager builds it (messaging_ad.py).

Diffed field by field against the one campaign on the account set up by hand —
which delivered cheaper conversations from better people than any Jane campaign —
every difference sat below the campaign level. These pin the copied values, so a
later edit cannot quietly drift Jane away from the setup that worked.
"""
import json

from app.agents.jane_ads import messaging_ad as M
from app.agents.jane_ads.adapters.meta import _with_city_radius


def test_one_ad_set_covers_all_three_inboxes():
    fields = M.adset_messaging_fields("PAGE1")
    assert fields["destination_type"] == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER_WHATSAPP"
    assert fields["promoted_object"] == {"page_id": "PAGE1", "smart_pse_enabled": False}


def test_the_asset_feed_offers_each_inbox_with_the_client_s_button():
    spec = M.asset_feed_spec("BOOK_NOW", instagram_legacy_id="18983274320")
    apps = [a["value"]["app_destination"] for a in spec["call_to_actions"]]
    assert apps == ["MESSENGER", "WHATSAPP", "INSTAGRAM_DIRECT"]
    assert {a["type"] for a in spec["call_to_actions"]} == {"BOOK_NOW"}
    assert spec["optimization_type"] == "DOF_MESSAGING_DESTINATION"
    assert spec["additional_data"] == {"multi_share_end_card": False, "is_click_to_message": True}
    ig = spec["call_to_actions"][2]["value"]
    assert ig["app_link"] == "instagram://direct?userid=18983274320"


def test_instagram_is_left_off_rather_than_guessed():
    """A broken deep link opens nowhere; two working inboxes are still two."""
    spec = M.asset_feed_spec("CONTACT_US")
    assert [a["value"]["app_destination"] for a in spec["call_to_actions"]] == ["MESSENGER", "WHATSAPP"]


def test_creative_enhancements_match_the_manual_ad_exactly():
    """An API-created creative defaults every enhancement to OPT_OUT. The manual ad
    opts into seven and explicitly declines three — omitting a feature is not the same
    as declining it, so both halves are pinned."""
    feats = M.DEGREES_OF_FREEDOM_SPEC["creative_features_spec"]
    opted_in = {k for k, v in feats.items() if v["enroll_status"] == "OPT_IN"}
    opted_out = {k for k, v in feats.items() if v["enroll_status"] == "OPT_OUT"}
    assert opted_in == {"enhance_cta", "inline_comment", "show_destination_blurbs",
                        "standard_enhancements", "video_auto_crop", "video_filtering",
                        "video_uncrop"}
    assert opted_out == {"ads_with_benefits", "advantage_plus_creative", "text_optimizations"}
    assert M.creative_extras()["contextual_multi_ads"] == {"enroll_status": "OPT_IN"}


def test_the_welcome_message_opens_on_an_ice_breaker():
    wm = json.loads(M.welcome_message())
    assert wm["type"] == "VISUAL_EDITOR" and wm["landing_screen_type"] == "welcome_message"
    msg = wm["text_format"]["message"]
    assert wm["text_format"]["customer_action_type"] == "ice_breakers"
    assert msg["ice_breakers"] and msg["ice_breakers"][0]["title"]
    assert "{{user_first_name}}" in msg["text"]


def test_the_primary_button_routes_to_messenger_as_ads_manager_sets_it():
    cta = M.primary_call_to_action("SHOP_NOW")
    assert cta == {"type": "SHOP_NOW",
                   "value": {"app_destination": "MESSENGER", "link": "https://fb.com/messenger_doc/"}}


def test_the_legacy_instagram_id_is_read_from_the_page():
    assert M.instagram_legacy_id({"instagram_business_account": {"id": "1784", "ig_id": 189}}) == "189"
    assert M.instagram_legacy_id({}) == ""


def test_cities_get_the_manual_campaign_s_25_mile_radius():
    """A city key alone is Meta's administrative boundary — far tighter than where a
    city's people actually live and shop."""
    out = _with_city_radius({"geo_locations": {"cities": [{"key": "1"}],
                                               "neighborhoods": [{"key": "2"}]}})
    assert out["geo_locations"]["cities"] == [{"key": "1", "radius": 25, "distance_unit": "mile"}]
    assert out["geo_locations"]["neighborhoods"] == [{"key": "2"}]   # Meta takes no radius here


def test_a_radius_the_plan_already_set_is_kept():
    out = _with_city_radius({"geo_locations": {"cities": [{"key": "1", "radius": 10,
                                                           "distance_unit": "kilometer"}]}})
    assert out["geo_locations"]["cities"][0]["radius"] == 10


def test_the_client_s_age_becomes_the_advantage_audience_suggestion():
    """With Advantage+ audience Meta rejects a hard max age below 65 (subcode 1870189)
    and caps a hard minimum at 25 — the manual campaign shows the accepted shape."""
    from app.agents.jane_ads.adapters.meta import _advantage_age

    out = _advantage_age({"age_min": 23, "age_max": 55})
    assert out["age_range"] == [23, 55]          # the client's choice, intact
    assert out["age_max"] == 65                   # the only hard max Meta accepts
    assert out["age_min"] == 23

    assert _advantage_age({"age_min": 30, "age_max": 50})["age_min"] == 25
    assert _advantage_age({"age_min": 25, "age_max": 65}) == {
        "age_min": 25, "age_max": 65, "age_range": [25, 65]}   # exactly the manual ad set
    assert _advantage_age({"geo_locations": {}}) == {"geo_locations": {}}


def test_editing_another_field_keeps_the_client_s_age_range():
    """A live Advantage+ ad set reads back with the range in age_range. Re-deriving it
    from the hard fences would turn 23-55 into 23-65 on every unrelated edit."""
    from app.agents.jane_ads.adapters.meta import _advantage_age

    live = {"age_min": 23, "age_max": 65, "age_range": [23, 55]}
    assert _advantage_age(live)["age_range"] == [23, 55]
    assert _advantage_age({"age_min": 25, "age_max": 65, "age_range": [30, 50]})["age_range"] == [30, 50]
