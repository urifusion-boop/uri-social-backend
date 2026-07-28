"""
Unit tests for the strategic consultant layer's pure mapping (jane_consultant.py's
_coerce). The LLM call itself is live/non-deterministic; here we test that raw model
JSON — in both the "ask" and "ready" shapes — normalizes correctly into a
ConsultantBrief, and that the result stays a valid ParsedCampaign so
to_campaign_request() keeps working unchanged downstream.
"""
from app.agents.jane_ads.jane_consultant import ConsultantBrief, _coerce, build_history_turns
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
