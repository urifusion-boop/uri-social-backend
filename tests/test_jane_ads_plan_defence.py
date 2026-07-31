"""
Unit tests for Plan Defence (plan_defence.py) — Jane explaining and defending a plan
she already built.

classify_followup and explain_plan are LLM-backed (mocked here); what_if is pure and
re-runs the REAL decision engine, so its test asserts against an independently
computed expected plan rather than a mock.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.jane_ads.decision_engine import plan_campaign
from app.agents.jane_ads.models import (
    ABTestScope, CampaignObjective, CampaignPlan, CampaignRequest, CreativeContext,
    GeoMode, GeoPin, GeoPlan, Goal, OfferType, PlanDecision, Platform, PlatformPlan,
    PurchaseBehaviour,
)
from app.agents.jane_ads.plan_defence import (
    FollowupIntent,
    NlUnavailableError,
    classify_followup,
    explain_plan,
    what_if,
)
from app.agents.jane_ads.summary import build_campaign_summary


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _plan(**kw) -> CampaignPlan:
    base = dict(
        business_id="b1", goal=Goal.MESSAGES, behaviour=PurchaseBehaviour.DISCOVER,
        platforms=[PlatformPlan(platform=Platform.META, budget_ngn=20_000, days=7,
                                variants=1, test_scope=ABTestScope.NONE,
                                objective=CampaignObjective.CONVERSATIONS)],
        per_business_cap_ngn=20_000, account_cap_ngn=20_000, estimated_conversations=40,
        geo=GeoPlan(mode=GeoMode.OWN_RADIUS, city="Lagos",
                    pins=[GeoPin(name="Surulere", reason="dense commercial area")],
                    explanation="Surulere has the foot traffic your shop needs."),
        explanation="I chose Instagram + Facebook because your customers discover this by scrolling.",
        trace=["Goal of this campaign: MESSAGES.", "Resolved behaviour: DISCOVER."],
    )
    base.update(kw)
    return CampaignPlan(**base)


def _req(**kw) -> CampaignRequest:
    base = dict(business_id="b1", business_name="Mama Kitchen", category="restaurant",
                goal=Goal.MESSAGES, offer_type=OfferType.PRODUCT, budget_ngn=20_000,
                creative=CreativeContext())
    base.update(kw)
    return CampaignRequest(**base)


def _mock_openai_response(content: dict | str):
    payload = content if isinstance(content, str) else json.dumps(content)
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=payload))]
    return resp


# ── classify_followup ───────────────────────────────────────────────────────────

def test_classify_followup_detects_a_why_question():
    mock_create = AsyncMock(return_value=_mock_openai_response(
        {"kind": "question", "corrected_field": "", "corrected_value": "", "what_if_budget_ngn": None},
    ))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        intent = _run(classify_followup("Why did you pick ₦25,000 for this?"))
    assert intent.kind == "question"


def test_classify_followup_detects_a_what_if_and_resolves_the_amount():
    mock_create = AsyncMock(return_value=_mock_openai_response(
        {"kind": "question", "corrected_field": "", "corrected_value": "", "what_if_budget_ngn": 12500},
    ))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        intent = _run(classify_followup("What if I spent half of that?", current_budget_ngn=25_000))
    assert intent.kind == "question"
    assert intent.what_if_budget_ngn == 12500


def test_classify_followup_detects_a_challenge():
    mock_create = AsyncMock(return_value=_mock_openai_response(
        {"kind": "challenge", "corrected_field": "target_audience",
         "corrected_value": "students aren't my main buyers", "what_if_budget_ngn": None},
    ))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        intent = _run(classify_followup("Actually, students aren't my main buyers."))
    assert intent.kind == "challenge"
    assert intent.corrected_field == "target_audience"


def test_classify_followup_detects_unrelated_new_campaign():
    mock_create = AsyncMock(return_value=_mock_openai_response(
        {"kind": "new_campaign", "corrected_field": "", "corrected_value": "", "what_if_budget_ngn": None},
    ))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        intent = _run(classify_followup("Also I want to run a totally separate campaign for my other shop."))
    assert intent.kind == "new_campaign"


def test_classify_followup_defaults_to_new_campaign_on_model_error():
    with patch("openai.AsyncOpenAI", side_effect=Exception("down")):
        intent = _run(classify_followup("why this budget?"))
    assert intent.kind == "new_campaign"


def test_classify_followup_new_campaign_on_empty_message():
    intent = _run(classify_followup(""))
    assert intent.kind == "new_campaign"


# ── explain_plan ────────────────────────────────────────────────────────────────

def test_explain_plan_prompt_only_contains_supplied_data_no_fabrication_possible():
    plan = _plan()
    req = _req()
    summary = build_campaign_summary(plan, req)
    captured = {}

    async def _capture_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_openai_response("Your budget was split for the strongest signal.")

    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = _capture_create
        answer = _run(explain_plan("Why this platform?", plan, req, summary, {"city": "Lagos"}))

    prompt = captured["messages"][0]["content"]
    # Every number the model could possibly quote must trace back to the real plan/
    # summary — spot-check a few concrete figures actually present in the fixtures.
    assert "20000.0" in prompt or "20,000" in prompt or "20000" in prompt
    assert plan.explanation in prompt
    assert "Surulere" in prompt
    assert answer == "Your budget was split for the strongest signal."


def test_explain_plan_raises_when_openai_not_configured():
    with patch("app.agents.jane_ads.plan_defence.settings") as mock_settings:
        mock_settings.jane_ads_openai_key = ""
        with pytest.raises(NlUnavailableError):
            _run(explain_plan("why?", _plan(), _req(), None, {}))


def test_explain_plan_raises_nl_unavailable_on_model_error():
    with patch("openai.AsyncOpenAI", side_effect=Exception("down")):
        with pytest.raises(NlUnavailableError):
            _run(explain_plan("why?", _plan(), _req(), None, {}))


# ── what_if — must call the REAL decision engine, never estimate ───────────────

def test_what_if_matches_independently_computed_plan_for_same_budget_change():
    plan = _plan()
    req = _req()
    result = what_if(plan, req, budget_ngn=10_000)

    # Independently re-derive the same hypothetical via the real engine directly —
    # what_if must produce the SAME plan, not a model-estimated approximation.
    expected = plan_campaign(req.model_copy(update={"budget_ngn": 10_000}),
                             plan.per_business_cap_ngn, plan.account_cap_ngn)
    assert expected.decision == PlanDecision.PLAN
    assert result.hypothetical_plan.platforms == expected.plan.platforms
    assert result.hypothetical_plan.behaviour == expected.plan.behaviour
    assert result.changed == "budget_ngn: 20,000 -> 10,000"


def test_what_if_narrative_reflects_real_duration_change():
    # A much smaller budget forces a shorter run (Meta's daily-floor gate caps days
    # to what the smaller total can actually clear) — the narrative must say so,
    # derived from the two REAL summaries, not a guess.
    plan = _plan()
    req = _req(budget_ngn=20_000)
    result = what_if(plan, req, budget_ngn=6_000)
    assert "6,000" in result.narrative
    assert "20,000" in result.narrative
    assert "days" in result.narrative


def test_what_if_raises_with_janes_own_reason_when_budget_is_unworkable():
    plan = _plan()
    req = _req()
    with pytest.raises(ValueError):
        what_if(plan, req, budget_ngn=1)


def test_what_if_reuses_supplied_summary_instead_of_recomputing_original():
    plan = _plan()
    req = _req()
    supplied_summary = build_campaign_summary(plan, req)
    result = what_if(plan, req, budget_ngn=10_000, summary=supplied_summary)
    assert result.original == supplied_summary
