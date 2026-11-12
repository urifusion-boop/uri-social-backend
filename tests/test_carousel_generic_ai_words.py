"""
CarouselGenerationService now also rejects the universal generic-AI-word
list, not just a brand's own custom words_to_avoid.

Confirmed live (Oct 2026): a real carousel draft came back with a slide
headline reading "...To Seamless Content" — "seamless" was already in
CaptionValidatorService.BANNED_WORDS before this fix, but carousels never
consulted that list at all; _issues() only ever checked brand_brief.audit(),
which is scoped to the brand's own custom banned words (empty by default).
These tests mock the OpenAI call directly (CarouselGenerationService talks
to its own _client, not the shared AIService) so no network/AI cost is
needed to prove the retry-on-banned-word path actually fires.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.social_media_manager.services.carousel_generation_service import (
    CarouselGenerationService,
)

BAD = {
    "caption": "Check out our latest update.",
    "slides": [
        {"headline": "From Scattered Posts To Seamless Content", "body": "Automate your workflow."},
        {"headline": "See The Results", "body": "Engagement is up."},
    ],
}
GOOD = {
    "caption": "Check out our latest update.",
    "slides": [
        {"headline": "From Scattered Posts To Simple Content", "body": "Automate your workflow."},
        {"headline": "See The Results", "body": "Engagement is up."},
    ],
}


def _response(payload: dict):
    msg = type("Msg", (), {"content": json.dumps(payload)})
    choice = type("Choice", (), {"message": msg})
    return type("Resp", (), {"choices": [choice]})()


class TestGenericAiWordTriggersARewrite:
    @pytest.mark.asyncio
    async def test_a_banned_word_in_a_slide_headline_triggers_a_rewrite(self):
        replies = iter([_response(BAD), _response(GOOD)])

        async def fake_create(*args, **kwargs):
            return next(replies)

        with patch(
            "app.agents.social_media_manager.services.carousel_generation_service._client.chat.completions.create",
            new=AsyncMock(side_effect=fake_create),
        ) as mock_create:
            data = await CarouselGenerationService.generate(
                "A quick idea about marketing automation.", "instagram", {}, num_slides=2, force_num_slides=True,
            )

        assert mock_create.call_count == 2  # first attempt, then the correction
        headlines = [s["headline"] for s in data["slides"]]
        assert not any("seamless" in h.lower() for h in headlines)
        assert "Simple" in headlines[0]

    @pytest.mark.asyncio
    async def test_a_clean_carousel_costs_no_rewrite(self):
        async def fake_create(*args, **kwargs):
            return _response(GOOD)

        with patch(
            "app.agents.social_media_manager.services.carousel_generation_service._client.chat.completions.create",
            new=AsyncMock(side_effect=fake_create),
        ) as mock_create:
            data = await CarouselGenerationService.generate(
                "A quick idea about marketing automation.", "instagram", {}, num_slides=2, force_num_slides=True,
            )

        assert mock_create.call_count == 1
        assert data["playbook_issues"] == []

    @pytest.mark.asyncio
    async def test_a_banned_word_in_the_caption_is_also_caught(self):
        bad_caption = {**GOOD, "caption": "Unlock your best marketing results today."}
        replies = iter([_response(bad_caption), _response(GOOD)])

        async def fake_create(*args, **kwargs):
            return next(replies)

        with patch(
            "app.agents.social_media_manager.services.carousel_generation_service._client.chat.completions.create",
            new=AsyncMock(side_effect=fake_create),
        ) as mock_create:
            data = await CarouselGenerationService.generate(
                "A quick idea about marketing automation.", "instagram", {}, num_slides=2, force_num_slides=True,
            )

        assert mock_create.call_count == 2
        assert "unlock" not in data["caption"].lower()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
