"""
Unit tests for Multi-Plan Audience Variants (plan_variants.py).

max_selectable_plans is pure arithmetic off the real budget floors — tested without
mocks. generate_plan_variants is LLM-backed (mocked here, mirroring
test_jane_ads_creative.py's _call_ad_copy_model pattern); we test the parsing/
code-enforcement around the model call, not the model's own judgment.
"""
import asyncio
import json
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.jane_ads import constants as C
from app.agents.jane_ads.jane_consultant import ConsultantBrief
from app.agents.jane_ads.models import PlanVariant
from app.agents.jane_ads.plan_variants import (
    PlanVariantsUnavailableError,
    generate_plan_variants,
    max_selectable_plans,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _brief(**kw) -> ConsultantBrief:
    base = dict(business_name="Test Biz", category="solar installer", budget_ngn=80_000.0, city="Lekki")
    base.update(kw)
    return ConsultantBrief(**base)


def _mock_openai_response(content: dict):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=json.dumps(content)))]
    return resp


# ── max_selectable_plans — pure, computed off real floors (spec §6.1) ──────────

def test_tier_1_below_15k_allows_only_one():
    n, reason = max_selectable_plans(12_000.0)
    assert n == 1
    assert "12,000" in reason


def test_tier_2_between_15k_and_50k_allows_two_when_floor_clears():
    n, reason = max_selectable_plans(30_000.0)
    assert n == 2


def test_tier_2_still_one_if_splitting_would_starve_a_variant():
    # With today's real constants (tier 2 starts at ₦15,000, Meta's useful min is
    # ₦5,000), half of the smallest tier-2 budget (₦7,500) always comfortably
    # clears the floor — this safety branch is a guard against the constants
    # changing, not a reachable case today. Exercise it directly by raising the
    # tier-2 floor past what half of it could actually fund.
    with patch("app.agents.jane_ads.plan_variants.C.PLAN_VARIANT_TIER_2_NGN", 8_000.0):
        n, reason = max_selectable_plans(8_000.0)
    assert n == 1
    assert "clear the useful minimum" in reason


def test_tier_3_between_50k_and_250k_allows_three():
    n, _ = max_selectable_plans(100_000.0)
    assert n == 3


def test_tier_4_above_250k_allows_multiple():
    n, _ = max_selectable_plans(300_000.0)
    assert n == 5


def test_boundary_exactly_at_tier_2_is_tier_2_not_tier_1():
    n, _ = max_selectable_plans(C.PLAN_VARIANT_TIER_2_NGN)
    assert n == 2


# ── generate_plan_variants — parsing/code-enforcement around the model call ────

def test_generate_plan_variants_parses_ranked_list():
    payload = {
        "variants": [
            {
                "who_its_for": "property developers doing multiple units",
                "audience_segment": "B2B developers",
                "geo_pockets": ["Lekki construction sites"],
                "trigger": "a developer fitting out ten units is one relationship worth ten jobs",
                "why_this_could_work": "buys on competence, comes back",
                "trade_off": "small audience, longer conversation",
                "needs_video": True,
                "rank": 1,
                "recommended": True,
            },
            {
                "who_its_for": "people fitting out a new place",
                "audience_segment": "homeowners 30-55",
                "geo_pockets": ["Ajah", "Sangotedo"],
                "trigger": "solar gets bought at the moment someone sets up a new home",
                "why_this_could_work": "catches them while deciding",
                "trade_off": "slower to convert",
                "needs_video": True,
                "rank": 2,
                "recommended": False,
            },
        ],
        "recommendation_reason": "I'd start with the developers — bigger jobs.",
    }
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        result = _run(generate_plan_variants(_brief()))

    assert len(result.variants) == 2
    assert result.variants[0].rank == 1
    assert result.variants[0].recommended is True
    assert result.variants[0].who_its_for == "property developers doing multiple units"
    assert result.variants[1].recommended is False
    assert result.recommendation_reason == "I'd start with the developers — bigger jobs."
    assert result.max_selectable == 3   # ₦80,000 budget in _brief() → tier 3 (§6.1)


