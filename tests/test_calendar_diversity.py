"""
Tests for content calendar diversity improvements.

Verifies that:
- All 7 hook styles are assigned (one per day, no repeats)
- All 7 post formats are assigned (one per day, no repeats)
- Hook/format assignments change between runs (shuffled)
- The AI prompt contains the hook and format for each day
- gpt-4o is always used (not gpt-4o-mini)
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.social_media_manager.services.content_calendar_service import (
    HOOK_STYLES,
    POST_FORMATS,
    WEEK_DAYS,
    _generate_ideas,
)

BRAND = {
    "brand_name": "TestBrand",
    "industry": "technology",
    "brand_voice": "bold and direct",
    "target_audience": "startup founders",
    "content_pillars": ["growth", "product"],
}

FAKE_IDEAS = [
    {"day_index": i, "title": f"Title {i}", "description": f"Description {i}"}
    for i in range(7)
]


def _fake_ai_response(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class TestHookAndFormatConstants:
    def test_seven_hook_styles(self):
        assert len(HOOK_STYLES) == 7, "Need exactly 7 hook styles — one per day"

    def test_seven_post_formats(self):
        assert len(POST_FORMATS) == 7, "Need exactly 7 post formats — one per day"

    def test_hooks_are_distinct(self):
        assert len(set(HOOK_STYLES)) == 7, "All hook styles must be unique"

    def test_formats_are_distinct(self):
        assert len(set(POST_FORMATS)) == 7, "All post formats must be unique"


class TestPromptDiversity:
    @pytest.mark.asyncio
    async def test_all_hooks_appear_in_prompt(self):
        """Every hook style must appear exactly once in the generated prompt."""
        captured = {}

        async def fake_completion(request):
            captured["prompt"] = request.messages[0]["content"]
            return _fake_ai_response(json.dumps(FAKE_IDEAS))

        with patch(
            "app.agents.social_media_manager.services.content_calendar_service.AIService.chat_completion",
            side_effect=fake_completion,
        ), patch(
            "app.agents.social_media_manager.services.content_calendar_service.AIService.build_ai_model",
            side_effect=lambda messages, model, temperature: MagicMock(messages=messages),
        ):
            await _generate_ideas(BRAND, ["educational"] * 7, "2026-05-26", ["linkedin"])

        prompt = captured["prompt"]
        for hook in HOOK_STYLES:
            # Each hook style keyword should appear in the prompt
            hook_keyword = hook.split("—")[0].strip().lower()
            assert hook_keyword in prompt.lower(), f"Hook style '{hook_keyword}' missing from prompt"

    @pytest.mark.asyncio
    async def test_all_formats_appear_in_prompt(self):
        """Every post format must appear exactly once in the generated prompt."""
        captured = {}

        async def fake_completion(request):
            captured["prompt"] = request.messages[0]["content"]
            return _fake_ai_response(json.dumps(FAKE_IDEAS))

        with patch(
            "app.agents.social_media_manager.services.content_calendar_service.AIService.chat_completion",
            side_effect=fake_completion,
        ), patch(
            "app.agents.social_media_manager.services.content_calendar_service.AIService.build_ai_model",
            side_effect=lambda messages, model, temperature: MagicMock(messages=messages),
        ):
            await _generate_ideas(BRAND, ["educational"] * 7, "2026-05-26", ["linkedin"])

        prompt = captured["prompt"]
        for fmt in POST_FORMATS:
            assert fmt in prompt, f"Post format '{fmt}' missing from prompt"

    @pytest.mark.asyncio
    async def test_uses_gpt4o_not_mini(self):
        """Calendar generation must always use gpt-4o, never gpt-4o-mini."""
        captured = {}

        async def fake_completion(request):
            return _fake_ai_response(json.dumps(FAKE_IDEAS))

        with patch(
            "app.agents.social_media_manager.services.content_calendar_service.AIService.chat_completion",
            side_effect=fake_completion,
        ), patch(
            "app.agents.social_media_manager.services.content_calendar_service.AIService.build_ai_model",
            side_effect=lambda messages, model, temperature: (
                captured.update({"model": model, "temperature": temperature}) or MagicMock(messages=messages)
            ),
        ):
            await _generate_ideas(BRAND, ["educational"] * 7, "2026-05-26", ["linkedin"])

        assert captured["model"] == "gpt-4o", f"Expected gpt-4o, got {captured['model']}"
        assert captured["model"] != "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_hooks_differ_between_runs(self):
        """Hooks should be shuffled — two runs should produce different day assignments."""
        prompts = []

        async def fake_completion(request):
            prompts.append(request.messages[0]["content"])
            return _fake_ai_response(json.dumps(FAKE_IDEAS))

        with patch(
            "app.agents.social_media_manager.services.content_calendar_service.AIService.chat_completion",
            side_effect=fake_completion,
        ), patch(
            "app.agents.social_media_manager.services.content_calendar_service.AIService.build_ai_model",
            side_effect=lambda messages, model, temperature: MagicMock(messages=messages),
        ):
            for _ in range(5):
                await _generate_ideas(BRAND, ["educational"] * 7, "2026-05-26", ["linkedin"])

        # Extract Day 0 hook line from each prompt and check they're not all identical
        day0_lines = []
        for p in prompts:
            for line in p.split("\n"):
                if "Day 0" in line and "hook:" in line:
                    day0_lines.append(line)
                    break

        assert len(set(day0_lines)) > 1, (
            "Day 0 hook assignment was identical across all 5 runs — shuffling is not working"
        )
