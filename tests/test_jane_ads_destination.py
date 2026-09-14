"""
Unit tests for the ad destination (destination.py) — the pure part. WhatsApp used to
be the only place an ad could send people; a brand can now point at their website or
their Instagram DMs instead. These are the functions that decide whether the link on
a live ad actually goes anywhere, so they're tested directly (the get/set helpers are
thin Mongo wrappers, covered live).
"""
import pytest

from app.agents.jane_ads.destination import (
    CTA_CHOICES,
    DEFAULT_DESTINATION,
    DestinationType,
    coerce_cta,
    build_link,
    coerce_type,
    cta_label,
    image_cta,
    link_for_plan,
    meta_cta,
    normalize_instagram_username,
    normalize_website_url,
)


# ── Website ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("uri.com", "https://uri.com"),
    ("  uri.com  ", "https://uri.com"),
    ("www.urishop.com/sale", "https://www.urishop.com/sale"),
    ("https://uri.com/a?b=1", "https://uri.com/a?b=1"),
    ("http://uri.ng", "http://uri.ng"),          # their own scheme is respected
    ("//uri.com", "https://uri.com"),
])
def test_website_becomes_an_absolute_url(raw, expected):
    assert normalize_website_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "uri", "not a url", "https://", "localhost"])
def test_unusable_website_is_rejected(raw):
    # Every ad platform rejects a bare hostname as a final/landing URL — better to
    # refuse it at save time than to ship an ad that links nowhere.
    assert normalize_website_url(raw) is None


# ── Instagram ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("uri", "uri"),
    ("@uri", "uri"),
    ("instagram.com/uri_brand/", "uri_brand"),
    ("https://www.instagram.com/uri.brand/?hl=en", "uri.brand"),
    ("  @Uri_Brand  ", "Uri_Brand"),
])
def test_instagram_handle_from_every_shape_users_paste(raw, expected):
    assert normalize_instagram_username(raw) == expected


@pytest.mark.parametrize("raw", ["", "bad handle", "uri!", "a" * 31])
def test_unusable_instagram_handle_is_rejected(raw):
    assert normalize_instagram_username(raw) is None


# ── The link the ad actually carries ──────────────────────────────────────────

def test_whatsapp_link_is_prefilled():
    # A bare wa.me link opens an EMPTY chat with a number the person has never
    # messaged — live-confirmed at 186 link clicks and zero messages.
    link = build_link(DestinationType.WHATSAPP, whatsapp_number="0803 123 4567")
    assert link.startswith("https://wa.me/2348031234567?text=")


def test_website_link_is_the_normalized_url():
    assert build_link(DestinationType.WEBSITE, website_url="urishop.com/sale") == "https://urishop.com/sale"


def test_instagram_link_is_the_dm_deep_link():
    assert build_link(DestinationType.INSTAGRAM_DM, instagram_username="@urishop") == "https://ig.me/m/urishop"


@pytest.mark.parametrize("dest,kwargs", [
    (DestinationType.WHATSAPP, {"whatsapp_number": ""}),
    (DestinationType.WEBSITE, {"website_url": "nope"}),
    (DestinationType.INSTAGRAM_DM, {"instagram_username": "bad handle"}),
])
def test_unconfigured_destination_yields_no_link(dest, kwargs):
    # '' is the signal every adapter refuses to launch on, rather than shipping a
    # dead-end ad.
    assert build_link(dest, **kwargs) == ""


# ── Type coercion + labels ────────────────────────────────────────────────────

def test_unknown_type_falls_back_to_whatsapp():
    assert coerce_type("garbage") is DEFAULT_DESTINATION
    assert coerce_type("") is DestinationType.WHATSAPP
    assert coerce_type("WEBSITE") is DestinationType.WEBSITE


def test_each_destination_has_its_own_image_wording():
    # The IMAGE wording is ours to write, so it stays distinct per destination.
    assert len({image_cta(d) for d in DestinationType}) == len(DestinationType)


def test_button_labels_are_only_ever_what_meta_renders():
    # Superseded 2026-08-31: this used to require a DISTINCT label per destination,
    # which is what let the WhatsApp label drift. Meta renders one of a fixed set of
    # buttons, and two destinations can legitimately land on the same one — the label
    # must match the ad, not be unique. Live-confirmed on ad 52561318289410: the
    # preview said "Send WhatsApp Message", the ad said "Contact Us".
    pairs = set(CTA_CHOICES.values())
    for d in DestinationType:
        meta_type = meta_cta(d, "https://x.com", False)["type"]
        assert (cta_label(d), meta_type) in pairs


