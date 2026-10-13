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
        assert "Write in Yoruba." in build({"brand_name": "B", "languages": ["Yoruba"]})

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


class TestApprovalMode:
    """The Playbook page saved "Auto-publish"/"Review before publishing" and
    onboarding saved "auto"/"review"/"hybrid" — and nothing read either."""

    def _mode(self, value):
        from app.agents.social_media_manager.services.posting_preferences import approval_mode
        return approval_mode({"approval_workflow": value})

    def test_both_spellings_map_to_one_mode(self):
        assert self._mode("Auto-publish") == self._mode("auto") == "auto"
        assert self._mode("Review before publishing") == self._mode("review") == "review"
        assert self._mode("hybrid") == "hybrid"

    def test_unknown_or_unset_is_review(self):
        assert self._mode("") == "review"
        assert self._mode("something else") == "review"
        from app.agents.social_media_manager.services.posting_preferences import approval_mode
        assert approval_mode(None) == "review"

    def test_only_auto_and_hybrid_publish_recurring_posts(self):
        from app.agents.social_media_manager.services.posting_preferences import auto_publishes_recurring
        assert auto_publishes_recurring({"approval_workflow": "auto"})
        assert auto_publishes_recurring({"approval_workflow": "hybrid"})
        assert not auto_publishes_recurring({"approval_workflow": "review"})
        assert not auto_publishes_recurring({})


class TestCadence:
    def _interval(self, cadence, fallback="daily"):
        from app.agents.social_media_manager.services.posting_preferences import cadence_interval
        return cadence_interval({"posting_cadence": cadence}, fallback)

    def test_playbook_cadence_sets_auto_interval(self):
        from datetime import timedelta
        assert self._interval("Daily") == timedelta(days=1)
        assert self._interval("3–4x per week") == timedelta(days=2)
        assert self._interval("2–3x per week") == timedelta(days=3)
        assert self._interval("Weekly") == timedelta(weeks=1)
        assert self._interval("Bi-weekly") == timedelta(weeks=2)

    def test_no_cadence_keeps_auto_tab_setting(self):
        from datetime import timedelta
        assert self._interval("", "weekly") == timedelta(weeks=1)
        assert self._interval("", "daily") == timedelta(days=1)


class TestNextPostTime:
    """Preferred time-of-day slots, in the brand's local time (Lagos = UTC+1)."""

    def _next(self, profile, platform, now):
        from app.agents.social_media_manager.services.posting_preferences import next_post_time
        return next_post_time(profile, platform, now=now)

    def test_general_slot_same_day(self):
        from datetime import datetime
        profile = {"region": "Nigeria", "posting_time_mode": "general",
                   "posting_time_prefs": {"general": "Evening (4-8pm),"}}
        # 08:00 UTC = 09:00 Lagos -> 18:00 Lagos = 17:00 UTC
        assert self._next(profile, "instagram", datetime(2026, 10, 14, 8, 0)) == datetime(2026, 10, 14, 17, 0)

    def test_rolls_to_next_day_when_slot_passed(self):
        from datetime import datetime
        profile = {"region": "Nigeria", "posting_time_mode": "general",
                   "posting_time_prefs": {"general": "Late Morning (9am-12pm),"}}
        # 12:00 UTC = 13:00 Lagos, 10:00 slot gone -> tomorrow 10:00 Lagos = 09:00 UTC
        assert self._next(profile, "facebook", datetime(2026, 10, 14, 12, 0)) == datetime(2026, 10, 15, 9, 0)

    def test_per_platform_slot_wins(self):
        from datetime import datetime
        profile = {"region": "Nigeria", "posting_time_mode": "custom",
                   "posting_time_prefs": {"linkedin": "Early Morning (6-9am),", "general": "Night (8pm-12am),"}}
        assert self._next(profile, "linkedin", datetime(2026, 10, 14, 3, 0)) == datetime(2026, 10, 14, 6, 0)
        # platform without its own slot falls back to the general one (21:00 Lagos)
        assert self._next(profile, "facebook", datetime(2026, 10, 14, 3, 0)) == datetime(2026, 10, 14, 20, 0)

    def test_respects_minimum_lead_time(self):
        from datetime import datetime
        profile = {"region": "Nigeria", "posting_time_mode": "general",
                   "posting_time_prefs": {"general": "Evening (4-8pm),"}}
        # 16:45 UTC = 17:45 Lagos: 18:00 is only 15 min away -> tomorrow
        assert self._next(profile, "x", datetime(2026, 10, 14, 16, 45)) == datetime(2026, 10, 15, 17, 0)

    def test_ai_mode_uses_platform_defaults(self):
        from datetime import datetime
        # LinkedIn defaults 09:00 / 12:00 Lagos
        assert self._next({"region": "Nigeria"}, "linkedin", datetime(2026, 10, 14, 5, 0)) == datetime(2026, 10, 14, 8, 0)

    def test_region_sets_timezone(self):
        from app.agents.social_media_manager.services.posting_preferences import brand_timezone
        assert str(brand_timezone({"region": "United Kingdom"})) == "Europe/London"
        assert str(brand_timezone({"region": "West Africa"})) == "Africa/Lagos"
        assert str(brand_timezone({})) == "Africa/Lagos"


class TestAutoTabUsesPlaybook:
    def test_auto_run_schedules_when_playbook_says_so(self):
        import inspect
        from app.agents.social_media_manager.services.auto_content_service import AutoContentService

        source = inspect.getsource(AutoContentService.generate_for_user)
        assert "posting_preferences.auto_publishes_recurring(profile_data)" in source
        assert "posting_preferences.cadence_interval(profile_data, frequency)" in source

    def test_auto_schedule_goes_through_normal_approve_and_schedule(self):
        import inspect
        from app.agents.social_media_manager.services.auto_content_service import AutoContentService

        source = inspect.getsource(AutoContentService._auto_schedule)
        assert 'schedule_option="schedule"' in source


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
