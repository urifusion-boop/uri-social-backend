"""
Even with today's territory-allocation fix live, a real regenerate of
URI's 30-day plan stayed flat at ~25-26/30 flagged "similar." Pulling the
real flagged items and checking them against _llm_diversity_check's own
three criteria by hand showed the actual cause: not duplicate ideas, but
duplicate SENTENCES — 6+ items independently landing on the same "chaos ->
simple/smooth/effortless" payoff language, and 6 items sharing the same
boilerplate CTA template ("[Verb] how URI can [benefit] your business
today"). Both originate in _generate_final_copy, called as 5 fully
concurrent, mutually blind batches of 6 items each for the initial 30-item
generation — each batch independently reaches for the same safe phrasing
with no idea what the other 4 batches are writing.

The chosen fix deliberately avoids adding a sequential wave (which would
slow down generation, same tradeoff already paid once for candidate
generation) — instead it uses the same deterministic-pre-assignment trick
that already fixed territory clustering, applied twice more:

1. CTA style — each item is assigned one of 5 CTA styles by day_index, so
   the 5 concurrent batches can't all default to the same template, with
   zero added round-trips (pure prompt content, decided before any call).
2. Crutch words ("simplify", "effortless", "seamless", "hassle-free",
   "streamline", "smooth", "chaos") — hard-banned for all but ~1-in-5 items
   (by day_index), reusing _generate_final_copy's EXISTING words_to_avoid
   validation + correction-retry loop (built for brand.words_to_avoid) —
   no new mechanism, and the correction only costs a retry within the
   existing per-chunk budget, not a new sequential stage across chunks.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.content_calendar_v2.services import content_calendar_v2_service as svc


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


class TestCtaStyleAssignedPerItem:
    def test_different_day_indices_get_different_cta_styles_in_prompt(self):
        concepts_chunk = [_concept(0), _concept(1), _concept(2)]
        captured = {}

        async def fake(req):
            captured["prompt"] = req["messages"][0]["content"] if isinstance(req, dict) else ""
            payload = [_valid_item(i) for i in range(3)]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(payload)
            ))])

        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=fake):
            asyncio.run(svc._generate_final_copy(
                brand={}, concepts_chunk=concepts_chunk, platforms=["instagram"],
                existing_assets_summary="", force=True,
            ))

        prompt = captured["prompt"]
        assert "CTA style (use this, not a generic template)" in prompt
        style0 = svc._CTA_STYLE_VARIANTS[0 % len(svc._CTA_STYLE_VARIANTS)]
        style1 = svc._CTA_STYLE_VARIANTS[1 % len(svc._CTA_STYLE_VARIANTS)]
        style2 = svc._CTA_STYLE_VARIANTS[2 % len(svc._CTA_STYLE_VARIANTS)]
        assert style0 in prompt and style1 in prompt and style2 in prompt
        assert style0 != style1 != style2

    def test_cta_style_wraps_around_for_day_index_beyond_variant_count(self):
        n = len(svc._CTA_STYLE_VARIANTS)
        concepts_chunk = [_concept(n)]  # should wrap back to index 0's style
        captured = {}

        async def fake(req):
            captured["prompt"] = req["messages"][0]["content"] if isinstance(req, dict) else ""
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps([_valid_item(0)])
            ))])

        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=fake):
            asyncio.run(svc._generate_final_copy(
                brand={}, concepts_chunk=concepts_chunk, platforms=["instagram"],
                existing_assets_summary="", force=True,
            ))

        assert svc._CTA_STYLE_VARIANTS[0] in captured["prompt"]


class TestCrutchWordAllowance:
    def test_disallowed_item_with_crutch_word_triggers_a_correction_retry(self):
        """day_index=1 -> 1 % 5 != 0 -> crutch words banned for this item."""
        concepts_chunk = [_concept(1)]
        calls = []

        async def fake(req):
            prompt = req["messages"][0]["content"] if isinstance(req, dict) else ""
            calls.append(prompt)
            if len(calls) == 1:
                payload = [_valid_item(0, caption="URI will simplify your whole workflow.")]
            else:
                payload = [_valid_item(0)]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(payload)
            ))])

        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=fake):
            asyncio.run(svc._generate_final_copy(
                brand={}, concepts_chunk=concepts_chunk, platforms=["instagram"],
                existing_assets_summary="", force=True,
            ))

        assert len(calls) == 2, "a crutch word on a disallowed item should force a second attempt"
        correction_prompt = calls[1]
        assert "FIX THESE SPECIFIC PROBLEMS" in correction_prompt
        assert "simplify" in correction_prompt.lower()

    def test_allowed_item_with_crutch_word_does_not_trigger_a_retry(self):
        """day_index=0 -> 0 % 5 == 0 -> this item may use crutch words."""
        concepts_chunk = [_concept(0)]
        calls = []

        async def fake(req):
            prompt = req["messages"][0]["content"] if isinstance(req, dict) else ""
            calls.append(prompt)
            payload = [_valid_item(0, caption="URI will simplify your whole workflow.")]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(payload)
            ))])

        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=fake):
            result = asyncio.run(svc._generate_final_copy(
                brand={}, concepts_chunk=concepts_chunk, platforms=["instagram"],
                existing_assets_summary="", force=True,
            ))

        assert len(calls) == 1, "an allowed item's crutch word must not force a retry"
        assert result[0]["exact_copy"]["caption"] == "URI will simplify your whole workflow."

    def test_crutch_word_allowance_is_deterministic_by_day_index(self):
        allowed = [i for i in range(10) if i % svc._CRUTCH_WORD_ALLOWED_EVERY == 0]
        assert allowed == [0, 5]

    def test_brand_words_to_avoid_still_applies_regardless_of_allowance(self):
        """The brand's own hard words_to_avoid must still be enforced on an
        'allowed' item — crutch-word allowance only widens what's permitted
        for THIS mechanism, it never weakens the brand's own rule."""
        concepts_chunk = [_concept(0)]  # allowed for crutch words
        calls = []

        async def fake(req):
            prompt = req["messages"][0]["content"] if isinstance(req, dict) else ""
            calls.append(prompt)
            if len(calls) == 1:
                payload = [_valid_item(0, caption="This is guaranteed to work every time.")]
            else:
                payload = [_valid_item(0)]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(payload)
            ))])

        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=fake):
            asyncio.run(svc._generate_final_copy(
                brand={"words_to_avoid": ["guaranteed"]}, concepts_chunk=concepts_chunk,
                platforms=["instagram"], existing_assets_summary="", force=True,
            ))

        assert len(calls) == 2, "the brand's own banned word must still force a retry"
