"""
Unit tests for the strategic consultant layer's pure mapping (jane_consultant.py's
_coerce). The LLM call itself is live/non-deterministic; here we test that raw model
JSON — in both the "ask" and "ready" shapes — normalizes correctly into a
ConsultantBrief, and that the result stays a valid ParsedCampaign so
to_campaign_request() keeps working unchanged downstream.
"""
from app.agents.jane_ads.jane_consultant import (
    ConsultantBrief, _budget_grounded, _build_budget_confirmation_note, _coerce,
    _enforce_hard_requirements, _latest_user_reply, build_history_turns,
)
from app.agents.jane_ads.nl import to_campaign_request


def test_ask_stage_carries_the_clarify_message():
    b = _coerce({"stage": "ask", "clarify": "What are you advertising?"}, "", "")
    assert b.clarify == "What are you advertising?"
    assert b.missing == ["business_name"]  # nothing known yet


def test_ask_stage_no_missing_flag_once_business_known():
    # Once business identity is known, `missing` must stay empty — the consultant
    # flow never triggers the old chip UI, even mid-conversation.
    b = _coerce({"stage": "ask", "clarify": "What's your budget?"}, "Mama Kitchen", "restaurant")
    assert b.missing == []


def test_ask_stage_defaults_clarify_when_model_omits_it():
    b = _coerce({"stage": "ask"}, "", "")
    assert b.clarify


def test_ready_stage_maps_core_fields():
    b = _coerce({
        "stage": "ready", "business_name": "Mama Kitchen", "category": "restaurant",
        "goal": "sales", "offer_type": "product", "budget_ngn": 10000, "city": "Surulere",
    }, "", "")
    assert b.business_name == "Mama Kitchen"
    assert b.goal == "sales"
    assert b.offer_type == "product"
    assert b.budget_ngn == 10000
    req = to_campaign_request(b)
    assert req is not None
    assert req.budget_ngn == 10000


def test_ready_stage_rejects_invalid_enum_values():
    b = _coerce({"stage": "ready", "business_name": "x", "goal": "not_a_real_goal",
                "offer_type": "not_real_either", "budget_ngn": 5000}, "", "")
    assert b.goal is None
    assert b.offer_type is None
    # to_campaign_request still gates on offer_type — bad data can't sneak a plan through.
    assert to_campaign_request(b) is None


def test_ready_stage_carries_strategic_fields():
    b = _coerce({
        "stage": "ready", "business_name": "SolarCo", "budget_ngn": 20000,
        "offer_type": "service",
        "geo_mode": "watering_hole",
        "geo_areas": [{"name": "Ajah new estates", "reason": "solar gets bought when fitting out"}],
        "geo_explanation": "targeting where new construction is happening",
        "intermediary_note": "Property developers buy repeatedly, not individual homeowners.",
        "creative_fit_warning": "A brand video won't get messages this week.",
        "stated_plan": "I'll focus on new estates around Ajah rather than homeowners generally.",
    }, "", "")
    assert b.geo_mode == "watering_hole"
    assert b.geo_areas == [{"name": "Ajah new estates", "reason": "solar gets bought when fitting out"}]
    assert "developers" in b.intermediary_note
    assert "won't get messages" in b.creative_fit_warning
    assert "new estates" in b.stated_plan


def test_ready_stage_drops_geo_areas_missing_a_name():
    b = _coerce({"stage": "ready", "business_name": "x", "offer_type": "product",
                "budget_ngn": 5000, "geo_areas": [{"reason": "no name here"}, {"name": "Lekki"}]}, "", "")
    assert b.geo_areas == [{"name": "Lekki"}]


def test_ready_stage_rejects_invalid_geo_mode():
    b = _coerce({"stage": "ready", "business_name": "x", "offer_type": "product",
                "budget_ngn": 5000, "geo_mode": "somewhere_vague"}, "", "")
    assert b.geo_mode is None


def test_unknown_stage_falls_back_to_ask():
    b = _coerce({"stage": "confused"}, "", "")
    assert b.clarify
    assert b.missing == ["business_name"]


def test_desired_conversions_backwards_budget_still_works():
    # The backwards-budget conversion (router.py section 1.5) reads desired_conversions
    # off the SAME ParsedCampaign-shaped object — confirm the consultant's output carries it.
    b = _coerce({"stage": "ready", "business_name": "x", "offer_type": "product",
                "desired_conversions": 20}, "", "")
    assert b.desired_conversions == 20
    assert b.budget_ngn is None


