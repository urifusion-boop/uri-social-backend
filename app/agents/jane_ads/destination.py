"""
Jane + Ads — where an ad's tap actually goes.

Every Jane ad used to route to one place: wa.me/<the brand's number>, hard-coded in
each adapter. That's right for the brands that sell in DMs, but wrong for a brand
whose buying happens on their site (or whose only "inbox" is Instagram, or whose
whole checkout is one Paystack link). The ad mechanism never needed a phone number —
all three adapters (meta/google/tiktok) do the same thing with the result: put ONE
https link on the ad. So the destination is a per-brand choice here, and the adapters
ask for a link instead of building one.

Four types, but really two kinds. WHATSAPP/INSTAGRAM_DM/WEBSITE are guided: the user
gives a number, a handle or a domain in whatever shape they type it, and this module
turns it into a working link. CUSTOM is the escape hatch — the user pastes the exact
URL they want and picks the button wording, which is what a brand selling through a
Paystack/Selar/Linktree/Google-Form link needs and no preset can anticipate.

Instagram uses ig.me/m/<username>, the DM equivalent of wa.me: a plain link that
opens a DM with that account, so it needs no native Meta messaging routing (which
requires per-brand Page/IG linking — the exact thing whatsapp.py exists to avoid).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import NamedTuple, Optional
from urllib.parse import quote

from .whatsapp import SETTINGS_COLLECTION, normalize_wa_number

# Opens WhatsApp on an EMPTY chat otherwise — live-confirmed to cost nearly every
# lead (186 link clicks, zero messages). Pre-filling removes the "what do I say".
WA_PREFILL = "Hi! I saw your ad and I'm interested — tell me more?"


class DestinationType(str, Enum):
    WHATSAPP = "whatsapp"
    WEBSITE = "website"
    INSTAGRAM_DM = "instagram_dm"
    CUSTOM = "custom"            # the user pastes the exact URL — see module docstring


DEFAULT_DESTINATION = DestinationType.WHATSAPP

# The button wordings a user can pick from. Keys are ours (stable, storable); the
# values pair the label the ad shows with the Meta call_to_action type that renders
# it. Deliberately a CLOSED set: Meta rejects an unknown CTA type outright, so a
# free-text button would fail at launch instead of at save time — the user gets to
# choose the wording, not to invent it.
CTA_CHOICES: dict[str, tuple[str, str]] = {
    "learn_more": ("Learn More", "LEARN_MORE"),
    "shop_now": ("Shop Now", "SHOP_NOW"),
    "order_now": ("Order Now", "ORDER_NOW"),
    "book_now": ("Book Now", "BOOK_NOW"),
    "sign_up": ("Sign Up", "SIGN_UP"),
    "get_offer": ("Get Offer", "GET_OFFER"),
    "contact_us": ("Contact Us", "CONTACT_US"),
    "send_message": ("Send Message", "LEARN_MORE"),
}

# Used when the user hasn't picked a button. WHATSAPP needs an entry here now that it
# no longer uses Meta's native WHATSAPP_MESSAGE button (see meta_cta) — without one it
# fell through to "Learn More", which reads wrong on an ad whose whole purpose is to
# start a chat. CONTACT_US is the closest Meta type that still carries a plain link.
_DEFAULT_CTA_CHOICE = {
    DestinationType.WHATSAPP: "contact_us",
    DestinationType.WEBSITE: "learn_more",
    DestinationType.INSTAGRAM_DM: "send_message",
    DestinationType.CUSTOM: "learn_more",
}

_CTA_LABELS = {
    DestinationType.WHATSAPP: "Send WhatsApp Message",
    DestinationType.WEBSITE: "Visit Website",
    DestinationType.INSTAGRAM_DM: "Message on Instagram",
    DestinationType.CUSTOM: "Learn More",
}

# What the CREATIVE IMAGE is allowed to say. The image must never contradict the
# button (live-observed: a click-to-WhatsApp ad whose image read "Visit our website").
_IMAGE_CTAS = {
    DestinationType.WHATSAPP: "Message us on WhatsApp",
    DestinationType.WEBSITE: "Visit our website",
    DestinationType.INSTAGRAM_DM: "DM us on Instagram",
    DestinationType.CUSTOM: "Tap to find out more",
}

# The ask the ad COPY closes on, and the example of a good one. Same contract as
# _IMAGE_CTAS, one layer down: a written "message me to order" on an ad whose button
# opens a website is the same broken promise as an image that says the wrong thing.
# Phrased as an instruction to the copywriter, not as copy to paste.
_COPY_ACTIONS = {
    DestinationType.WHATSAPP: ("message on WhatsApp", "Message me to order"),
    DestinationType.WEBSITE: ("tap the button to open the website", "Tap to shop"),
    DestinationType.INSTAGRAM_DM: ("send a DM on Instagram", "DM me to order"),
    DestinationType.CUSTOM: ("tap the button", "Tap to get started"),
}

_IG_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# The picker Jane shows in the conversation ("where should people who tap end up?").
# The copy lives here, next to the validation that accepts or rejects each answer, so
# the prompt and the rule can't drift apart. `field` is the key set_brand_destination
# expects for that type — the UI needs only ONE input box, whatever the choice.
DESTINATION_OPTIONS: list[dict] = [
    {
        "value": DestinationType.WHATSAPP.value,
        "label": "My WhatsApp",
        "hint": "They tap and land in a WhatsApp chat with you, message already typed.",
        "field": "whatsapp_number",
        "input_label": "Your WhatsApp number",
        "placeholder": "0803 123 4567",
        "takes_cta": False,      # Meta renders its own WhatsApp button
    },
    {
        "value": DestinationType.WEBSITE.value,
        "label": "My website",
        "hint": "They tap and open your site.",
        "field": "website_url",
        "input_label": "Your website",
        "placeholder": "yourshop.com",
        "takes_cta": True,
    },
    {
        "value": DestinationType.INSTAGRAM_DM.value,
        "label": "My Instagram DMs",
        "hint": "They tap and land in your Instagram inbox.",
        "field": "instagram_username",
        "input_label": "Your Instagram handle",
        "placeholder": "@yourbrand",
        "takes_cta": True,
    },
    {
        "value": DestinationType.CUSTOM.value,
        "label": "A link I'll paste",
        "hint": "Anywhere else — a payment link, a form, a Linktree.",
        "field": "custom_url",
        "input_label": "Paste your link",
        "placeholder": "https://paystack.com/pay/your-store",
        "takes_cta": True,
    },
]

# Which stored field one type's single answer belongs in.
VALUE_FIELD_FOR_TYPE = {o["value"]: o["field"] for o in DESTINATION_OPTIONS}


def cta_choice_list() -> list[dict]:
    """The button options, in the shape a picker renders."""
    return [{"value": k, "label": v[0]} for k, v in CTA_CHOICES.items()]


def current_value(destination_type: DestinationType, saved: dict) -> str:
    """What this brand already has on file for this destination type — so the picker
    pre-fills instead of asking a returning brand to retype their own number."""
    return saved.get(VALUE_FIELD_FOR_TYPE.get(destination_type.value, ""), "") or ""


def coerce_cta(raw: str, destination_type: DestinationType) -> str:
    """A stored/typed CTA key, or this destination's default for anything
    unrecognised. Never returns a key Meta would reject."""
    key = str(raw or "").strip().lower()
    if key in CTA_CHOICES:
        return key
    return _DEFAULT_CTA_CHOICE.get(destination_type, "learn_more")


def coerce_type(raw: str) -> DestinationType:
    """A stored/typed destination type, or the WhatsApp default for anything
    unrecognised — a bad value must never silently produce a dead-end ad."""
    try:
        return DestinationType(str(raw or "").strip().lower())
    except ValueError:
        return DEFAULT_DESTINATION


def normalize_website_url(raw: str) -> Optional[str]:
    """A user-typed site ('uri.com', 'www.uri.com/shop', 'https://uri.com') as a real
    absolute https URL. Returns None if it can't be one — every ad platform rejects a
    bare hostname as a final/landing URL."""
    url = (raw or "").strip()
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        scheme, _, rest = url.partition("://")
    else:
        scheme, rest = "https", url
    rest = rest.lstrip("/")
    host = rest.split("/", 1)[0].split("?", 1)[0]
    # A real hostname: at least one dot, a 2+ char TLD, no spaces.
    if " " in host or not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", host):
        return None
    return f"{scheme}://{rest}"


def normalize_instagram_username(raw: str) -> Optional[str]:
    """A handle from any of the shapes users paste — '@uri', 'uri',
    'instagram.com/uri', 'https://www.instagram.com/uri/?hl=en' — as the bare
    username ig.me expects. None if it isn't a possible handle."""
    value = (raw or "").strip()
    if not value:
        return None
    value = re.sub(r"^https?://", "", value, flags=re.I)
    value = re.sub(r"^(www\.)?instagram\.com/", "", value, flags=re.I)
    value = value.split("?", 1)[0].split("/", 1)[0].lstrip("@").strip()
    return value if _IG_USERNAME_RE.match(value) else None


