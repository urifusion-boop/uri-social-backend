"""After a caption is written it is checked against the Brand Playbook, rewritten
if a rule is broken, and the rules that can be guaranteed are applied.

Every case here is something real output on dev got wrong with the rule already
in the prompt: an invented statistic, no call-to-action, no Pidgin, a banned
word in another form, hashtags lost by the rewrite, paragraphs flattened.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.agents.social_media_manager.services import brand_brief as bb
from app.agents.social_media_manager.services import content_generation_service as cgs
from app.agents.social_media_manager.services.caption_validator_service import CaptionValidatorService
from app.agents.social_media_manager.services.content_generation_service import ContentGenerationService

BRAND = {
    "brand_name": "Docerity",
    "business_description": "Document automation for small businesses. Set up in ten minutes.",
    "languages": ["English", "Pidgin"],
    "words_to_avoid": ["streamline"],
    "guardrails": {"banned_words": "synergy", "emoji_usage": "no", "max_hashtags": "3"},
    "cta_styles": ["Book a free demo"],
}


def codes(issues):
    return [i["code"] for i in issues]


class TestCallToAction:
    def test_exact_phrase(self):
        assert bb.has_cta("Paperwork is hard.\n\nBook a free demo.", BRAND)

    def test_same_words_in_the_closing_line(self):
        assert bb.has_cta("Paperwork is hard.\n\nBook your free demo today!", BRAND)

    def test_missing(self):
        assert not bb.has_cta("Paperwork is hard.\n\nWhat challenges do you face?", BRAND)

    def test_words_scattered_earlier_do_not_count(self):
        text = "We demo it free. Book early.\n\nSecond paragraph here.\n\nThird one.\n\nWhat do you think?"
        assert not bb.has_cta(text, BRAND)

    def test_no_cta_set_means_nothing_to_miss(self):
        assert bb.has_cta("Anything", {})

    def test_one_off_override_wins(self):
        bc = {**BRAND, "override_cta": "Call 0800 123"}
        assert bb.ctas(bc) == ["Call 0800 123"]
        assert not bb.has_cta("Book a free demo.", bc)

    def test_any_of_several_ctas_is_enough(self):
        assert bb.has_cta("Hello.\n\nSend us a DM.", {"cta_styles": ["Book a free demo", "Send us a DM"]})


class TestLanguages:
    def test_plain_english_is_missing_pidgin(self):
        assert bb.missing_languages("We don't stop. Book a demo now.", BRAND) == ["Pidgin"]

    def test_pidgin_present(self):
        assert bb.missing_languages("Paperwork no be your work. We dey for you.", BRAND) == []

    def test_dont_is_not_pidgin(self):
        assert bb.missing_languages("Don't wait.", BRAND) == ["Pidgin"]

    def test_single_language_is_not_checked(self):
        assert bb.missing_languages("Hello", {"languages": ["English"]}) == []

    def test_unknown_language_is_not_guessed(self):
        assert bb.missing_languages("Hello", {"languages": ["English", "French"]}) == []

    def test_other_nigerian_languages(self):
        assert bb.missing_languages("Ẹ kú iṣẹ́, we are open.", {"languages": ["English", "Yoruba"]}) == []
        assert bb.missing_languages("Ndewo, we are open.", {"languages": ["English", "Igbo"]}) == []
        assert bb.missing_languages("Sannu, we are open.", {"languages": ["English", "Hausa"]}) == []
        assert bb.missing_languages("We are open.", {"languages": ["English", "Hausa"]}) == ["Hausa"]


class TestLanguageBlend:
    MOSTLY_PIDGIN = ("Docerity don make am easier for you.\n\nWe fit read invoices wey dem send for WhatsApp.\n\n"
                     "You go save time and e go reduce errors.\n\nWetin be the challenges wey you dey face?\n\n"
                     "Book a free demo.")
    LIGHT_BLEND = ("Are your invoices stuck in WhatsApp?\n\nDocerity reads invoices sent as photos.\n\n"
                   "No wahala, it takes ten minutes to set up.\n\nYour team gets hours back every week.\n\n"
                   "Book a free demo.")

    def test_a_piece_taken_over_by_the_second_language(self):
        assert bb.overused_languages(self.MOSTLY_PIDGIN, BRAND) == ["Pidgin"]
        assert "language_overused" in codes(bb.audit(self.MOSTLY_PIDGIN, BRAND))

    def test_a_light_blend_is_right(self):
        assert bb.overused_languages(self.LIGHT_BLEND, BRAND) == []
        assert bb.audit(self.LIGHT_BLEND, BRAND) == []

    def test_short_pieces_are_not_judged(self):
        assert bb.overused_languages("We dey for you. Book a free demo.", BRAND) == []

    def test_rule_states_both_ends(self):
        rule = bb.languages_rule(BRAND)
        assert "REQUIRED" in rule and "LIMIT" in rule and "main language" in rule

    def test_single_language_brand_is_never_over(self):
        assert bb.overused_languages(self.MOSTLY_PIDGIN, {"languages": ["Pidgin"]}) == []


class TestInventedNumbers:
    def test_statistic_not_in_the_sources(self):
        assert bb.unverified_numbers("Over 70% of small businesses struggle.", "Docerity, ten minutes") == ["70%"]

    def test_number_that_was_given_is_fine(self):
        assert bb.unverified_numbers("Get 20% off this week.", "Promo: 20% off until Friday") == []

    def test_ratios_and_multipliers(self):
        found = bb.unverified_numbers("3 in 5 teams lose invoices. Work 10x faster.", "")
        assert found == ["3 in 5", "10x"]

    def test_ordinary_numbers_are_left_alone(self):
        text = "Set up in 10 minutes. Open 24/7. Founded 2019. (1/4)"
        assert bb.unverified_numbers(text, "") == []

    def test_hundred_percent_is_an_expression(self):
        assert bb.unverified_numbers("100% Nigerian owned.", "") == []


class TestInventedSources:
    def test_a_study_nobody_provided(self):
        text = "Renewals get missed. A study by the International Finance Corporation highlighted this. Fix it."
        assert bb.unverified_sources(text, "Docerity") == [
            "A study by the International Finance Corporation highlighted this."]

    def test_other_ways_of_citing(self):
        for line in ("According to McKinsey, paperwork is slow.", "Research shows teams lose hours.",
                     "Experts agree it matters.", "Recent surveys suggest otherwise.", "Studies show it works."):
            assert bb.unverified_sources(line, ""), line

    def test_a_source_that_was_given_is_fine(self):
        text = "According to our 2025 customer survey, setup takes ten minutes."
        assert bb.unverified_sources(text, "Facts: according to our 2025 customer survey, setup is quick") == []

    def test_ordinary_sentences_are_left_alone(self):
        for line in ("Send us a report of your invoices.", "We study your workflow first.",
                     "Your data stays safe.", "Book a free demo to see the reports."):
            assert bb.unverified_sources(line, "") == [], line

    def test_audit_flags_it_with_a_fix(self):
        text = "No wahala. Research shows SMEs lose contracts.\n\nBook a free demo."
        issues = bb.audit(text, BRAND, sources="Docerity")
        assert codes(issues) == ["unverified_source"] and "Remove the citation" in issues[0]["fix"]

    def test_dont_is_not_pidgin(self):
        assert bb.missing_languages("I don't know. We don't stop.", BRAND) == ["Pidgin"]


class TestBlogLabels:
    def test_echoed_label_is_not_part_of_the_post(self):
        from app.agents.social_media_manager.services.blog_generation_service import _parse_blog_output
        parsed = _parse_blog_output("TITLE: T\nMETA: M\nBLOG:  \n\nFirst paragraph.\n\nSecond.")
        assert parsed == {"title": "T", "meta": "M", "body": "First paragraph.\n\nSecond."}


class TestAudit:
    def test_everything_wrong_at_once(self):
        text = "Streamlining operations matters. Over 70% of SMEs struggle.\n\nWhat do you think?"
        issues = bb.audit(text, BRAND, sources="Docerity")
        assert codes(issues) == ["banned_word", "unverified_number", "missing_language", "missing_cta"]
        assert all(i["fix"] for i in issues)

    def test_clean_piece(self):
        text = "Paperwork no be your work again. Docerity dey handle am.\n\nBook a free demo."
        assert bb.audit(text, BRAND, sources="Docerity") == []

    def test_brand_with_no_rules(self):
        assert bb.audit("Anything at all, 70% made up or not.", {}, sources="70") == []


class TestEnforce:
    def test_removes_every_emoji_when_the_brand_says_none(self):
        out = bb.enforce("Big news 🎉 today 🔥 !\n\nBook a free demo ✅", BRAND)
        assert out == "Big news today!\n\nBook a free demo"

    def test_keeps_emojis_when_allowed(self):
        bc = {**BRAND, "guardrails": {"emoji_usage": "yes"}}
        assert "🎉" in bb.enforce("Big news 🎉\n\nBook a free demo", bc)

    def test_appends_the_cta_when_missing(self):
        out = bb.enforce("Paperwork is hard.\n\nWhat do you think?", BRAND)
        assert out.endswith("What do you think?\n\nBook a free demo.")

    def test_does_not_double_the_cta(self):
        text = "Paperwork is hard.\n\nBook your free demo today."
        assert bb.enforce(text, BRAND) == text

    def test_thread_is_not_given_an_extra_line(self):
        assert bb.enforce("A tweet with no CTA", BRAND, append_cta=False) == "A tweet with no CTA"

    def test_empty_stays_empty(self):
        assert bb.enforce("", BRAND) == ""

    def test_hashtag_limit(self):
        assert bb.limit_hashtags(["a", "b", "c", "d", "e"], BRAND) == ["a", "b", "c"]
        assert bb.limit_hashtags(["a", "b", "c", "d"], {"guardrails": {"max_hashtags": "No limit"}}) == ["a", "b", "c", "d"]
        assert bb.limit_hashtags(None, BRAND) == []


class TestCleaning:
    def test_line_breaks_survive(self):
        content, tags = ContentGenerationService._extract_and_clean_hashtags(
            "First  line here.\n\nSecond line.\n.\n.\n\n\n#One #Two", "instagram")
        assert content == "First line here.\n\nSecond line."
        assert tags == ["One", "Two"]

    def test_thread_in_a_code_fence(self):
        raw = '```json\n["First tweet (1/2)", "Second tweet (2/2)"]\n```'
        out = ContentGenerationService._post_process_content(raw, "twitter")
        assert out["tweets"] == ["First tweet (1/2)", "Second tweet (2/2)"]
        assert out["is_thread"] is True

    def test_thread_numbering_is_not_a_style_fault(self):
        text = ContentGenerationService._for_validation("Short and clear (1/4)", "twitter")
        assert "slash_construction" not in CaptionValidatorService.validate_caption(text)["issues"]


class TestFixPrompt:
    def test_carries_the_playbook_fixes_and_brand_rules(self):
        issues = bb.audit("Streamlining matters.\n\nThoughts?", BRAND, sources="")
        prompt = CaptionValidatorService.generate_fix_prompt(
            "Streamlining matters.\n\nThoughts?", {"passed": True, "issues": []},
            playbook_issues=issues, brand_rules=bb.hard_rules(BRAND, captions=True), platform="linkedin")
        assert 'Remove "streamline"' in prompt
        assert 'final line: "Book a free demo"' in prompt
        assert "THIS BRAND'S RULES STILL APPLY" in prompt and "Pidgin" in prompt
        assert "Do not add facts, numbers or statistics" in prompt

    def test_nothing_wrong_means_no_prompt(self):
        assert CaptionValidatorService.generate_fix_prompt("ok", {"passed": True, "issues": []}) == ""

    def test_thread_layout_rule(self):
        prompt = CaptionValidatorService.generate_fix_prompt(
            "x", {"passed": False, "issues": ["contains_semicolon"]}, platform="twitter")
        assert "keep each tweet as its own paragraph" in prompt
        assert "phone screen" not in prompt


GOOD = ("Paperwork no be your work again.\n\nDocerity turns invoices into clean data for you.\n\n"
        "Set up takes ten minutes.\n\nYour team gets hours back.\n\nBook a free demo.\n\n#Docerity #SME #Naija #Extra #More")
BAD = ("Streamlining your paperwork matters 🎉\n\nOver 70% of small businesses struggle with invoices.\n\n"
       "Docerity turns them into clean data.\n\nWhat do you think?\n\n#Docerity #SME #Naija #Extra #More")


def _reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class TestGenerationPipeline:
    def _run(self, monkeypatch, replies, platform="linkedin", brand=BRAND):
        prompts = []

        async def fake_chat(request):
            prompts.append(request.messages[0]["content"] if hasattr(request, "messages") else str(request))
            return _reply(replies[min(len(prompts) - 1, len(replies) - 1)])

        monkeypatch.setattr(cgs.AIService, "chat_completion", staticmethod(fake_chat))
        monkeypatch.setattr(cgs.AIService, "build_ai_model",
                            staticmethod(lambda messages, **kw: SimpleNamespace(messages=messages)))
        result = asyncio.run(ContentGenerationService._generate_platform_content(
            platform, "Introduce Docerity", "req1", "user1", brand_context=brand))
        assert result["status"], result
        return result["responseData"], prompts

    def test_broken_caption_is_rewritten_with_the_brand_rules(self, monkeypatch):
        data, prompts = self._run(monkeypatch, [BAD, GOOD])
        assert len(prompts) == 2
        fix = prompts[1]
        assert 'Remove "streamline"' in fix and '"70%"' in fix and "Pidgin" in fix and "Book a free demo" in fix
        assert data["content"].endswith("Book a free demo.")
        assert data["ai_metadata"]["playbook_issues"] == []
        assert data["ai_metadata"]["rewrites"] == 1

    def test_hashtags_are_capped_at_the_playbook_limit(self, monkeypatch):
        data, _ = self._run(monkeypatch, [GOOD])
        assert data["hashtags"] == ["Docerity", "SME", "Naija"]

    def test_clean_caption_costs_no_rewrite(self, monkeypatch):
        data, prompts = self._run(monkeypatch, [GOOD])
        assert len(prompts) == 1 and data["ai_metadata"]["rewrites"] == 0
        assert "\n\n" in data["content"]

    def test_rewrite_that_drops_hashtags_keeps_the_originals(self, monkeypatch):
        no_tags = GOOD.split("\n\n#")[0]
        data, _ = self._run(monkeypatch, [BAD, no_tags])
        assert data["hashtags"] == ["Docerity", "SME", "Naija"]

    def test_model_that_never_complies_still_gets_the_guarantees(self, monkeypatch):
        data, prompts = self._run(monkeypatch, [BAD])
        assert len(prompts) == 3  # first draft + two rewrites, then stop
        assert "🎉" not in data["content"]
        assert data["content"].endswith("Book a free demo.")
        assert len(data["hashtags"]) == 3
        remaining = data["ai_metadata"]["playbook_issues"]
        assert any(r.startswith("banned_word") for r in remaining)
        assert any(r.startswith("unverified_number") for r in remaining)
        assert not any(r.startswith("missing_cta") for r in remaining)

    def test_rewrite_that_makes_it_worse_is_thrown_away(self, monkeypatch):
        only_cta_missing = GOOD.replace("Book a free demo.\n\n", "")
        data, _ = self._run(monkeypatch, [only_cta_missing, BAD])
        assert "streamlin" not in data["content"].lower()
        assert data["content"].endswith("Book a free demo.")

    def test_prompt_states_todays_date(self, monkeypatch):
        _, prompts = self._run(monkeypatch, [GOOD])
        assert "Today's date is" in prompts[0]

    def test_thread_keeps_its_tweets_and_gets_no_extra_line(self, monkeypatch):
        thread = '```json\n["Paperwork no be your work again 🎉 (1/2)", "Book a free demo today. (2/2)"]\n```'
        data, _ = self._run(monkeypatch, [thread], platform="twitter")
        assert data["tweets"] == ["Paperwork no be your work again (1/2)", "Book a free demo today. (2/2)"]
        assert data["is_twitter_thread"] is True

    def test_brand_without_rules_behaves_as_before(self, monkeypatch):
        data, prompts = self._run(monkeypatch, [GOOD], brand={"brand_name": "Plain"})
        assert len(prompts) == 1
        assert len(data["hashtags"]) == 5


class TestRegenerateKeepsThePlaybook:
    def test_brand_context_reaches_the_writer(self, monkeypatch):
        seen = {}

        async def fake_generate(platform, seed_content, request_id, user_id, brand_context=None):
            seen["brand_context"] = brand_context
            return {"status": True, "responseData": {}}

        monkeypatch.setattr(ContentGenerationService, "_generate_platform_content", staticmethod(fake_generate))
        asyncio.run(ContentGenerationService.regenerate_content(
            "d1", "u1", feedback="shorter", platform="linkedin", original_seed="seed", brand_context=BRAND))
        assert seen["brand_context"] is BRAND


class TestCalendarUsesTheSameBannedWordCheck:
    def test_other_forms_and_the_guardrail_list(self):
        from app.agents.content_calendar_v2.services import content_calendar_v2_service as cal
        words = cal._all_banned_words(BRAND)
        assert set(words) == {"streamline", "synergy"}
        idea = {"exact_copy": {"caption": "Streamlining your week with real synergies.", "headline": "Book it"}}
        assert set(cal._find_banned_words(idea, words)) == {"streamline", "synergy"}

    def test_short_word_does_not_flag_a_longer_one(self):
        from app.agents.content_calendar_v2.services import content_calendar_v2_service as cal
        assert cal._find_banned_words({"exact_copy": {"caption": "Book a table"}}, ["ok"]) == []


class TestRegenerateEndToEnd:
    """Regenerate used to fail on the unique (request_id, platform) index and
    write with no Playbook. It now rewrites the caller's own draft in place."""

    def _db(self):
        from mongomock_motor import AsyncMongoMockClient
        db = AsyncMongoMockClient()["t"]

        async def seed():
            await db["content_drafts"].create_index([("request_id", 1), ("platform", 1)], unique=True)
            await db["content_drafts"].insert_one({
                "id": "d1", "request_id": "r1", "platform": "linkedin", "user_id": "u1", "brand_id": "b1",
                "content": "Old caption", "hashtags": ["old"], "image_url": "https://img/1.png"})
            await db["content_requests"].insert_one({"id": "r1", "user_id": "u1", "seed_content": "Seed", "retry_count": 0})
            await db["brand_profiles"].insert_one({"brand_id": "b1", "user_id": "u1", **BRAND})
        asyncio.run(seed())
        return db

    def _regenerate(self, monkeypatch, db, user="u1"):
        from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService
        seen = {}

        async def fake_generate(platform, seed_content, request_id, user_id, brand_context=None):
            seen["brand_context"] = brand_context
            return {"status": True, "responseData": {"content": "New caption", "hashtags": ["new"], "ai_metadata": {"rewrites": 0}}}

        monkeypatch.setattr(ContentGenerationService, "_generate_platform_content", staticmethod(fake_generate))
        result = asyncio.run(ApprovalWorkflowService.regenerate_content(db=db, user_id=user, draft_id="d1",
                                                                        regeneration_feedback="shorter"))
        return result, seen

    def test_rewrites_in_place_with_the_playbook(self, monkeypatch):
        db = self._db()
        result, seen = self._regenerate(monkeypatch, db)
        assert result["status"], result
        assert seen["brand_context"]["cta_styles"] == ["Book a free demo"]
        drafts = asyncio.run(db["content_drafts"].find({}).to_list(10))
        assert len(drafts) == 1
        d = drafts[0]
        assert d["content"] == "New caption" and d["hashtags"] == ["new"]
        assert d["image_url"] == "https://img/1.png" and d["user_id"] == "u1"
        assert d["previous_versions"][0]["content"] == "Old caption"
        assert result["responseData"]["new_draft_id"] == "d1"
        assert asyncio.run(db["content_requests"].find_one({"id": "r1"}))["retry_count"] == 1

    def test_someone_elses_draft_is_not_found(self, monkeypatch):
        db = self._db()
        result, seen = self._regenerate(monkeypatch, db, user="intruder")
        assert not result["status"] and seen == {}
        assert asyncio.run(db["content_drafts"].find_one({"id": "d1"}))["content"] == "Old caption"


