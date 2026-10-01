"""
Jane + Ads — the campaign objective the CLIENT chooses.

Meta's campaign objective decides what its delivery system optimises for, and it is
the single most consequential setting on a campaign. Jane used to pick it herself from
the goal she inferred — every campaign came out OUTCOME_ENGAGEMENT or OUTCOME_TRAFFIC —
so a client who wanted reach got an ad optimised for conversations, and Ads Manager
showed them "Objective: Engagement" for a campaign they thought was about sales.

These are Meta's own objectives, offered in Meta's own words, so what the client picks
here is what Ads Manager will show them later.

Two honesty rules encoded below, both of which cost features we could otherwise claim:

· **Only what launches is offered.** App promotion needs an app Uri does not have, and
  a Followers campaign cannot be created through this path at all: Meta rejects the ad
  with "Ad set with promoted object is required", and adding that object makes it
  reject the AD SET with "Performance goal isn't available" — a contradiction, probed
  both ways against the live account. Offering a choice that always fails to launch is
  worse than not offering it.

  FOLLOWERS stays in the enum because plans and records already carry it; it is simply
  not in CHOICES.

· **SALES and LEADS are sent to Meta as themselves.** Meta runs OUTCOME_SALES and
  OUTCOME_LEADS with its own optimisation, which is the point of picking them — the
  client's choice IS the goal, and second-guessing it in the UI just made them doubt a
  setting that works.

Every pairing below was validated against the live ad account by creating a real
campaign per objective and validating an ad set under it (2026-09-24).
"""
from __future__ import annotations

from typing import Optional

from .models import CampaignObjective


# What each objective becomes on Meta, and what it optimises toward. The optimisation
# depends on the destination: a Click-to-WhatsApp ad optimises for CONVERSATIONS, a
# link ad for LINK_CLICKS. CONVERSATIONS is not available under OUTCOME_AWARENESS.
_META = {
    CampaignObjective.AWARENESS:   {"objective": "OUTCOME_AWARENESS",
                                    "link": "REACH", "whatsapp": "REACH"},
    CampaignObjective.TRAFFIC:     {"objective": "OUTCOME_TRAFFIC",
                                    "link": "LINK_CLICKS", "whatsapp": "LINK_CLICKS"},
    CampaignObjective.ENGAGEMENT:  {"objective": "OUTCOME_ENGAGEMENT",
                                    "link": "POST_ENGAGEMENT", "whatsapp": "CONVERSATIONS"},
    CampaignObjective.LEADS:       {"objective": "OUTCOME_LEADS",
                                    "link": "LINK_CLICKS", "whatsapp": "CONVERSATIONS"},
    CampaignObjective.SALES:       {"objective": "OUTCOME_SALES",
                                    "link": "LINK_CLICKS", "whatsapp": "CONVERSATIONS"},
    # Page-follower growth. Not one of Meta's six headline objectives — it is
    # ENGAGEMENT with PAGE_LIKES — but it is a distinct thing a client asks for.
    # PAGE_LIKES, not POST_ENGAGEMENT. Meta rejects POST_ENGAGEMENT once the ad set
    # promotes a Page — "Performance goal isn't available … with your campaign
    # objective" (subcode 2446286) — and PAGE_LIKES is the goal that actually grows a
    # following. Live-caught; the old value had never been exercised because no
    # followers campaign had been launched through this path.
    CampaignObjective.FOLLOWERS:   {"objective": "OUTCOME_ENGAGEMENT",
                                    "link": "PAGE_LIKES", "whatsapp": "PAGE_LIKES"},
    # Legacy value on plans written before the client could choose. It always meant
    # "Click-to-WhatsApp conversations", which is ENGAGEMENT.
    CampaignObjective.CONVERSATIONS: {"objective": "OUTCOME_ENGAGEMENT",
                                      "link": "LINK_CLICKS", "whatsapp": "CONVERSATIONS"},
}

# What the client is choosing between, in Meta's own words plus a plain-language line.
CHOICES = [
    {"value": CampaignObjective.AWARENESS.value, "label": "Awareness",
     "blurb": "Reach as many people as possible.",
     "caveat": ""},
    {"value": CampaignObjective.TRAFFIC.value, "label": "Traffic",
     "blurb": "Send people to your link or website.",
     "caveat": ""},
    {"value": CampaignObjective.ENGAGEMENT.value, "label": "Engagement",
     "blurb": "Get people messaging you. Best for WhatsApp orders.",
     "caveat": ""},
    {"value": CampaignObjective.LEADS.value, "label": "Leads",
     "blurb": "Collect enquiries from interested people.",
     "caveat": ""},
    {"value": CampaignObjective.SALES.value, "label": "Sales",
     "blurb": "Find people likely to buy.",
     "caveat": ""},
]