def build_link(
    destination_type: DestinationType,
    *,
    whatsapp_number: str = "",
    website_url: str = "",
    instagram_username: str = "",
    custom_url: str = "",
) -> str:
    """The single https link the ad's tap opens, or '' when the destination isn't
    configured yet (callers treat that as 'not launchable', same as a missing number
    always has been)."""
    if destination_type == DestinationType.CUSTOM:
        # Whatever the user pasted, exactly as they meant it — query string, path and
        # all (a Paystack/Selar checkout, a Google Form, a Linktree). Still normalized,
        # because a pasted link is just as likely to be missing its scheme.
        return normalize_website_url(custom_url) or ""
    if destination_type == DestinationType.WEBSITE:
        return normalize_website_url(website_url) or ""
    if destination_type == DestinationType.INSTAGRAM_DM:
        handle = normalize_instagram_username(instagram_username)
        # ig.me/m/<user> is Instagram's own DM deep link — opens the app on a chat
        # with that account, falls back to the web profile when the app isn't there.
        return f"https://ig.me/m/{handle}" if handle else ""
    number = normalize_wa_number(whatsapp_number) if whatsapp_number else ""
    return f"https://wa.me/{number}?text={quote(WA_PREFILL)}" if number else ""


def cta_label(destination_type: DestinationType, cta_choice: str = "") -> str:
    """The button wording shown on the ad (and in previews/plans). WhatsApp keeps its
    own native button; every other destination uses the wording the user picked, or
    that destination's sensible default."""
    if destination_type == DestinationType.WHATSAPP:
        return _CTA_LABELS[DestinationType.WHATSAPP]
    if cta_choice:
        return CTA_CHOICES[coerce_cta(cta_choice, destination_type)][0]
    return _CTA_LABELS.get(destination_type, _CTA_LABELS[DEFAULT_DESTINATION])


