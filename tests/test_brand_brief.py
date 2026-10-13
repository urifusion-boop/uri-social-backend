"""
The shared Brand Brief: one set of rules, voice, audience, offer and
"right now" lines for every generator, in one priority order.
"""
from datetime import datetime, timedelta

import pytest

from app.agents.social_media_manager.services import brand_brief as bb
from app.agents.social_media_manager.services.content_generation_service import ContentGenerationService

FULL = {
    "brand_name": "Bukka Express",
    "brand_voice": "warm, witty",
    "voice_sample": "Hot jollof. No stories.",
    "guardrails": {"avoid_topics": "politics", "banned_words": "cheap, hack", "emoji_usage": "no",
                   "max_hashtags": "3", "compliance_notes": "never promise delivery times"},
    "words_to_avoid": ["deal of the century"],
    "voice_profile": {"banned_words": ["synergy"]},
    "languages": ["English", "Pidgin"],
    "target_audience": "busy Lagos professionals",
    "customer_pain_points": ["no time to cook"],
    "business_description": "Home-style Nigerian food delivered",
    "unique_selling_proposition": "cooked fresh every morning",
    "price_range": "mid-range",
}


class TestHardRules:
    def test_all_sources_of_banned_words_in_one_rule(self):
        assert bb.banned_words(FULL) == ["synergy", "deal of the century", "cheap", "hack"]
        rules = bb.hard_rules(FULL)
        assert "NEVER use these words or phrases: synergy, deal of the century, cheap, hack." in rules

    def test_avoid_topics_compliance_language_emoji(self):
        rules = " ".join(bb.hard_rules(FULL))
        assert "NEVER mention or reference: politics." in rules
        assert "Compliance requirement: never promise delivery times." in rules
        assert "Write mainly in English and naturally mix in Pidgin" in rules
        assert "Use NO emojis" in rules

    def test_hashtag_limit_is_caption_only(self):
        assert not any("hashtags" in r for r in bb.hard_rules(FULL))
        assert any("maximum of 3 hashtags" in r for r in bb.hard_rules(FULL, captions=True))

    def test_language_can_be_left_to_the_reader(self):
        assert not any("Languages:" in r for r in bb.hard_rules(FULL, language=False))

    def test_empty_profile_has_no_rules(self):
        assert bb.hard_rules({}) == [] and bb.hard_rules(None) == []


class TestViolations:
    def test_finds_banned_words_and_phrases_any_case(self):
        assert bb.violations("A CHEAP lunch — the deal of the century!", FULL) == ["deal of the century", "cheap"]

    def test_whole_words_only(self):
        assert bb.violations("We love cheapskates... no: cheaper is fine", {"words_to_avoid": ["cheap"]}) == []

    def test_clean_text(self):
        assert bb.violations("Fresh jollof, every morning.", FULL) == []


class TestVoice:
    def test_platform_tone_wins_when_tones_differ(self):
        bc = {"brand_voice": "warm", "same_tone_everywhere": False, "platform_tones": {"linkedin": "professional"}}
        assert "professional" in bb.voice(bc, platform="linkedin")[0]
        assert "warm" in bb.voice(bc, platform="instagram")[0]

    def test_same_tone_everywhere_ignores_platform_tones(self):
        bc = {"brand_voice": "warm", "same_tone_everywhere": True, "platform_tones": {"linkedin": "professional"}}
        assert "warm" in bb.voice(bc, platform="linkedin")[0]


class TestAudienceAdaptation:
    def test_single_group_leans_in(self):
        lines = bb.audience_adaptation({"audience_age_range": "Gen Z (18-24)"})
        assert len(lines) == 1 and "Gen Z" in lines[0]
        assert "stay inside the brand voice and the hard rules" in lines[0]

    def test_several_groups_are_blended_not_first_wins(self):
        lines = bb.audience_adaptation({"audience_age_range": "Gen Z (18-24), Millennials (25-40)"})
        assert "spans Gen Z, Millennials" in lines[0]
        assert "don't write for only one group" in lines[0]

    def test_everyone_adds_nothing(self):
        assert bb.audience_adaptation({"audience_age_range": "Everyone"}) == []

    def test_custom_range_still_noted(self):
        assert "30-45" in bb.audience_adaptation({"audience_age_range": "30-45"})[0]


class TestPulse:
    NOW = datetime(2026, 10, 14)

    def _bc(self, days_old):
        return {"business_pulse": {"current_promotions": ["20% off party trays this week"]},
                "business_pulse_updated_at": self.NOW - timedelta(days=days_old)}

    def test_fresh_promo_is_used(self):
        lines = bb.pulse(self._bc(3), now=self.NOW)
        assert lines == ["Active promotions/offers: 20% off party trays this week."]

    def test_ageing_promo_carries_a_caution(self):
        lines = bb.pulse(self._bc(30), now=self.NOW)
        assert "may have ended" in lines[-1]

    def test_old_promo_is_dropped(self):
        assert bb.pulse(self._bc(60), now=self.NOW) == []

    def test_no_pulse(self):
        assert bb.pulse({}) == []


