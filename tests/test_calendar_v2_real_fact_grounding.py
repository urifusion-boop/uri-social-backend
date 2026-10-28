"""
Even after URI's Brand Playbook had real pain points, objections, and proof
points on file, a live regenerate still showed most of a 30-item plan
reading as generic filler disconnected from any of them. Root cause: the
instruction to "draw on a real fact" was only ever a soft prompt suggestion
in _customer_psychology_block — nothing deterministically assigned which
fact an item should use, and nothing checked afterward whether it actually
did. Every other diversity problem this session (territory clustering, CTA
templates, crutch words) was only actually fixed once it stopped being a
request and became either a deterministic assignment or a hard, code-
enforced validation — this applies the same two moves here:

1. _flatten_psychology_facts turns the Playbook's pain points/needs/
   objections/proof points/etc into individual (label, fact) entries.
2. _generate_final_copy deterministically assigns each item one fact (by
   day_index, wrapping around the pool) and tells it to build around that
   fact specifically.
3. _fact_grounding_issue is a hard check reusing the words_to_avoid
   validation+correction-retry mechanism: if an item's actual text doesn't
   contain the assigned fact's real vocabulary, it's forced to retry —
   same backstop already proven for crutch words.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.content_calendar_v2.services import content_calendar_v2_service as svc
from app.agents.content_calendar_v2.services.content_calendar_v2_service import (
    _fact_grounding_issue,
    _flatten_psychology_facts,
    _significant_words,
)


def _concept(day_index, **overrides):
    base = {
        "day_index": day_index,
        "date": f"2026-11-{day_index + 1:02d}",
        "territory": "A_PROBLEM",
        "subject": "shipping delays",
        "angle": "mythbusting",
        "creative_device": {"category": "story", "device": "day_in_the_life"},
        "format": "image",
        "format_hint": "image",
        "objective": "engagement",
        "audience_segment": "first-time buyers",
        "concept_name": "Concept",
    }
    base.update(overrides)
    return base


def _valid_item(i, caption="A clean, specific caption with no banned language."):
    return {
        "title": f"Clean title {i}", "hook": f"Clean hook, an actual opening line {i}",
        "key_points": ["A genuinely specific first point here", "A genuinely specific second point here"],
        "description": "x", "caption_direction": "Specific guidance well over fifteen characters",
        "keywords": ["a", "b"], "cta": "Shop now",
        "topic": "topic", "promised_business_outcome": "x", "content_pillar": "x",
        "customer_journey_stage": "awareness",
        "video_idea": {
            "format": "talking_head", "hook": "An actual video opening line",
            "talking_points": ["point one"], "scenes": ["scene one"], "cta": "Watch now",
        },
        "holiday_reference": None,
        "exact_copy": {"headline": f"Clean headline {i}", "caption": caption, "hashtags": []},
        "carousel": None, "creative_concept_name": "x", "central_visual_idea": "x",
        "design_style": "x", "layout_direction": "x", "visual_metaphor": "",
        "ai_image_prompt": "a photo. no text, no logos.", "required_assets": [],
        "designer_execution_notes": "x", "reasoning": "x", "primary_kpi": "engagement",
    }


class TestFlattenPsychologyFacts:
    def test_each_list_item_becomes_its_own_fact(self):
        brand = {
            "customer_pain_points": ["Inconsistent content", "Lack of time"],
            "customer_objections": ["Too expensive"],
            "proof_points": ["Cut content time in half"],
        }
        facts = _flatten_psychology_facts(brand)
        assert ("Pain point", "Inconsistent content") in facts
        assert ("Pain point", "Lack of time") in facts
        assert ("Objection", "Too expensive") in facts
        assert ("Proof point", "Cut content time in half") in facts
        assert len(facts) == 4

    def test_why_customers_choose_us_included_as_its_own_fact(self):
        facts = _flatten_psychology_facts({"why_customers_choose_us": "We deliver same-day"})
        assert ("Why customers choose this business", "We deliver same-day") in facts

    def test_empty_brand_returns_empty_list(self):
        assert _flatten_psychology_facts({}) == []

    def test_blank_strings_filtered(self):
        facts = _flatten_psychology_facts({"customer_pain_points": ["", "  ", "Real one"]})
        assert facts == [("Pain point", "Real one")]


class TestFactGroundingIssue:
    def test_item_that_ignores_the_fact_is_flagged(self):
        idea = _valid_item(0, caption="URI helps your business grow in exciting ways.")
        issue = _fact_grounding_issue(idea, "Pain point", "Inconsistent content scheduling")
        assert issue is not None
        assert "does not actually engage" in issue

    def test_item_that_uses_the_facts_real_words_passes(self):
        idea = _valid_item(0, caption="Inconsistent scheduling kills momentum — URI keeps it steady.")
        issue = _fact_grounding_issue(idea, "Pain point", "Inconsistent content scheduling")
        assert issue is None

    def test_fact_with_no_significant_words_is_never_flagged(self):
        """A fact that's too short/generic to meaningfully check against
        (e.g. just stopwords) shouldn't block generation."""
        idea = _valid_item(0)
        issue = _fact_grounding_issue(idea, "Need", "that this")
        assert issue is None