def test_generate_plan_variants_caps_at_five():
    payload = {"variants": [
        {"who_its_for": f"segment {i}", "audience_segment": f"seg{i}", "trigger": "t",
         "why_this_could_work": "w", "trade_off": "x", "rank": i, "recommended": i == 1}
        for i in range(1, 8)
    ], "recommendation_reason": "r"}
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        result = _run(generate_plan_variants(_brief()))
    assert len(result.variants) == 5


def test_generate_plan_variants_enforces_exactly_one_recommended():
    # Model misbehaves: zero variants marked recommended.
    payload = {"variants": [
        {"who_its_for": "a", "audience_segment": "a", "trigger": "t", "why_this_could_work": "w",
         "trade_off": "x", "rank": 1, "recommended": False},
        {"who_its_for": "b", "audience_segment": "b", "trigger": "t", "why_this_could_work": "w",
         "trade_off": "x", "rank": 2, "recommended": False},
    ], "recommendation_reason": "r"}
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        result = _run(generate_plan_variants(_brief()))
    assert sum(1 for v in result.variants) == 2
    assert sum(1 for v in result.variants if v.recommended) == 1
    assert result.variants[0].recommended is True   # falls back to rank 1


def test_generate_plan_variants_enforces_exactly_one_recommended_when_model_marks_two():
    payload = {"variants": [
        {"who_its_for": "a", "audience_segment": "a", "trigger": "t", "why_this_could_work": "w",
         "trade_off": "x", "rank": 1, "recommended": True},
        {"who_its_for": "b", "audience_segment": "b", "trigger": "t", "why_this_could_work": "w",
         "trade_off": "x", "rank": 2, "recommended": True},
    ], "recommendation_reason": "r"}
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        result = _run(generate_plan_variants(_brief()))
    assert sum(1 for v in result.variants if v.recommended) == 1


def test_generate_plan_variants_attaches_computed_not_generated_budget_gate():
    # ₦12,000 → tier 1 → max_selectable=1, regardless of how many variants the model returns.
    payload = {"variants": [
        {"who_its_for": "a", "audience_segment": "a", "trigger": "t", "why_this_could_work": "w",
         "trade_off": "x", "rank": 1, "recommended": True},
        {"who_its_for": "b", "audience_segment": "b", "trigger": "t", "why_this_could_work": "w",
         "trade_off": "x", "rank": 2, "recommended": False},
    ], "recommendation_reason": "r"}
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        result = _run(generate_plan_variants(_brief(budget_ngn=12_000.0)))
    assert result.max_selectable == 1
    assert result.variants[0].budget_shared_ngn is None   # nothing to share at tier 1
    assert result.variants[0].budget_alone_ngn == 12_000.0


def test_generate_plan_variants_shows_shared_budget_when_multi_selectable():
    payload = {"variants": [
        {"who_its_for": "a", "audience_segment": "a", "trigger": "t", "why_this_could_work": "w",
         "trade_off": "x", "rank": 1, "recommended": True},
    ], "recommendation_reason": "r"}
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        result = _run(generate_plan_variants(_brief(budget_ngn=40_000.0)))
    assert result.max_selectable == 2
    assert result.variants[0].budget_shared_ngn == 20_000.0


def test_generate_plan_variants_raises_when_openai_not_configured():
    with patch("app.agents.jane_ads.plan_variants.settings") as mock_settings:
        mock_settings.jane_ads_openai_key = ""
        with pytest.raises(PlanVariantsUnavailableError):
            _run(generate_plan_variants(_brief()))


def test_generate_plan_variants_raises_on_model_error():
    with patch("openai.AsyncOpenAI", side_effect=Exception("down")):
        with pytest.raises(PlanVariantsUnavailableError):
            _run(generate_plan_variants(_brief()))


def test_generate_plan_variants_never_a_measured_number_hard_check():
    # A pure sanity check on the PlanVariant model itself — no numeric estimate
    # fields exist to invent a fake number into (spec §5), only qualitative text.
    v = PlanVariant(rank=1, who_its_for="x", audience_segment="y", trigger="t",
                    why_this_could_work="w", trade_off="z")
    numeric_fields = {name for name, field in PlanVariant.model_fields.items()
                      if field.annotation in (int, float, Optional[float])}
    assert numeric_fields <= {"rank", "budget_alone_ngn", "budget_shared_ngn"}
