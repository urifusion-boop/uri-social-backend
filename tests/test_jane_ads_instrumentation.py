"""
Unit tests for Jane + Ads instrumentation (PRD §1.8): log every Jane decision and
every user override. Against the in-memory store, no DB. Also covers
decision_engine.apply_platform_override, the pure helper an override applies.
"""
import asyncio

import pytest

from app.agents.jane_ads.decision_engine import apply_platform_override, choose_platform
from app.agents.jane_ads.instrumentation import (
    InMemoryInstrumentationStore,
    InstrumentationService,
)
from app.agents.jane_ads.models import CampaignRequest, Goal, PlanDecision, Platform


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _req(**kw) -> CampaignRequest:
    base = dict(business_id="b1", budget_ngn=10_000.0, goal=Goal.MESSAGES)
    base.update(kw)
    return CampaignRequest(**base)


def _svc() -> InstrumentationService:
    return InstrumentationService(InMemoryInstrumentationStore())


# ── apply_platform_override (pure) ─────────────────────────────────────────────

def test_override_rebuilds_platforms_to_chosen_set():
    res = choose_platform(_req(category="fashion", budget_ngn=20_000), 20_000, 20_000)
    overridden = apply_platform_override(res.plan, [Platform.GOOGLE])
    assert [p.platform for p in overridden.platforms] == [Platform.GOOGLE]


def test_override_splits_total_budget_evenly_across_chosen():
    res = choose_platform(_req(budget_ngn=20_000), 20_000, 20_000)
    overridden = apply_platform_override(res.plan, [Platform.META, Platform.GOOGLE])
    assert {p.platform: p.budget_ngn for p in overridden.platforms} == {
        Platform.META: 10_000.0, Platform.GOOGLE: 10_000.0,
    }


def test_override_preserves_goal_and_behaviour():
    res = choose_platform(_req(goal=Goal.LEADS), 10_000, 10_000)
    overridden = apply_platform_override(res.plan, [Platform.GOOGLE])
    assert overridden.goal == Goal.LEADS
    assert overridden.behaviour == res.plan.behaviour


def test_override_dedupes_repeated_platforms():
    res = choose_platform(_req(budget_ngn=20_000), 20_000, 20_000)
    overridden = apply_platform_override(res.plan, [Platform.META, Platform.META])
    assert [p.platform for p in overridden.platforms] == [Platform.META]


def test_override_rejects_empty_choice():
    res = choose_platform(_req(), 10_000, 10_000)
    with pytest.raises(ValueError):
        apply_platform_override(res.plan, [])


# ── InstrumentationService.record_decision ─────────────────────────────────────

def test_record_decision_logs_a_plan_result():
    svc = _svc()
    res = choose_platform(_req(), 10_000, 10_000)
    _run(svc.record_decision("b1", res))
    logged = _run(svc.decisions_for("b1"))
    assert len(logged) == 1
    assert logged[0].decision == PlanDecision.PLAN
    assert logged[0].goal == Goal.MESSAGES
    assert logged[0].overridden is False
    assert logged[0].jane_platforms == logged[0].final_platforms


def test_record_decision_flags_override_when_final_differs():
    svc = _svc()
    res = choose_platform(_req(budget_ngn=20_000), 20_000, 20_000)
    final = [Platform.GOOGLE]
    _run(svc.record_decision("b1", res, final_platforms=final))
    logged = _run(svc.decisions_for("b1"))[0]
    assert logged.overridden is True
    assert logged.final_platforms == final
    assert logged.jane_platforms != final


def test_record_decision_logs_advise_result():
    svc = _svc()
    res = choose_platform(_req(budget_ngn=100), 100, 100)   # too small for any platform
    assert res.decision == PlanDecision.ADVISE
    _run(svc.record_decision("b1", res))
    logged = _run(svc.decisions_for("b1"))[0]
    assert logged.decision == PlanDecision.ADVISE
    assert logged.explanation == res.advice.reason
    assert logged.trace == res.advice.trace


# ── InstrumentationService.record_override ─────────────────────────────────────

def test_record_override_is_retrievable():
    svc = _svc()
    _run(svc.record_override("b1", jane_platforms=[Platform.META],
                              user_platforms=[Platform.GOOGLE], reason="client insists on search"))
    logged = _run(svc.overrides_for("b1"))
    assert len(logged) == 1
    assert logged[0].jane_platforms == [Platform.META]
    assert logged[0].user_platforms == [Platform.GOOGLE]
    assert logged[0].reason == "client insists on search"


def test_logs_are_scoped_per_business():
    svc = _svc()
    _run(svc.record_override("b1", jane_platforms=[Platform.META], user_platforms=[Platform.GOOGLE]))
    _run(svc.record_override("b2", jane_platforms=[Platform.META], user_platforms=[Platform.TIKTOK]))
    assert len(_run(svc.overrides_for("b1"))) == 1
    assert len(_run(svc.overrides_for("b2"))) == 1
    assert _run(svc.overrides_for("b1"))[0].user_platforms == [Platform.GOOGLE]