class TestGroundingWiredIntoGeneration:
    def test_items_get_a_grounding_instruction_assigned_by_day_index(self):
        concepts_chunk = [_concept(0), _concept(1)]
        captured = {}

        async def fake(req):
            captured["prompt"] = req["messages"][0]["content"] if isinstance(req, dict) else ""
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps([_valid_item(0), _valid_item(1)])
            ))])

        brand = {"customer_pain_points": ["Inconsistent content"], "customer_needs": ["Reliable delivery"]}
        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=fake):
            asyncio.run(svc._generate_final_copy(
                brand=brand, concepts_chunk=concepts_chunk, platforms=["instagram"],
                existing_assets_summary="", force=True,
            ))

        prompt = captured["prompt"]
        assert "MUST GROUND IN THIS REAL FACT" in prompt
        assert "Inconsistent content" in prompt
        assert "Reliable delivery" in prompt

    def test_ungrounded_item_forces_a_correction_retry(self):
        concepts_chunk = [_concept(0)]
        calls = []

        async def fake(req):
            prompt = req["messages"][0]["content"] if isinstance(req, dict) else ""
            calls.append(prompt)
            if len(calls) == 1:
                payload = [_valid_item(0, caption="URI helps your business in exciting new ways.")]
            else:
                payload = [_valid_item(0, caption="Inconsistent scheduling is the real problem URI fixes.")]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(payload)
            ))])

        brand = {"customer_pain_points": ["Inconsistent content scheduling"]}
        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=fake):
            asyncio.run(svc._generate_final_copy(
                brand=brand, concepts_chunk=concepts_chunk, platforms=["instagram"],
                existing_assets_summary="", force=True,
            ))

        assert len(calls) == 2, "an ungrounded item should force a correction retry"
        assert "FIX THESE SPECIFIC PROBLEMS" in calls[1]
        assert "does not actually engage" in calls[1]

    def test_no_facts_means_no_grounding_requirement_at_all(self):
        """A brand with a totally blank Playbook must behave exactly as
        before this existed — no grounding line, no forced retry."""
        concepts_chunk = [_concept(0)]
        calls = []

        async def fake(req):
            prompt = req["messages"][0]["content"] if isinstance(req, dict) else ""
            calls.append(prompt)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps([_valid_item(0)])
            ))])

        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=fake):
            asyncio.run(svc._generate_final_copy(
                brand={}, concepts_chunk=concepts_chunk, platforms=["instagram"],
                existing_assets_summary="", force=True,
            ))

        assert len(calls) == 1
        assert "MUST GROUND IN THIS REAL FACT" not in calls[0]