# ── Meta's call_to_action ─────────────────────────────────────────────────────

def test_whatsapp_photo_ad_carries_the_wa_link_on_a_plain_cta():
    # Superseded 2026-08-31. This asserted Meta's native WHATSAPP_MESSAGE button, which
    # requires the Page to have a WhatsApp number connected — the per-brand linking the
    # wa.me approach exists to avoid. Meta accepts the creative and then blocks it at
    # review: "WhatsApp number required ... (#2446880)". Photo and video now behave the
    # same: a plain link ad whose CTA carries the wa.me destination.
    assert meta_cta(DestinationType.WHATSAPP, "https://wa.me/234", False) == {
        "type": "CONTACT_US", "value": {"link": "https://wa.me/234"}
    }


def test_whatsapp_video_ad_carries_the_wa_link_on_the_cta():
    # video_data has no link field of its own, so the destination rides on the CTA.
    # Photo does the same now, so both paths agree.
    assert meta_cta(DestinationType.WHATSAPP, "https://wa.me/234", True) == {
        "type": "CONTACT_US", "value": {"link": "https://wa.me/234"}
    }


@pytest.mark.parametrize("dest", [DestinationType.WEBSITE, DestinationType.INSTAGRAM_DM])
@pytest.mark.parametrize("is_video", [False, True])
def test_non_whatsapp_destinations_are_plain_link_ctas(dest, is_video):
    # INSTAGRAM_DM defaults to CONTACT_US, not LEARN_MORE: its old default key
    # "send_message" was labelled "Send Message" but mapped to LEARN_MORE, so the ad's
    # button read "Learn More". Meta has no generic send-message CTA on a link ad.
    expected = "CONTACT_US" if dest is DestinationType.INSTAGRAM_DM else "LEARN_MORE"
    assert meta_cta(dest, "https://x.com", is_video) == {
        "type": expected, "value": {"link": "https://x.com"}
    }


# ── Plan resolution ───────────────────────────────────────────────────────────

class _Plan:
    def __init__(self, **kw):
        self.whatsapp_number = kw.get("whatsapp_number", "")
        self.destination_type = kw.get("destination_type", "whatsapp")
        self.destination_link = kw.get("destination_link", "")
        self.destination_cta = kw.get("destination_cta", "")


def test_frozen_link_wins_so_an_approved_plan_cant_be_retargeted():
    plan = _Plan(destination_type="website", destination_link="https://urishop.com",
                 whatsapp_number="2348031234567")
    resolved = link_for_plan(plan)
    assert resolved.type is DestinationType.WEBSITE
    assert resolved.link == "https://urishop.com"


def test_legacy_plan_with_only_a_number_still_resolves():
    # Plans persisted before destination_type existed are all WhatsApp by definition.
    resolved = link_for_plan(_Plan(whatsapp_number="2348031234567"))
    assert resolved.type is DestinationType.WHATSAPP
    assert resolved.link.startswith("https://wa.me/2348031234567?text=")


def test_plan_carries_the_users_button_into_metas_payload():
    plan = _Plan(destination_type="custom", destination_link="https://paystack.com/pay/uri",
                 destination_cta="order_now")
    assert link_for_plan(plan).meta_call_to_action(is_video=False) == {
        "type": "ORDER_NOW", "value": {"link": "https://paystack.com/pay/uri"}
    }


# ── The custom link — the user decides ────────────────────────────────────────

@pytest.mark.parametrize("pasted,expected", [
    ("https://paystack.com/pay/uri-store", "https://paystack.com/pay/uri-store"),
    ("selar.co/m/uri?ref=ad", "https://selar.co/m/uri?ref=ad"),
    ("https://linktr.ee/urishop", "https://linktr.ee/urishop"),
    ("https://forms.gle/abc123", "https://forms.gle/abc123"),
])
def test_custom_link_is_kept_exactly_as_the_user_meant_it(pasted, expected):
    # Path and query survive intact — a checkout link is worthless without them.
    assert build_link(DestinationType.CUSTOM, custom_url=pasted) == expected


def test_custom_link_that_isnt_a_link_yields_nothing():
    assert build_link(DestinationType.CUSTOM, custom_url="my shop") == ""


# ── The button the user picks ─────────────────────────────────────────────────

def test_chosen_button_drives_both_the_label_and_metas_cta_type():
    assert cta_label(DestinationType.CUSTOM, "shop_now") == "Shop Now"
    assert meta_cta(DestinationType.CUSTOM, "https://x.com", False, "shop_now")["type"] == "SHOP_NOW"


