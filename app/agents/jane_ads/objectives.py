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