# What the objective still needs to know before it can be launched, per objective.
# Only the question the previous answer makes necessary is asked — AWARENESS never
# needs a URL, and SALES on WhatsApp never needs a pixel event.
#
# These are OUR enums, not Meta's. The value is stored; the label is shown. A display
# label must never become business logic: renaming a button would then change what
# launches.
ACTIONS: dict[str, list[dict]] = {
    CampaignObjective.AWARENESS.value: [
        {"value": "remember_brand", "label": "Remember my business"},
        {"value": "watch_video", "label": "Watch my video"},
        {"value": "see_business", "label": "See my business"},
    ],
    CampaignObjective.TRAFFIC.value: [
        {"value": "visit_site", "label": "Visit my website"},
    ],
    CampaignObjective.ENGAGEMENT.value: [
        {"value": "send_message", "label": "Message me"},
        {"value": "post_engagement", "label": "Like, comment or share"},
        {"value": "video_views", "label": "Watch my video"},
    ],
    CampaignObjective.LEADS.value: [
        {"value": "submit_lead", "label": "Send me their details"},
    ],
    CampaignObjective.SALES.value: [
        {"value": "purchase", "label": "Buy something"},
        {"value": "booking", "label": "Book an appointment"},
        {"value": "subscription", "label": "Start a subscription"},
        {"value": "application", "label": "Apply or register"},
        {"value": "payment", "label": "Pay a deposit"},
    ],
    CampaignObjective.FOLLOWERS.value: [
        {"value": "follow_page", "label": "Follow my page"},
    ],
}

# A destination that answers "where does the tap go" for each objective. SALES and
# LEADS can also be fulfilled in a DM, which is how most Nigerian SMEs actually sell —
# refusing that pairing would be modelling Meta's docs instead of the business.
_ALLOWED_DESTINATIONS: dict[str, set[str]] = {
    CampaignObjective.AWARENESS.value: {"whatsapp", "website", "instagram_dm", "custom"},
    CampaignObjective.TRAFFIC.value: {"website", "custom"},
    CampaignObjective.ENGAGEMENT.value: {"whatsapp", "instagram_dm", "website", "custom"},
    CampaignObjective.LEADS.value: {"whatsapp", "instagram_dm", "website", "custom"},
    CampaignObjective.SALES.value: {"whatsapp", "instagram_dm", "website", "custom"},
    CampaignObjective.FOLLOWERS.value: {"whatsapp", "website", "instagram_dm", "custom"},
    # Legacy. Plans written before the client could choose carry CONVERSATIONS, and
    # they are still launchable — an objective this never knew about would otherwise
    # read as "no destination is allowed" and block every one of them.
    CampaignObjective.CONVERSATIONS.value: {"whatsapp", "website", "instagram_dm", "custom"},
}

# Objectives whose desired action names a measurable conversion — the thing the
# client counts as a customer. Asked only for these; inventing one elsewhere would
# put a number on the review screen that nothing measures.
_NEEDS_ACTION = {CampaignObjective.SALES.value, CampaignObjective.AWARENESS.value,
                 CampaignObjective.ENGAGEMENT.value}


def destinations_for(objective: CampaignObjective) -> list[str]:
    """Where this objective is allowed to send people."""
    return sorted(_ALLOWED_DESTINATIONS.get(objective.value, set()))


def actions_for(objective: CampaignObjective) -> list[dict]:
    """The follow-up choices this objective still needs, or [] if it needs none."""
    return ACTIONS.get(objective.value, [])


