"""
Brand Playbook settings that were saved but never reached Jane.

Each class covers one gap found when tracing the Playbook through the code:
fields the user can set that had no effect on what Jane produces.
"""
import pytest

from app.agents.social_media_manager.services.caption_validator_service import CaptionValidatorService
from app.agents.social_media_manager.services.content_generation_service import ContentGenerationService

build = ContentGenerationService._build_brand_block


class TestCaptionsUseFullCustomerPsychology:
    """Words to avoid and the deeper customer fields used to reach only the
    30-day Calendar — Create Post, Upload Content and Auto captions ignored them."""

    def test_words_to_avoid_are_forbidden(self):
        block = build({"brand_name": "Bukka", "words_to_avoid": ["cheap", "deal of the century"]})
        assert "NEVER use these words or phrases: cheap, deal of the century" in block

    def test_deeper_psychology_fields_reach_captions(self):
        block = build({
            "brand_name": "Bukka",
            "customer_desires": ["eat well without cooking"],
            "customer_fears": ["food poisoning"],
            "customer_hesitations": ["delivery takes too long"],
            "common_questions": ["Do you deliver to Lekki?"],
            "buying_triggers": ["payday"],
        })
        for text in ("eat well without cooking", "food poisoning", "delivery takes too long",
                     "Do you deliver to Lekki?", "payday"):
            assert text in block

    def test_psychology_lists_are_capped_at_three(self):
        block = build({"brand_name": "B", "customer_fears": ["a1", "a2", "a3", "a4"]})
        assert "a3" in block and "a4" not in block

    def test_empty_profile_adds_nothing(self):
        assert "More about the customer" not in build({"brand_name": "B"})


class TestBannedWordsEnforcedAfterWriting:
    def test_collects_every_source_once(self):
        words = ContentGenerationService._brand_banned_words({
            "voice_profile": {"banned_words": ["synergy"]},
            "words_to_avoid": ["cheap", "Synergy"],
            "guardrails": {"banned_words": "hack, guru\nhustle"},
        })
        assert words == ["synergy", "cheap", "hack", "guru", "hustle"]

    def test_no_context_no_words(self):
        assert ContentGenerationService._brand_banned_words(None) == []

    def test_custom_words_match_whole_words_only(self):
        result = CaptionValidatorService.validate_caption("Book your table today.", ["ok"])
        assert not any("banned_word: 'ok'" in i for i in result["issues"])

    def test_custom_word_is_flagged(self):
        result = CaptionValidatorService.validate_caption("Grab a cheap lunch today.", ["cheap"])
        assert any("banned_word: 'cheap'" in i for i in result["issues"])

    def test_custom_phrase_is_flagged(self):
        result = CaptionValidatorService.validate_caption("The deal of the century is here.", ["Deal of the century"])
        assert any("banned_word: 'deal of the century'" in i for i in result["issues"])


class TestLanguages:
    def test_english_only_adds_nothing(self):
        assert "Languages" not in build({"brand_name": "B", "languages": ["English"]})

    def test_single_language_is_written_in(self):
        assert "Write this post in Yoruba." in build({"brand_name": "B", "languages": ["Yoruba"]})

    def test_first_language_leads_others_mixed_in(self):
        block = build({"brand_name": "B", "languages": ["English", "Pidgin"]})
        assert "Write mainly in English and naturally mix in Pidgin" in block


class TestCarouselsGetFullPlaybook:
    def test_carousel_prompt_uses_full_brand_block(self):
        import inspect
        from app.agents.social_media_manager.services.carousel_generation_service import CarouselGenerationService

        source = inspect.getsource(CarouselGenerationService.generate)
        assert "ContentGenerationService._build_brand_block(bc, platform=platform)" in source


class TestAdFormatRotation:
    """The Playbook's preferred ad formats used to always lead with the first
    one; a rotation index was saved but never read."""

    def _pick(self, *args):
        from app.agents.jane_ads.vsg01_orchestrator import pick_preferred_ad_format
        return pick_preferred_ad_format(*args)

    def test_takes_turns(self):
        assert self._pick(["a", "b", "c"], 0, {"a", "b", "c"}) == ("a", 1)
        assert self._pick(["a", "b", "c"], 1, {"a", "b", "c"}) == ("b", 2)
        assert self._pick(["a", "b", "c"], 2, {"a", "b", "c"}) == ("c", 0)

    def test_skips_ineligible_formats(self):
        assert self._pick(["a", "b", "c"], 1, {"a", "c"}) == ("c", 0)

    def test_none_eligible_falls_back_to_normal_ranking(self):
        assert self._pick(["a", "b"], 0, {"x"}) == (None, None)

    def test_no_preferences(self):
        assert self._pick([], 3, {"a"}) == (None, None)

    def test_stale_index_wraps(self):
        assert self._pick(["a", "b"], 7, {"a", "b"}) == ("b", 0)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