# ── build_history_turns — the real conversation memory fix ────────────────────
# Without this, the consultant only ever saw the frontend's flattened "brief so far"
# (the CLIENT's fragments jammed together with no idea which answer matched which
# question), and looped re-asking the same ground forever. This turns saved chat
# messages into real, ordered role/content pairs so the model can track state.

def test_text_messages_become_ordered_turns():
    saved = [
        {"role": "user", "kind": "text", "text": "hey"},
        {"role": "jane", "kind": "text", "text": "What are you advertising?"},
        {"role": "user", "kind": "text", "text": "the product"},
    ]
    turns = build_history_turns(saved)
    assert turns == [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "What are you advertising?"},
        {"role": "user", "content": "the product"},
    ]


def test_result_with_question_becomes_an_assistant_turn():
    saved = [{"role": "jane", "kind": "result", "result": {"stage": "need_more", "question": "What's your budget?"}}]
    turns = build_history_turns(saved)
    assert turns == [{"role": "assistant", "content": "What's your budget?"}]


def test_result_planned_becomes_a_summarized_assistant_turn():
    saved = [{"role": "jane", "kind": "result", "result": {
        "stage": "planned", "plan": {"explanation": "Targeting Lekki estates for solar leads."},
    }}]
    turns = build_history_turns(saved)
    assert len(turns) == 1
    assert turns[0]["role"] == "assistant"
    assert "Targeting Lekki estates" in turns[0]["content"]


def test_empty_text_is_skipped():
    saved = [{"role": "user", "kind": "text", "text": "   "}, {"role": "user", "kind": "text", "text": "real answer"}]
    turns = build_history_turns(saved)
    assert turns == [{"role": "user", "content": "real answer"}]


def test_caps_to_last_24_turns():
    saved = [{"role": "user", "kind": "text", "text": f"turn {i}"} for i in range(30)]
    turns = build_history_turns(saved)
    assert len(turns) == 24
    assert turns[0]["content"] == "turn 6"
    assert turns[-1]["content"] == "turn 29"


# ── _budget_grounded / _enforce_hard_requirements ──────────────────────────────
# Regression coverage for a REAL bug found in live testing: the model treated a
# remembered past campaign's spend as if the client had restated it THIS campaign,
# and separately skipped geography entirely — both must be code-enforced, not just
# prompted, since prompting alone already let both through once.

def test_budget_grounded_when_no_remembered_budget():
    assert _budget_grounded(5000, None, "budget is 5000", []) is True


def test_budget_grounded_when_it_differs_from_remembered():
    # A genuinely different figure is presumed to come from THIS conversation.
    assert _budget_grounded(10000, 5000, "anything", []) is True


def test_budget_not_grounded_when_matching_remembered_with_no_proposal_and_no_confirmation():
    # The original bug this guard exists for: Jane silently carries forward a past
    # campaign's ₦5,000 spend, having never actually proposed it in THIS conversation
    # and with no confirmation from the client either.
    assert _budget_grounded(5000, 5000, "yes sure", []) is False


def test_budget_grounded_when_client_actually_restates_the_remembered_figure():
    history = [{"role": "user", "content": "yes 5000 sounds good"}]
    assert _budget_grounded(5000, 5000, "yes 5000 sounds good", history) is True


def test_budget_grounded_when_client_says_yes_to_janes_own_explicit_proposal():
    # Live-reported bug: Jane explicitly asks "want to do the same again (₦5,000)?" —
    # a deliberate, THIS-campaign confirmation, not a silent carry-over — and the
    # client answers plainly. Without this, Jane re-asked the identical question
    # forever because the client never retyped the digits themselves.
    history = [{"role": "assistant", "content": "Last time you spent ₦5,000, want the same again?"}]
    assert _budget_grounded(5000, 5000, "yes", history) is True


def test_budget_not_grounded_when_yes_does_not_follow_a_proposal_of_that_figure():
    # A bare "yes" with no assistant turn proposing the remembered figure at all must
    # NOT ground it — otherwise any unrelated "yes" would silently confirm a budget
    # the client never actually saw proposed.
    history = [{"role": "assistant", "content": "What area should I focus on?"}]
    assert _budget_grounded(5000, 5000, "yes", history) is False


