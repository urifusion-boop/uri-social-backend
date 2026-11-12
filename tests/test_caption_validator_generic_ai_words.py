"""
CaptionValidatorService — generic-AI-word ban and optional word-count cap.

Live feedback (product owner, Oct 2026): calendar-generated captions still
used "unlock/transform/discover" even though content_calendar_v2's own PLAN
titles already avoided them, and calendar-sourced posts came out noticeably
wordier than the plan's own concise preview. Root cause (confirmed by
reading the code): the calendar's GENERIC_AI_WORDS ban only ever governed
plan titles; the real draft is written by this shared validator, which
didn't know about that list, and had no word-count check at all. Both are
fixed here — the banned words are merged in (benefiting every generation
path, not just the calendar), and max_words is optional so only a
calendar-sourced caption is held to a tighter length.
"""
from app.agents.social_media_manager.services.caption_validator_service import CaptionValidatorService


class TestGenericAiWordsBanned:
    def test_bare_unlock_is_flagged(self):
        result = CaptionValidatorService.validate_caption("Unlock your best skincare routine today.")
        assert any("unlock" in i for i in result["issues"])
        assert not result["passed"]

    def test_bare_transform_is_flagged(self):
        result = CaptionValidatorService.validate_caption("This will transform how you shop for shoes.")
        assert any("transform" in i for i in result["issues"])

    def test_bare_discover_is_flagged(self):
        result = CaptionValidatorService.validate_caption("Discover our new collection this week.")
        assert any("discover" in i for i in result["issues"])

    def test_journey_empower_unleash_are_flagged(self):
        for word, text in (
            ("journey", "Join us on this journey to better coffee."),
            ("empower", "We empower small business owners every day."),
            ("unleash", "Unleash your creativity with our new kit."),
        ):
            result = CaptionValidatorService.validate_caption(text)
            assert any(word in i for i in result["issues"]), f"{word!r} should be flagged"

    def test_a_clean_caption_with_none_of_the_banned_words_passes(self):
        result = CaptionValidatorService.validate_caption(
            "We just restocked the lace front wigs. Message us to grab one before they're gone.\n\nDM to order."
        )
        assert result["passed"]
        assert result["issues"] == []

    def test_fix_prompt_names_the_specific_banned_words_found(self):
        result = CaptionValidatorService.validate_caption("Unlock your potential and discover more every day.")
        fix_prompt = CaptionValidatorService.generate_fix_prompt(
            "Unlock your potential and discover more every day.", result, platform="instagram")
        assert "unlock" in fix_prompt.lower()
        assert "discover" in fix_prompt.lower()


class TestWordCountCap:
    def test_no_max_words_means_no_length_check_at_all(self):
        long_caption = " ".join(["word"] * 500)
        result = CaptionValidatorService.validate_caption(long_caption)
        assert not any("too_many_words" in i for i in result["issues"])

    def test_under_the_cap_passes(self):
        caption = " ".join(["word"] * 50)
        result = CaptionValidatorService.validate_caption(caption, max_words=70)
        assert not any("too_many_words" in i for i in result["issues"])

    def test_over_the_cap_is_flagged_with_the_real_count_and_target(self):
        caption = " ".join(["word"] * 90)
        result = CaptionValidatorService.validate_caption(caption, max_words=70)
        issue = next(i for i in result["issues"] if "too_many_words" in i)
        assert "90" in issue and "70" in issue
        assert not result["passed"]

    def test_fix_prompt_tells_the_model_the_real_target_length(self):
        caption = " ".join(["word"] * 90)
        result = CaptionValidatorService.validate_caption(caption, max_words=70)
        fix_prompt = CaptionValidatorService.generate_fix_prompt(caption, result, platform="linkedin")
        assert "70 words" in fix_prompt


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
