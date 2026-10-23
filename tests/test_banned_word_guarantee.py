"""
brand_brief.enforce() now guarantees — with certainty, no model involved —
that no banned word ships, no matter what any AI rewrite attempt did.

Confirmed live (Oct 2026): after carousel generation was fixed to also check
CaptionValidatorService.BANNED_WORDS (not just the brand's own custom words),
a real retest on the admin account still shipped "effortlessly" in a carousel
slide body, and "innovative" in a caption — both are banned words. Root
cause: the retry mechanism only ever kept "whichever draft had fewer issues",
never guaranteed zero, and only retried once. enforce() is the one place
every generation path (single posts, thread tweets, carousel captions,
carousel slides) already runs last — this makes that stop being a maybe.
"""
from app.agents.social_media_manager.services import brand_brief


class TestBannedWordGuarantee:
    def test_the_exact_live_bug_text_comes_out_clean(self):
        text = ("Modern platforms like Uri Social help African businesses create stunning "
                "posts, automate replies, and track results—all in one place, effortlessly.")
        out = brand_brief.enforce(text, {})
        assert "effortless" not in out.lower()
        assert "simply" in out.lower()  # natural adverb, not a mangled leftover

    def test_the_exact_live_bug_caption_comes_out_clean(self):
        text = "Are you clinging to old marketing habits? Break free with Uri Social's innovative solutions."
        out = brand_brief.enforce(text, {})
        assert "innovative" not in out.lower()

    def test_every_universal_banned_word_is_guaranteed_gone(self):
        from app.agents.social_media_manager.services.caption_validator_service import CaptionValidatorService
        for word in CaptionValidatorService.BANNED_WORDS:
            text = f"Here is a sentence that uses the word {word} right in the middle of it."
            out = brand_brief.enforce(text, {})
            assert word.lower() not in out.lower(), f"{word!r} survived enforce()"

    def test_word_family_forms_are_also_caught(self):
        # "effortless" is banned — "effortlessly"/"effortlessness" are the same family.
        for form in ("effortless", "effortlessly", "effortlessness"):
            out = brand_brief.enforce(f"We made it {form} for them.", {})
            assert "effortless" not in out.lower()

    def test_brands_own_custom_words_are_also_guaranteed_gone(self):
        bc = {"words_to_avoid": "bananaword, zoop"}
        out = brand_brief.enforce("We offer a totally bananaword zoop experience, and it works great.", bc)
        assert "bananaword" not in out.lower()
        assert "zoop" not in out.lower()

    def test_text_with_nothing_banned_is_untouched(self):
        text = "We help small businesses post consistently on Instagram and Facebook."
        assert brand_brief.enforce(text, {}) == text

    def test_no_double_punctuation_artifacts_left_behind(self):
        out = brand_brief.enforce("We offer a bespoke, curated service.", {})
        assert ",," not in out
        assert ", ." not in out
        assert "  " not in out

    def test_case_is_matched_at_the_start_of_a_sentence(self):
        # A capitalised banned word at a sentence start must still be caught —
        # the detector lowercases first, but enforce() rewrites real mixed-case text.
        out = brand_brief.enforce("Effortless setup, every time.", {})
        assert "effortless" not in out.lower()


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