def test_budget_not_grounded_when_none_or_zero():
    assert _budget_grounded(None, None, "x", []) is False
    assert _budget_grounded(0, None, "x", []) is False


def test_enforce_downgrades_to_ask_when_budget_not_grounded():
    ready = ConsultantBrief(business_name="Mama Kitchen", offer_type="product", budget_ngn=5000)
    result = _enforce_hard_requirements(ready, "hey", [], known_budget=5000)
    assert result.clarify
    assert "budget" in result.clarify.lower()
    assert to_campaign_request(result) is None


def test_enforce_downgrades_to_ask_when_geography_missing():
    ready = ConsultantBrief(business_name="Mama Kitchen", offer_type="product", budget_ngn=10000)
    result = _enforce_hard_requirements(ready, "budget is 10000", [], known_budget=None)
    assert result.clarify
    assert "area" in result.clarify.lower() or "city" in result.clarify.lower()


def test_enforce_passes_through_when_both_requirements_met():
    ready = ConsultantBrief(business_name="Mama Kitchen", offer_type="product",
                            budget_ngn=10000, city="Surulere", geo_mode="own_radius")
    result = _enforce_hard_requirements(ready, "budget is 10000 in Surulere", [], known_budget=None)
    assert result.city == "Surulere"
    assert to_campaign_request(result) is not None


def test_enforce_passes_through_non_local_without_a_city():
    ready = ConsultantBrief(business_name="OnlineShop", offer_type="product",
                            budget_ngn=10000, geo_mode="non_local")
    result = _enforce_hard_requirements(ready, "budget is 10000, we ship nationwide", [], known_budget=None)
    assert result.geo_mode == "non_local"


def test_enforce_leaves_an_ask_stage_untouched():
    asking = ConsultantBrief(clarify="What's your budget?")
    result = _enforce_hard_requirements(asking, "hey", [], known_budget=None)
    assert result is asking


# ── _build_budget_confirmation_note ─────────────────────────────────────────────
# Live-reported bug, 2026-08-12: the bare-affirmative case below already worked, but
# a client who instead retyped the remembered figure themselves ("10000, ikeja") got
# no steering at all — the model treated that restatement as ambiguous (given the
# `known_line` warning against silently carrying it forward) and re-asked the
# identical budget question anyway, even though _budget_grounded would have accepted
# that exact same restatement had the model just said "ready".

def test_stated_budget_is_accepted_without_a_remembered_one():
    """Superseded 2026-08-26. This asserted no note on a fresh thread, which is what
    let Jane re-ask a budget the client had just typed — live-observed: "Budget 20000
    for this campaign" answered with "can you confirm if this budget is still
    accurate". A stated figure is now told to the model plainly whether or not a
    remembered one exists; silence is reserved for when nothing was stated."""
    note = _build_budget_confirmation_note(None, "10000, ikeja", [])
    assert "10,000" in note and "do not ask them to confirm" in note
    assert _build_budget_confirmation_note(None, "ikeja", []) == ""


def test_note_fires_on_bare_affirmative_to_janes_own_proposal():
    history = [{"role": "assistant", "content": "Last time you spent ₦10,000, want the same again?"}]
    note = _build_budget_confirmation_note(10000, "yes", history)
    assert "10000" in note.replace(",", "")
    assert "budget again" in note


def test_note_fires_when_client_restates_the_remembered_figure_outright():
    # The exact live scenario: no "yes", just the number itself, alongside unrelated
    # new information (the city) in the same reply.
    note = _build_budget_confirmation_note(10000, "Get me more sales. 10000, ikeja", [])
    assert "10000" in note.replace(",", "")
    assert "budget again" in note


def test_no_note_when_bare_affirmative_does_not_follow_a_matching_proposal():
    history = [{"role": "assistant", "content": "What area should I focus on?"}]
    assert _build_budget_confirmation_note(10000, "yes", history) == ""


def test_no_note_when_latest_reply_has_no_matching_number():
    assert _build_budget_confirmation_note(10000, "Get me more sales. just ikeja", []) == ""


def test_latest_user_reply_takes_the_segment_after_the_last_period():
    assert _latest_user_reply("Get me more sales. 10000, ikeja") == "10000, ikeja"
    assert _latest_user_reply("") == ""