def coerce_action(objective: CampaignObjective, value: Optional[str]) -> str:
    """The desired action as one of OURS, or "" if it is not valid for this objective.

    Scoped to the objective on purpose: "purchase" is a real action under SALES and
    meaningless under AWARENESS, and accepting it there would launch a campaign
    optimising for something the objective cannot deliver.
    """
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return ""
    allowed = {a["value"] for a in actions_for(objective)}
    aliases = {
        "messages": "send_message", "message": "send_message", "conversations": "send_message",
        "video": "watch_video", "views": "video_views",
        "lead": "submit_lead", "leads": "submit_lead", "enquiry": "submit_lead",
        "buy": "purchase", "sale": "purchase", "sales": "purchase",
        "book": "booking", "deposit": "payment", "register": "application",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in allowed else ""


def validate(objective: CampaignObjective, *, destination_type: str = "",
             desired_action: str = "", destination_link: str = "",
             require_action: bool = True) -> list[str]:
    """Everything still wrong with this configuration, in the client's language.

    Server-side and total: the frontend decides what to ASK, this decides what may
    LAUNCH. Returning every problem at once rather than the first one means a client
    fixes one form, not three in sequence.

    require_action=False asks only whether what IS set can launch. The launch path
    uses it: a plan built before the client was ever asked carries no action, and
    refusing it at commit would strand campaigns that were valid when planned. An
    action that contradicts the objective is still refused either way — absent is
    survivable, wrong is not.
    """
    problems: list[str] = []

    allowed = _ALLOWED_DESTINATIONS.get(objective.value, set())
    if not destination_type:
        problems.append("Choose where people should go when they tap the ad.")
    elif destination_type not in allowed:
        problems.append(
            f"A {objective.value} campaign cannot send people to "
            f"{destination_type.replace('_', ' ')}.")

    # A website destination with no link launches an ad that goes nowhere — Meta
    # accepts the campaign and rejects the ad, so this has to be caught here.
    if destination_type in ("website", "custom") and not destination_link:
        problems.append("Add the web address people should land on.")

    if require_action and objective.value in _NEEDS_ACTION and not desired_action:
        problems.append("Choose what you want people to do.")
    elif desired_action and not coerce_action(objective, desired_action):
        problems.append(
            f"'{desired_action}' is not something a {objective.value} campaign can "
            f"optimise for.")

    return problems


def clear_incompatible(objective: CampaignObjective, *, destination_type: str = "",
                       desired_action: str = "") -> dict[str, str]:
    """What survives an objective change, as the fields to keep.

    Changing the objective silently keeping a now-invalid action is how a campaign
    launches optimising for something the client never chose for THIS goal — so an
    answer that no longer applies is dropped and asked again, rather than coerced
    into the nearest valid one.
    """
    keep_destination = (destination_type
                        if destination_type in _ALLOWED_DESTINATIONS.get(objective.value, set())
                        else "")
    return {
        "destination_type": keep_destination,
        "desired_action": coerce_action(objective, desired_action),
    }


def coerce(value: Optional[str]) -> Optional[CampaignObjective]:
    """Whatever the client picked, as an objective — or None if it is not one of ours."""
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return None
    aliases = {
        "outcome_awareness": "awareness", "reach": "awareness", "brand_awareness": "awareness",
        "outcome_traffic": "traffic", "clicks": "traffic", "website": "traffic",
        "outcome_engagement": "engagement", "messages": "engagement",
        "conversations": "engagement", "whatsapp": "engagement",
        "outcome_leads": "leads", "lead_generation": "leads", "enquiries": "leads",
        "outcome_sales": "sales", "conversions": "sales", "purchases": "sales",
        "page_likes": "followers", "likes": "followers",
    }
    raw = aliases.get(raw, raw)
    try:
        return CampaignObjective(raw)
    except ValueError:
        return None


def meta_objective(objective: CampaignObjective) -> str:
    """The OUTCOME_* value Meta stores — what Ads Manager shows the client."""
    return _META.get(objective, _META[CampaignObjective.ENGAGEMENT])["objective"]


def optimization_goal(objective: CampaignObjective, *, is_whatsapp: bool) -> str:
    """What Meta's delivery system optimises toward, given where the tap goes.

    The same objective optimises differently by destination: ENGAGEMENT with a
    Click-to-WhatsApp ad optimises for CONVERSATIONS, but a link ad has no conversation
    to count, so it optimises for POST_ENGAGEMENT instead.
    """
    entry = _META.get(objective, _META[CampaignObjective.ENGAGEMENT])
    return entry["whatsapp" if is_whatsapp else "link"]


def caveat_for(objective: CampaignObjective) -> str:
    """The limit worth stating before the client picks, if any."""
    for c in CHOICES:
        if c["value"] == objective.value:
            return c["caveat"]
    return ""
