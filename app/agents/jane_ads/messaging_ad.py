"""
Jane + Ads — the messaging ad, built the way Ads Manager builds it.

Jane's campaigns cost more per click and brought worse conversations than the one
campaign on the account set up by hand in Ads Manager. Diffing the two field by field
(campaign, ad set, ad) put every difference below the campaign level, and every one
was a setting Ads Manager applies and the API leaves off unless asked:

· **One ad, three inboxes.** destination_type MESSAGING_INSTAGRAM_DIRECT_MESSENGER_
  WHATSAPP, encoded on the creative as an asset_feed_spec with one call-to-action per
  app and optimization_type DOF_MESSAGING_DESTINATION. Meta opens whichever app each
  person actually uses, instead of sending everybody to WhatsApp.
· **A welcome message with an ice-breaker**, so the chat opens on a question about the
  offer rather than an empty box.
· **Meta's standard creative enhancements**, opted IN exactly as the manual ad has
  them. An API-created creative defaults every one of them to OPT_OUT.
· **Multi-advertiser ads** opted in.

Nothing here decides WHO sees the ad — age, interests and areas stay the client's.
The values are copied from the manual campaign rather than invented, so the exact set
lives in one place and can be re-diffed against it.
"""
from __future__ import annotations

import json
from typing import Optional

MULTI_DESTINATION = "MESSAGING_INSTAGRAM_DIRECT_MESSENGER_WHATSAPP"

# Ads Manager's own placeholder links for each app. The real routing is the ad set's
# destination_type plus the Page's connected inboxes; these only have to be the URLs
# Meta expects for each app_destination.
_MESSENGER_LINK = "https://fb.com/messenger_doc/"
_WHATSAPP_LINK = "https://api.whatsapp.com/send"
_INSTAGRAM_LINK = "https://www.instagram.com/"

# Exactly the manual ad's set — opt-ins AND the explicit opt-outs, since an omitted
# feature is not the same as one the manual ad declined.
#
# Minus "standard_enhancements". Ads Manager still SHOWS that bundle on the manual ad,
# but the API refuses it outright ("Including standard enhancements field in creative
# has been deprecated. Please choose to set individual features instead", subcode
# 3858504 — caught on the first real launch). The individual features Ads Manager
# expands it into are the ones listed here.
DEGREES_OF_FREEDOM_SPEC = {
    "creative_features_spec": {
        "ads_with_benefits": {"enroll_status": "OPT_OUT"},
        "advantage_plus_creative": {"enroll_status": "OPT_OUT"},
        "enhance_cta": {"enroll_status": "OPT_IN"},
        "inline_comment": {"enroll_status": "OPT_IN"},
        "show_destination_blurbs": {"enroll_status": "OPT_IN"},
        "text_optimizations": {"enroll_status": "OPT_OUT"},
        "video_auto_crop": {"enroll_status": "OPT_IN"},
        "video_filtering": {"enroll_status": "OPT_IN"},
        "video_uncrop": {"enroll_status": "OPT_IN"},
    }
}

CONTEXTUAL_MULTI_ADS = {"enroll_status": "OPT_IN"}


def primary_call_to_action(cta_type: str) -> dict:
    """The button on the story itself — Messenger, as Ads Manager sets it."""
    return {"type": cta_type,
            "value": {"app_destination": "MESSENGER", "link": _MESSENGER_LINK}}


def asset_feed_spec(cta_type: str, instagram_legacy_id: str = "") -> dict:
    """One call-to-action per inbox, with Meta choosing which to show each person.

    Instagram needs the account's LEGACY id in its app link — a different number from
    the 1784… business id stored elsewhere. When it can't be resolved the Instagram leg
    is left off rather than sent with a guess: a broken deep link opens nowhere, while
    two working inboxes are still two.
    """
    actions = [
        {"type": cta_type, "value": {"app_destination": "MESSENGER", "link": _MESSENGER_LINK}},
        {"type": cta_type, "value": {"app_destination": "WHATSAPP", "link": _WHATSAPP_LINK}},
    ]
    if instagram_legacy_id:
        actions.append({"type": cta_type, "value": {
            "app_destination": "INSTAGRAM_DIRECT",
            "app_link": f"instagram://direct?userid={instagram_legacy_id}",
            "link": _INSTAGRAM_LINK,
        }})
    return {
        "call_to_actions": actions,
        "optimization_type": "DOF_MESSAGING_DESTINATION",
        "additional_data": {"multi_share_end_card": False, "is_click_to_message": True},
    }


def welcome_message(business_name: str = "", ice_breaker: str = "") -> str:
    """The greeting a person sees when the chat opens, in Ads Manager's own format.

    Meta takes it as a JSON STRING, not an object. The ice-breaker is the one tap that
    turns "I opened the chat" into a question about the offer — the manual ad's is "I'm
    interested. How can I book?"; this is the general form of the same thing.
    """
    who = f" Thank you for your interest in {business_name}." if business_name else \
        " Thank you for your interest."
    text = "Hi{{user_first_name}}! \U0001F44B" + who + " How can we help you today?"
    return json.dumps({
        "type": "VISUAL_EDITOR",
        "version": 2,
        "landing_screen_type": "welcome_message",
        "media_type": "text",
        "text_format": {
            "customer_action_type": "ice_breakers",
            "message": {
                "ice_breakers": [{"title": ice_breaker or "I'm interested. How do I get started?",
                                  "response": ""}],
                "quick_replies": [],
                "text": text,
            },
        },
    })


def adset_messaging_fields(page_id: str) -> dict:
    """The ad set side of the same ad: one destination covering all three inboxes."""
    return {
        "destination_type": MULTI_DESTINATION,
        "promoted_object": {"page_id": page_id, "smart_pse_enabled": False},
    }


def creative_extras() -> dict:
    """The creative-level settings the manual ad has and an API creative defaults off."""
    return {
        "degrees_of_freedom_spec": DEGREES_OF_FREEDOM_SPEC,
        "contextual_multi_ads": CONTEXTUAL_MULTI_ADS,
    }


def instagram_legacy_id(page_payload: Optional[dict]) -> str:
    """The legacy Instagram id from a GET /{page}?fields=instagram_business_account{ig_id}."""
    account = (page_payload or {}).get("instagram_business_account") or {}
    return str(account.get("ig_id") or "")