class TestBuild:
    def test_priority_order_and_sections(self):
        text = bb.build(FULL)
        assert text.startswith("BRAND BRIEF — Bukka Express")
        assert bb.PRIORITY_ORDER in text
        order = [text.index(h) for h in ("HARD RULES (never", "BRAND VOICE:", "AUDIENCE:", "THE BUSINESS:")]
        assert order == sorted(order)

    def test_only_requested_sections(self):
        text = bb.build(FULL, sections=("rules", "voice"))
        assert "HARD RULES (never" in text and "BRAND VOICE:" in text
        assert "AUDIENCE:" not in text and "THE BUSINESS:" not in text

    def test_nothing_to_say_is_empty(self):
        assert bb.build({}) == "" and bb.build(None) == ""
        assert bb.build({"brand_name": "X"}, sections=("rules",)) == ""


class TestPostsUseTheBrief:
    build = staticmethod(ContentGenerationService._build_brand_block)

    def test_hard_rules_come_first_with_priority_order(self):
        block = self.build(FULL)
        assert bb.PRIORITY_ORDER in block
        assert block.index("HARD RULES") < block.index("Brand name is")
        assert "Use a maximum of 3 hashtags." in block

    def test_gen_z_no_longer_overrides_everything(self):
        block = self.build({"brand_name": "B", "audience_age_range": "Gen Z (18-24), Millennials (25-40)"})
        assert "OVERRIDES EVERYTHING" not in block
        assert "spans Gen Z, Millennials" in block

    def test_current_promotions_reach_posts(self):
        block = self.build({"brand_name": "B", "business_pulse": {"current_promotions": ["20% off this week"]},
                            "business_pulse_updated_at": datetime.utcnow()})
        assert "20% off this week" in block

    def test_no_playbook_no_block(self):
        assert self.build({}) == "" and self.build(None) == ""


class TestAdsUseTheBrief:
    def test_ads_get_rules_offer_and_promos_but_not_the_audience_tier(self):
        from app.agents.jane_ads.creative import _brand_prompt_bits

        bits = _brand_prompt_bits({**FULL, "business_pulse": {"current_promotions": ["20% off this week"]},
                                   "business_pulse_updated_at": datetime.utcnow()})
        assert "HARD RULES (never" in bits
        assert "NEVER mention or reference: politics." in bits
        assert "cooked fresh every morning" in bits       # offer
        assert "20% off this week" in bits                # current promo
        # Who an ad targets is delivery context — it must not be fed in as copy guidance.
        assert "AUDIENCE:" not in bits and "Pain points" not in bits

    def test_no_playbook_is_unchanged(self):
        from app.agents.jane_ads.creative import _brand_prompt_bits

        assert _brand_prompt_bits({}) == ""

    def test_ad_copy_is_checked_for_banned_words(self):
        import inspect
        from app.agents.jane_ads import creative

        assert "brand_brief.violations(" in inspect.getsource(creative.write_ad_copy)


class TestInboxSoundsLikeTheBrand:
    PROFILE = {"brand_name": "Bukka", "derived_voice": "playful and warm",
               "guardrails": {"banned_words": "cheap", "emoji_usage": "no", "avoid_topics": "politics"},
               "languages": ["Yoruba"]}

    def test_style_has_voice_and_rules_but_not_brand_language(self):
        from app.agents.inbox.agent import brand_style

        section, banned = brand_style(self.PROFILE)
        assert "playful and warm" in section
        assert "NEVER mention or reference: politics." in section and "Use NO emojis" in section
        assert "Yoruba" not in section      # replies stay in the customer's own language
        assert "never overrides rules 1-6" in section
        assert banned == ["cheap"]

    def test_no_profile_no_style(self):
        from app.agents.inbox.agent import brand_style

        assert brand_style(None) == ("", [])
        assert brand_style({"brand_name": "X"}) == ("", [])

    def test_prompt_takes_the_style_section(self):
        from app.agents.inbox.agent import _DRAFT_SYSTEM

        text = _DRAFT_SYSTEM.format(channel="Instagram", public_note="", brand_style="7. Sound like this business.\n")
        assert "7. Sound like this business." in text
        assert "Use ONLY facts from the numbered SOURCES" in text

    def test_banned_word_in_draft_is_never_auto_sent(self):
        import inspect
        from app.agents.inbox import agent

        src = inspect.getsource(agent.decide_auto_reply)
        assert 'draft.get("banned_words")' in src


class TestCalendarPlanUsesRulesPillarsAndDates:
    def test_rules_block(self):
        from app.agents.content_calendar_v2.services.content_calendar_v2_service import _brand_rules_block

        block = _brand_rules_block({**FULL, "content_pillars": ["Behind the scenes", "Tips"],
                                    "key_dates": "Anniversary (2026-11-02)"})
        assert "NEVER mention or reference: politics." in block
        assert "CONTENT PILLARS: Behind the scenes, Tips." in block
        assert "Anniversary (2026-11-02)" in block

    def test_empty_profile_adds_nothing(self):
        from app.agents.content_calendar_v2.services.content_calendar_v2_service import _brand_rules_block

        assert _brand_rules_block({}) == ""


class TestVideoAndBlogUseTheBrief:
    def test_wired_in(self):
        import inspect
        from app.agents.social_media_manager.services import (
            blog_generation_service, video_creative_service, video_storyboard_service, voiceover_service,
        )

        for module in (video_creative_service, video_storyboard_service, voiceover_service, blog_generation_service):
            assert "brand_brief.build(" in inspect.getsource(module), module.__name__

    def test_voiceover_keeps_the_voices_language(self):
        import inspect
        from app.agents.social_media_manager.services import voiceover_service

        assert "language=False" in inspect.getsource(voiceover_service)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