def image_cta(destination_type: DestinationType) -> str:
    """What the generated image is allowed to say, so it matches the real button."""
    return _IMAGE_CTAS.get(destination_type, _IMAGE_CTAS[DEFAULT_DESTINATION])


def copy_action(destination_type: DestinationType) -> str:
    """The action the written copy tells the reader to take."""
    return _COPY_ACTIONS.get(destination_type, _COPY_ACTIONS[DEFAULT_DESTINATION])[0]


def copy_action_example(destination_type: DestinationType) -> str:
    """A model closing ask for this destination, for the register block's example."""
    return _COPY_ACTIONS.get(destination_type, _COPY_ACTIONS[DEFAULT_DESTINATION])[1]


def meta_cta(destination_type: DestinationType, link: str, is_video: bool,
             cta_choice: str = "") -> dict:
    """Meta's call_to_action object for this destination.

    Every destination, WhatsApp included, is a plain link ad carrying the CTA type
    behind the button the user chose. The native INSTAGRAM_MESSAGE/SEND_MESSAGE types
    route through Meta's own messaging setup, which is the per-brand linking this
    system avoids.

    WHATSAPP_MESSAGE used to be used for WhatsApp image ads. It is Meta's native
    WhatsApp button and it requires the Page to have a WhatsApp number connected —
    the very linking the wa.me approach exists to avoid. Meta accepts the creative at
    creation and then blocks it at review:

        WhatsApp number required: Reconnect your WhatsApp number to your Facebook
        Page or Instagram account to run this ad. (#2446880)

    Live-confirmed 2026-08-31 on a launched ad whose link was already a valid
    wa.me/<number>. Neither connected Page had a WhatsApp number, so every
    click-to-WhatsApp ad was affected, not just that one. A plain-link ad that
    delivers beats a native button that does not — and the wa.me link is the
    destination either way, so the tap lands in the same chat.
    """
    cta_type = CTA_CHOICES[coerce_cta(cta_choice, destination_type)][1]
    return {"type": cta_type, "value": {"link": link}} if link else {"type": cta_type}


