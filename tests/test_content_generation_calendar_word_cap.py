"""
ContentGenerationService — calendar-sourced drafts ask for a tighter word
count than a normal Create Content seed. Pure prompt-building tests (no AI
calls); the actual validation-triggers-a-rewrite behaviour is already
covered end-to-end by caption_validator_service's own tests plus the
retry loop already proven for banned words/playbook issues.
"""
from app.agents.social_media_manager.services.content_generation_service import ContentGenerationService


class TestCalendarSeedGetsATighterWordTarget:
    def test_calendar_seed_type_adds_the_word_cap_instruction(self):
        prompt = ContentGenerationService.build_caption_prompt(
            "A quick idea about lace front wigs.", "instagram", {}, seed_type="calendar_v2_idea")
        assert f"{ContentGenerationService.CALENDAR_MAX_WORDS} words or fewer" in prompt

    def test_plain_text_seed_type_does_not_add_the_word_cap(self):
        prompt = ContentGenerationService.build_caption_prompt(
            "A quick idea about lace front wigs.", "instagram", {}, seed_type="text")
        assert "words or fewer" not in prompt

    def test_default_seed_type_is_also_unaffected(self):
        # No seed_type passed at all — the normal Create Content tab's own call shape.
        prompt = ContentGenerationService.build_caption_prompt(
            "A quick idea about lace front wigs.", "instagram", {})
        assert "words or fewer" not in prompt


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