BLOG_HEAD = "TITLE: Stop Missing Renewals\nMETA: A guide.\n\n"
BLOG_BAD = (BLOG_HEAD + "No wahala. " + "Docerity helps you every day. " * 30
            + "SMEs make up 96% of businesses. Streamlined work matters.")
BLOG_GOOD = BLOG_HEAD + "No wahala. " + "Docerity helps you every day. " * 32


class TestBlogIsCheckedToo:
    def _run(self, monkeypatch, replies):
        from mongomock_motor import AsyncMongoMockClient
        from app.agents.social_media_manager.services import blog_generation_service as blog
        db = AsyncMongoMockClient()["t"]
        prompts = []

        async def fake_chat(request):
            prompts.append(request.messages[0]["content"])
            return _reply(replies[min(len(prompts) - 1, len(replies) - 1)])

        async def no_dna(*a, **k):
            return ""

        monkeypatch.setattr(blog.AIService, "chat_completion", staticmethod(fake_chat))
        monkeypatch.setattr(blog.AIService, "build_ai_model",
                            staticmethod(lambda messages, **kw: SimpleNamespace(messages=messages)))
        monkeypatch.setattr(blog.WritingDNAService, "get_prompt", staticmethod(no_dna))

        async def go():
            await db["brand_profiles"].insert_one({"brand_id": "b1", "user_id": "u1", **BRAND})
            result = await blog.BlogGenerationService.generate(
                user_id="u1", topic="Renewals", primary_keyword="renewal", secondary_keywords=[],
                word_count=100, db=db, brand_id="b1")
            return result, await db[blog.BLOG_POSTS_COLLECTION].find_one({})
        result, saved = asyncio.run(go())
        return result, saved, prompts

    def test_invented_statistic_and_banned_word_are_rewritten(self, monkeypatch):
        result, saved, prompts = self._run(monkeypatch, [BLOG_BAD, BLOG_GOOD])
        assert result["status"] and len(prompts) == 2
        assert '"96%"' in prompts[1] and 'Remove "streamline"' in prompts[1]
        assert "96%" not in result["responseData"]["content"]
        assert saved["playbook_issues"] == []

    def test_clean_blog_costs_no_rewrite(self, monkeypatch):
        result, saved, prompts = self._run(monkeypatch, [BLOG_GOOD])
        assert len(prompts) == 1 and saved["playbook_issues"] == []

    def test_a_rewrite_that_is_no_better_is_not_used_and_is_flagged(self, monkeypatch):
        result, saved, prompts = self._run(monkeypatch, [BLOG_BAD])
        assert result["status"]
        assert any(i.startswith("unverified_number") for i in saved["playbook_issues"])

    def test_expansion_pass_carries_the_brand_rules(self, monkeypatch):
        short = "TITLE: T\nMETA: M\n\nDocerity dey help you."
        result, saved, prompts = self._run(monkeypatch, [short, BLOG_GOOD])
        assert "needs to be at least" in prompts[1]
        assert "real numbers" not in prompts[1]
        assert "streamline" in prompts[1] and "NEVER invent facts" in prompts[1]


