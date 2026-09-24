"""The campaign objective the CLIENT chooses.

Jane used to pick it from the goal she inferred, so every campaign came out
OUTCOME_ENGAGEMENT or OUTCOME_TRAFFIC — a client who asked for sales found
"Objective: Engagement" on their campaign in Ads Manager. These cover that the choice
reaches Meta, and that the optimisation matches both the objective and where the tap
goes.

Every pairing asserted here was validated against the live ad account by creating a
real campaign per objective and validating an ad set under it (2026-09-24).
"""
import pytest

from app.agents.jane_ads.models import CampaignObjective
from app.agents.jane_ads.objectives import (
    CHOICES, caveat_for, coerce, meta_objective, optimization_goal,
)


@pytest.mark.parametrize("objective,expected", [
    (CampaignObjective.AWARENESS, "OUTCOME_AWARENESS"),
    (CampaignObjective.TRAFFIC, "OUTCOME_TRAFFIC"),
    (CampaignObjective.ENGAGEMENT, "OUTCOME_ENGAGEMENT"),
    (CampaignObjective.LEADS, "OUTCOME_LEADS"),
    (CampaignObjective.SALES, "OUTCOME_SALES"),
    (CampaignObjective.FOLLOWERS, "OUTCOME_ENGAGEMENT"),
])
def test_each_objective_reaches_meta_as_itself(objective, expected):
    """What the client picks is what Ads Manager shows them. A client who chose Sales
    must not find Engagement on their campaign."""
    assert meta_objective(objective) == expected


def test_a_whatsapp_ad_optimises_for_conversations_a_link_ad_cannot():
    """The same objective optimises differently by destination: a link ad has no
    conversation for Meta to count."""
    assert optimization_goal(CampaignObjective.ENGAGEMENT, is_whatsapp=True) == "CONVERSATIONS"
    assert optimization_goal(CampaignObjective.ENGAGEMENT, is_whatsapp=False) == "POST_ENGAGEMENT"


def test_awareness_never_optimises_for_conversations():
    """CONVERSATIONS is not available under OUTCOME_AWARENESS — Meta rejects it."""
    for wa in (True, False):
        assert optimization_goal(CampaignObjective.AWARENESS, is_whatsapp=wa) == "REACH"


def test_a_followers_campaign_optimises_for_page_likes():
    """It stays on the Page. POST_ENGAGEMENT is rejected once the ad set promotes a
    Page — "Performance goal isn't available" (subcode 2446286) — and PAGE_LIKES is
    what actually grows a following."""
    for wa in (True, False):
        assert optimization_goal(CampaignObjective.FOLLOWERS, is_whatsapp=wa) == "PAGE_LIKES"


def test_the_legacy_objective_still_loads():
    """Plans and decision records written before the client could choose carry
    CONVERSATIONS. It always meant Click-to-WhatsApp, which is ENGAGEMENT."""
    assert meta_objective(CampaignObjective.CONVERSATIONS) == "OUTCOME_ENGAGEMENT"
    assert optimization_goal(CampaignObjective.CONVERSATIONS, is_whatsapp=True) == "CONVERSATIONS"


@pytest.mark.parametrize("typed,expected", [
    ("Sales", CampaignObjective.SALES),
    ("OUTCOME_TRAFFIC", CampaignObjective.TRAFFIC),
    ("messages", CampaignObjective.ENGAGEMENT),
    ("page_likes", CampaignObjective.FOLLOWERS),
    ("  Awareness  ", CampaignObjective.AWARENESS),
    ("", None),
    ("nonsense", None),
])
def test_whatever_the_client_picked_is_understood(typed, expected):
    assert coerce(typed) == expected


def test_app_promotion_is_not_offered():
    """Meta accepts OUTCOME_APP_PROMOTION, but Uri has no app to install — offering it
    would set an objective that can never be honoured."""
    assert "app" not in " ".join(c["value"] for c in CHOICES).lower()


def test_no_objective_second_guesses_the_clients_choice():
    """Sales and Leads go to Meta as themselves and Meta optimises them. Warning about
    them in the picker only made clients doubt a setting that works."""
    for o in CampaignObjective:
        assert caveat_for(o) == "", o


def test_every_choice_is_a_real_objective():
    for c in CHOICES:
        assert coerce(c["value"]) is not None, c["value"]
        assert c["label"] and c["blurb"]


# ── Typing the objective is the same as tapping it ────────────────────────────

def test_typing_an_objective_is_understood_as_a_choice():
    """Someone who types "awareness" instead of tapping the card has chosen just as
    deliberately. Treating it as ordinary prose left the objective to be inferred —
    the exact guesswork the picker exists to remove."""
    for typed in ("awareness", "Awareness", "  SALES  ", "leads", "traffic", "engagement"):
        assert coerce(typed) is not None, typed


def test_a_brief_that_merely_mentions_one_is_not_a_choice():
    """"Get me more sales in Lekki" is a brief, not a pick. Reading an objective out of
    a sentence would hand the client a campaign goal they never chose — and quietly
    beat an explicit pick they might make afterwards."""
    for brief in ("get me more sales in Lekki", "I want awareness for my salon",
                  "traffic is bad on my website", "sell more wigs"):
        assert coerce(brief) is None, brief