def test_whatsapp_label_tracks_the_button_the_ad_will_actually_show():
    # Was: the label stayed "Send WhatsApp Message" whatever the choice. That was a
    # leftover from Meta's native WhatsApp button and became a lie once the ad started
    # shipping a plain link CTA — the preview promised one button, the ad showed
    # another. Now the label is read off the same row as the Meta type.
    assert cta_label(DestinationType.WHATSAPP, "shop_now") == "Shop Now"
    assert cta_label(DestinationType.WHATSAPP) == "Contact Us"
    # Meta gets a real, link-carrying CTA rather than its native WhatsApp
    # button, which would not deliver without the Page being linked.
    assert meta_cta(DestinationType.WHATSAPP, "https://wa.me/234", False, "shop_now") == {
        "type": "SHOP_NOW", "value": {"link": "https://wa.me/234"}
    }


def test_unknown_button_falls_back_per_destination_never_to_something_meta_rejects():
    assert coerce_cta("make_it_pop", DestinationType.CUSTOM) == "learn_more"
    assert coerce_cta("", DestinationType.INSTAGRAM_DM) == "contact_us"
    # A destination stored before "send_message" was removed coerces to the default
    # rather than raising or reaching a key that no longer exists.
    assert coerce_cta("send_message", DestinationType.INSTAGRAM_DM) == "contact_us"
    assert coerce_cta("SHOP_NOW", DestinationType.WEBSITE) == "shop_now"


def test_every_offered_button_maps_to_a_real_meta_cta_type():
    for key, (label, meta_type) in CTA_CHOICES.items():
        assert label and meta_type.isupper()
        assert meta_cta(DestinationType.CUSTOM, "https://x.com", False, key)["type"] == meta_type


# ── The conversation picker (the choose_destination stage) ────────────────────

def test_every_option_names_a_field_set_brand_destination_actually_accepts():
    import inspect
    from app.agents.jane_ads.destination import (
        DESTINATION_OPTIONS, VALUE_FIELD_FOR_TYPE, set_brand_destination,
    )
    accepted = set(inspect.signature(set_brand_destination).parameters)
    for opt in DESTINATION_OPTIONS:
        # whatsapp_number is the one answer that routes to its own setter (the ads
        # connection reads it), so it's deliberately not a set_brand_destination kwarg.
        if opt["field"] != "whatsapp_number":
            assert opt["field"] in accepted, opt["value"]
        assert VALUE_FIELD_FOR_TYPE[opt["value"]] == opt["field"]


def test_picker_offers_exactly_the_destination_types_that_exist():
    from app.agents.jane_ads.destination import DESTINATION_OPTIONS
    assert {o["value"] for o in DESTINATION_OPTIONS} == {d.value for d in DestinationType}


def test_every_destination_offers_the_button_picker():
    # Was: WhatsApp hid the picker, because Meta rendered its own native button there.
    # It ships a plain link ad now, so its button is as chooseable as any other's and
    # hiding the picker would just make it unchangeable.
    from app.agents.jane_ads.destination import DESTINATION_OPTIONS
    assert all(o["takes_cta"] for o in DESTINATION_OPTIONS)


def test_options_carry_the_copy_a_picker_needs():
    from app.agents.jane_ads.destination import DESTINATION_OPTIONS
    for opt in DESTINATION_OPTIONS:
        assert opt["label"] and opt["hint"] and opt["input_label"] and opt["placeholder"]


def test_cta_choice_list_is_renderable_and_matches_the_accepted_set():
    from app.agents.jane_ads.destination import cta_choice_list
    choices = cta_choice_list()
    assert {c["value"] for c in choices} == set(CTA_CHOICES)
    assert all(c["label"] for c in choices)


def test_current_value_prefills_from_what_the_brand_already_saved():
    from app.agents.jane_ads.destination import current_value
    saved = {"whatsapp_number": "2348031234567", "website_url": "https://uri.com",
             "instagram_username": "urishop", "custom_url": ""}
    assert current_value(DestinationType.WHATSAPP, saved) == "2348031234567"
    assert current_value(DestinationType.WEBSITE, saved) == "https://uri.com"
    assert current_value(DestinationType.CUSTOM, saved) == ""