class TestAdCopyIsCheckedToo:
    def _run(self, monkeypatch, replies):
        from app.agents.jane_ads import creative
        prompts = []

        async def fake_model(prompt):
            prompts.append(prompt)
            return replies[min(len(prompts) - 1, len(replies) - 1)]

        monkeypatch.setattr(creative, "_call_ad_copy_model", fake_model)
        copy = asyncio.run(creative.write_ad_copy("Docerity", "software", brand_context=BRAND))
        return copy, prompts

    def test_invented_statistic_is_rewritten(self, monkeypatch):
        bad = {"headline": "Paperwork, handled 🎉", "primary_text": "Over 70% of SMEs lose invoices. Try Docerity.",
               "image_prompt": "an office"}
        good = {"headline": "Paperwork, handled", "primary_text": "Stop losing invoices. Try Docerity.",
                "image_prompt": "an office"}
        copy, prompts = self._run(monkeypatch, [bad, good])
        assert len(prompts) == 2 and '"70%"' in prompts[1]
        assert copy.primary_text == "Stop losing invoices. Try Docerity."

    def test_emojis_removed_even_if_the_model_keeps_them(self, monkeypatch):
        reply = {"headline": "Paperwork, handled 🎉", "primary_text": "Try Docerity today ✅", "image_prompt": "an office"}
        copy, prompts = self._run(monkeypatch, [reply])
        assert len(prompts) == 1
        assert copy.headline == "Paperwork, handled" and copy.primary_text == "Try Docerity today"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