async def get_brand_destination(db, brand_id: Optional[str]) -> dict:
    """The brand's saved destination — every field, so a brand that switches type
    keeps the values it already gave for the others. Defaults to WhatsApp: every
    existing brand predates this setting and already has a number stored, so they
    keep working untouched."""
    doc = {}
    if brand_id and db is not None:
        doc = await db[SETTINGS_COLLECTION].find_one(
            {"brand_id": brand_id},
            {"_id": 0, "destination_type": 1, "whatsapp_number": 1, "website_url": 1,
             "instagram_username": 1, "custom_url": 1, "destination_cta": 1},
        ) or {}
    dest = coerce_type(doc.get("destination_type", ""))
    return {
        "destination_type": dest.value,
        "whatsapp_number": doc.get("whatsapp_number", "") or "",
        "website_url": doc.get("website_url", "") or "",
        "instagram_username": doc.get("instagram_username", "") or "",
        "custom_url": doc.get("custom_url", "") or "",
        "destination_cta": coerce_cta(doc.get("destination_cta", ""), dest),
    }


async def set_brand_destination(
    db,
    brand_id: str,
    destination_type: DestinationType,
    *,
    website_url: str = "",
    instagram_username: str = "",
    custom_url: str = "",
    destination_cta: str = "",
) -> dict:
    """Save where this brand's ads route, and what the button says. Raises ValueError
    when the value for the chosen type isn't usable, so a brand can never save a
    destination that would produce an ad linking nowhere. The WhatsApp number keeps
    its own endpoint (/whatsapp) — switching TO whatsapp here just selects the stored
    number, and ignores destination_cta (that button is Meta's native one)."""
    update: dict = {"destination_type": destination_type.value,
                    "updated_at": datetime.now(timezone.utc)}
    if destination_type == DestinationType.CUSTOM:
        url = normalize_website_url(custom_url)
        if not url:
            raise ValueError("That link doesn't look right — paste the full address, e.g. https://paystack.com/pay/your-store.")
        update["custom_url"] = url
    elif destination_type == DestinationType.WEBSITE:
        url = normalize_website_url(website_url)
        if not url:
            raise ValueError("That website address doesn't look right — please include the full address, e.g. yourshop.com.")
        update["website_url"] = url
    elif destination_type == DestinationType.INSTAGRAM_DM:
        handle = normalize_instagram_username(instagram_username)
        if not handle:
            raise ValueError("That Instagram handle doesn't look right — please give just the username, e.g. @yourbrand.")
        update["instagram_username"] = handle
    if destination_type != DestinationType.WHATSAPP:
        if destination_cta and coerce_cta(destination_cta, destination_type) != destination_cta.strip().lower():
            raise ValueError(
                f"'{destination_cta}' isn't a button Meta accepts — choose one of: "
                + ", ".join(sorted(CTA_CHOICES))
            )
        update["destination_cta"] = coerce_cta(destination_cta, destination_type)
    await db[SETTINGS_COLLECTION].update_one(
        {"brand_id": brand_id}, {"$set": update}, upsert=True
    )
    return await get_brand_destination(db, brand_id)


class ResolvedDestination(NamedTuple):
    """What an adapter needs to put an ad's tap somewhere: the type, the link, and
    the button the user chose."""
    type: DestinationType
    link: str
    cta: str

    def meta_call_to_action(self, is_video: bool) -> dict:
        return meta_cta(self.type, self.link, is_video, self.cta)


def link_for_plan(plan) -> ResolvedDestination:
    """The destination a CampaignPlan actually launches to.

    Takes `plan.destination_link` as authoritative — it was resolved and frozen when
    the plan was built, so a brand switching destination mid-flight can't retarget an
    already-approved plan. Falls back to rebuilding from `whatsapp_number` for plans
    persisted before destination_type existed, which are all WhatsApp by definition.
    """
    dest = coerce_type(getattr(plan, "destination_type", "") or DEFAULT_DESTINATION.value)
    link = getattr(plan, "destination_link", "") or ""
    if not link and dest == DestinationType.WHATSAPP:
        link = build_link(dest, whatsapp_number=getattr(plan, "whatsapp_number", "") or "")
    return ResolvedDestination(dest, link, coerce_cta(getattr(plan, "destination_cta", ""), dest))