def test_destination_is_asked_before_the_creative_is_built():
    # Order is load-bearing, not cosmetic: the chosen destination decides the button
    # AND the CTA baked into the generated image, so asking after the creative step
    # would mean regenerating the image (and burning a content credit) to fix it.
    import inspect
    from app.agents.jane_ads import router
    src = inspect.getsource(router._build_campaign_plan)
    assert src.index('"stage": "choose_destination"') < src.index('"stage": "choose_creative_source"')


# ── The written copy follows the destination too ─────────────────────────────
# The button and the generated image already did; the copy PROMPT did not, so a
# website ad whose button read "Shop Now" was still being told to close on
# "message on WhatsApp". Same broken promise as a mismatched image, one layer down.

def test_copy_action_follows_destination():
    from app.agents.jane_ads.destination import copy_action, copy_action_examples

    assert copy_action(DestinationType.WHATSAPP) == "message on WhatsApp"
    assert copy_action(DestinationType.WEBSITE) == "go to the website"
    assert "DM" in copy_action(DestinationType.INSTAGRAM_DM)
    # CUSTOM can't name the place — it's whatever URL the brand pasted — so it
    # points at the button, which is the one thing that is always true.
    assert copy_action(DestinationType.CUSTOM) == "tap the button"

    # Two examples, not one: with a single example the model pasted it verbatim
    # (a football academy closed on "Tap to shop"). They must differ, or the pair
    # is no better than one.
    web_examples = copy_action_examples(DestinationType.WEBSITE)
    assert len(web_examples) == 2 and web_examples[0] != web_examples[1]
    assert not any("WhatsApp" in e for e in web_examples)


def test_register_block_example_matches_destination():
    from app.agents.jane_ads.creative import _register_rules_block

    wa = _register_rules_block(DestinationType.WHATSAPP.value)
    web = _register_rules_block(DestinationType.WEBSITE.value)

    assert "Message me to order" in wa
    # The example is what the model imitates, so it must not say WhatsApp on a
    # website ad. The REGISTER line itself ("texting a customer on WhatsApp")
    # stays either way — that's about tone of voice, not where the tap goes.
    assert "Message me to order" not in web
    assert "Tap to see prices" in web
    # The block must name the destination's action AND demand a closing ask —
    # softening the examples to "your own words" without keeping the ask
    # mandatory made website ads end with no ask at all (1 of 4 samples had one).
    assert "go to the website" in web
    assert "Never end without one." in web
    assert "your own words" in web


# ── A brand that never chose is not a brand that chose WhatsApp ───────────────
# Live-observed on staging: a brand with no destination at all was never asked for
# a link. get_brand_destination defaults destination_type to "whatsapp", and the
# step-1.6 gate read that default as an answer, so the brand was sent to the
# "link your WhatsApp number" wall instead of the choose_destination question.

def _fake_db(doc):
    class _Coll:
        async def find_one(self, *a, **k):
            return doc
    class _DB:
        def __getitem__(self, name):
            return _Coll()
    return _DB()


@pytest.mark.asyncio
async def test_unset_destination_is_marked_unchosen():
    from app.agents.jane_ads.destination import get_brand_destination

    d = await get_brand_destination(_fake_db(None), "brand_1")
    # The default is still WhatsApp, so nothing downstream changes shape...
    assert d["destination_type"] == "whatsapp"
    # ...but callers can tell it apart from a brand that actually said WhatsApp.
    assert d["chosen"] is False


@pytest.mark.asyncio
async def test_a_real_choice_is_marked_chosen():
    from app.agents.jane_ads.destination import get_brand_destination

    d = await get_brand_destination(
        _fake_db({"destination_type": "whatsapp", "whatsapp_number": "2348000000000"}),
        "brand_1",
    )
    assert (d["destination_type"], d["chosen"]) == ("whatsapp", True)

    web = await get_brand_destination(
        _fake_db({"destination_type": "website", "website_url": "https://x.com"}), "brand_1")
    assert (web["destination_type"], web["chosen"]) == ("website", True)


@pytest.mark.asyncio
async def test_legacy_brand_with_only_a_number_still_reads_as_whatsapp():
    # A brand from before this setting existed has a number but no destination_type.
    # It must keep working untouched: chosen is False, but the number is what the
    # step-1.6 gate falls back to, so nothing starts re-asking these brands.
    from app.agents.jane_ads.destination import get_brand_destination

    d = await get_brand_destination(_fake_db({"whatsapp_number": "2348000000000"}), "brand_1")
    assert d["chosen"] is False
    assert d["whatsapp_number"] == "2348000000000"
    assert d["destination_type"] == "whatsapp"
