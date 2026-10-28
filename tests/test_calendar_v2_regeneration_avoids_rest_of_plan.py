"""
Content Calendar V2 — regeneration must see the rest of the plan.

Confirmed live: a 30-day plan still showed items flagged "similar" even
after its one auto-regeneration pass was supposed to fix them. Root cause:
_regenerate_flagged_items called _generate_final_copy with ONLY the
flagged items as concepts_chunk — that function's prompt has a real
"no two titles share an opening word" rule, but it only applies WITHIN
whatever's in concepts_chunk. Regenerating in isolation like this
guarantees the flagged items differ from EACH OTHER, but gives the model
zero visibility into the plan's other ~29 untouched items, so the
"fixed" item can easily land right back on a title/hook/visual idea that
duplicates one of those.

Fix: _generate_final_copy now accepts an optional avoid_context list —
other, already-finalized items it must not duplicate — and
_regenerate_flagged_items passes the rest of the plan's (non-flagged)
items as that context.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


def _fake_response_for(n):
    import json

    async def fake(req):
        payload = [
            {
                "title": f"Fresh title {i}", "hook": f"Fresh hook {i}", "key_points": ["a", "b"],
                "description": "x", "caption_direction": "x", "keywords": ["a", "b"], "cta": "Shop now",
                "topic": "topic", "promised_business_outcome": "x", "content_pillar": "x",
                "customer_journey_stage": "awareness", "video_idea": {}, "holiday_reference": None,
                "exact_copy": {"headline": f"Fresh headline {i}", "caption": "x", "hashtags": []},
                "carousel": None, "creative_concept_name": "x", "central_visual_idea": "x",
                "design_style": "x", "layout_direction": "x", "visual_metaphor": "",
                "ai_image_prompt": "a photo. no text, no logos.", "required_assets": [],
                "designer_execution_notes": "x", "reasoning": "x", "primary_kpi": "engagement",
            }
            for i in range(n)
        ]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(payload)
        ))])
    return fake


def test_avoid_context_appears_in_final_copy_prompt_when_given():
    concepts_chunk = [_concept(5)]
    avoid_context = [{
        "title": "Already Used Title", "hook": "Already hook",
        "exact_copy": {"headline": "Already headline"},
        "creative_direction": {"central_visual_idea": "Already visual idea"},
    }]
    captured = {}

    async def fake(req):
        captured["prompt"] = req["messages"][0]["content"] if isinstance(req, dict) else ""
        return await _fake_response_for(1)(req)

    with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
         patch.object(svc.AIService, "chat_completion", side_effect=fake):
        asyncio.run(svc._generate_final_copy(
            brand={}, concepts_chunk=concepts_chunk, platforms=["instagram"],
            existing_assets_summary="", force=True, avoid_context=avoid_context,
        ))

    prompt = captured["prompt"]
    assert "ALREADY USED ELSEWHERE IN THIS SAME 30-DAY PLAN" in prompt
    # exact_copy.headline takes precedence over title when both are present
    # (it's the actual publish-ready headline — title is the internal field).
    assert "Already headline" in prompt
    assert "Already Used Title" not in prompt
    assert "Already hook" in prompt
    assert "Already visual idea" in prompt


def test_no_avoid_context_block_when_not_given():
    concepts_chunk = [_concept(5)]
    captured = {}

    async def fake(req):
        captured["prompt"] = req["messages"][0]["content"] if isinstance(req, dict) else ""
        return await _fake_response_for(1)(req)

    with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
         patch.object(svc.AIService, "chat_completion", side_effect=fake):
        asyncio.run(svc._generate_final_copy(
            brand={}, concepts_chunk=concepts_chunk, platforms=["instagram"],
            existing_assets_summary="", force=True,
        ))

    assert "ALREADY USED ELSEWHERE IN THIS SAME 30-DAY PLAN" not in captured["prompt"]


def test_regenerate_flagged_items_passes_the_rest_of_the_plan_as_avoid_context():
    items_by_index = {i: _concept(i, title=f"Title {i}", hook=f"Hook {i}") for i in range(3)}
    captured = {}

    async def fake_generate_final_copy(**kwargs):
        captured["avoid_context"] = kwargs.get("avoid_context")
        return [{**items_by_index[1], "title": "Regenerated Title"}]

    with patch.object(svc, "_generate_final_copy", side_effect=fake_generate_final_copy):
        asyncio.run(svc._regenerate_flagged_items(
            flagged_day_indices=[1], items_by_index=items_by_index,
            brand={}, platforms=["instagram"], existing_assets_summary="",
        ))

    avoid_context = captured["avoid_context"]
    assert avoid_context is not None
    avoided_day_indices = {item["day_index"] for item in avoid_context}
    assert avoided_day_indices == {0, 2}   # the rest of the plan, not the flagged item itself
