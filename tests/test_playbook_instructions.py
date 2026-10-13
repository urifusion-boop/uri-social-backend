"""
"Tell Jane": the owner describes what they want in plain words and the Brand
Playbook updates itself — fields where one fits, standing instructions for the
rest — and every writer follows it from then on.

The model call is faked; everything after it (validation, writing, history,
undo, and how instructions reach each writer's prompt) is real.
"""
from datetime import datetime

import pytest

pytest.importorskip("mongomock_motor")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.agents.social_media_manager.routers.complete_social_manager import router as smm_router  # noqa: E402
from app.agents.social_media_manager.services import brand_brief as bb  # noqa: E402
from app.agents.social_media_manager.services import playbook_instructions as pi  # noqa: E402
from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService  # noqa: E402
from app.agents.social_media_manager.services.content_generation_service import ContentGenerationService  # noqa: E402
from app.dependencies import get_active_brand_context, get_db_dependency  # noqa: E402

USER, BRAND = "u1", "brand_1"
PROFILE = {
    "user_id": USER, "brand_id": BRAND, "brand_name": "Bukka Express", "industry": "Restaurant",
    "region": "Nigeria", "product_description": "Home-style Nigerian food, delivered",
    "guardrails": {"avoid_topics": "politics", "emoji_usage": "some"}, "words_to_avoid": ["cheap"],
    "cta_styles": ["DM us"], "voice_profile": {"caption_length": "medium", "emoji_usage": "light"},
}


def plan(proposal, profile=None):
    return pi.plan_update(proposal, PROFILE if profile is None else profile, now=datetime(2026, 10, 13))


class TestPlanUpdate:
    def test_fields_and_instructions_together(self):
        out = plan({
            "changes": [
                {"field": "guardrails.emoji_usage", "op": "set", "value": "no"},
                {"field": "cta_styles", "op": "set", "value": ["Order on WhatsApp"]},
                {"field": "voice_profile.caption_length", "op": "set", "value": "short"},
                {"field": "platform_tones.linkedin", "op": "set", "value": "formal and precise"},
                {"field": "words_to_avoid", "op": "add", "value": ["game-changer", "Cheap"]},
            ],
            "remember": [{"text": "Start every caption with a one-line hook in capital letters.",
                          "applies_to": ["posts"], "platforms": []}],
        })
        s = out["set"]
        assert s["guardrails"] == {"avoid_topics": "politics", "emoji_usage": "no"}   # rest of guardrails kept
        assert s["cta_styles"] == ["Order on WhatsApp"]
        assert s["voice_profile"]["caption_length"] == "short"
        assert s["voice_profile"]["emoji_usage"] == "none"        # kept consistent with the guardrail
        assert s["platform_tones"] == {"linkedin": "formal and precise"}
        assert s["same_tone_everywhere"] is False
        assert s["words_to_avoid"] == ["cheap", "game-changer"]   # no duplicate, case-insensitive
        assert s["playbook_instructions"][0]["text"].startswith("Start every caption")
        labels = {c["label"]: (c["before"], c["after"]) for c in out["changes"]}
        assert labels["Emoji usage"] == ("A few (1–2)", "None")
        assert labels["Caption length"] == ("Medium (5–8 lines)", "Short (3–5 lines)")

    def test_values_are_labelled_by_their_field(self):
        out = plan({"changes": [{"field": "voice_profile.hook_style", "op": "set", "value": "question"},
                                {"field": "voice_profile.cta_style", "op": "set", "value": "urgent"}]})
        assert {c["label"]: c["after"] for c in out["changes"]} == {"Opening line": "A question",
                                                                     "Call-to-action style": "Urgent"}

    def test_rejects_what_it_does_not_know_or_allow(self):
        out = plan({"changes": [
            {"field": "brand_name", "op": "set", "value": "Hacked"},           # not editable by text
            {"field": "user_id", "op": "set", "value": "someone"},
            {"field": "approval_workflow", "op": "set", "value": "publish everything"},  # not an option
            {"field": "voice_profile.caption_length", "op": "set", "value": "huge"},
            {"field": "tagline", "op": "add", "value": "x"},                     # wrong op for text
        ]})
        assert out["set"] == {} and out["changes"] == []

    def test_option_matching_is_case_insensitive(self):
        out = plan({"changes": [{"field": "approval_workflow", "op": "set", "value": "Auto"}]})
        assert out["set"] == {"approval_workflow": "auto"}

    def test_remove_from_a_list(self):
        out = plan({"changes": [{"field": "words_to_avoid", "op": "remove", "value": "CHEAP"}]})
        assert out["set"] == {"words_to_avoid": []}

    def test_no_change_when_already_so(self):
        out = plan({"changes": [{"field": "cta_styles", "op": "add", "value": "DM us"}]})
        assert out["set"] == {} and out["changes"] == []

    def test_posting_times_set_the_mode(self):
        out = plan({"changes": [{"field": "posting_time_prefs.linkedin", "op": "set",
                                 "value": ["Early Morning (6-9am)", "made-up slot"]}]})
        assert out["set"]["posting_time_prefs"] == {"linkedin": "Early Morning (6-9am),"}
        assert out["set"]["posting_time_mode"] == "custom"

    def test_no_hashtags_means_none_in_the_voice_profile_too(self):
        out = plan({"changes": [{"field": "guardrails.max_hashtags", "op": "set", "value": "0"}]})
        assert out["set"]["voice_profile"]["hashtag_style"] == "none"

    def test_newer_instruction_replaces_the_one_it_contradicts(self):
        profile = {**PROFILE, "playbook_instructions": [
            {"id": "ins_old", "text": "Keep every caption under 3 lines.", "applies_to": ["posts"]},
            {"id": "ins_keep", "text": "Always mention free delivery in Lekki.", "applies_to": ["all"]}]}
        out = plan({"forget": ["ins_old"],
                    "remember": [{"text": "Captions can be long stories of 8-12 lines.", "applies_to": ["posts"]}]},
                   profile)
        texts = [i["text"] for i in out["set"]["playbook_instructions"]]
        assert texts == ["Always mention free delivery in Lekki.", "Captions can be long stories of 8-12 lines."]
        assert [i["id"] for i in out["removed"]] == ["ins_old"]

    def test_duplicate_instruction_is_not_added_twice(self):
        profile = {**PROFILE, "playbook_instructions": [{"id": "a", "text": "Always use the ₦ sign for prices."}]}
        out = plan({"remember": [{"text": "always use the ₦ sign for prices"}]}, profile)
        assert out["set"] == {}

    def test_scope_and_platforms_are_cleaned(self):
        out = plan({"remember": [{"text": "Use a thread of at most 4 tweets.", "applies_to": ["posts", "bogus"],
                                  "platforms": ["X", "myspace"]},
                                 {"text": "Sign off replies with '— Team Bukka'.", "applies_to": []}]})
        a, b = out["set"]["playbook_instructions"]
        assert a["applies_to"] == ["posts"] and a["platforms"] == ["twitter"]
        assert b["applies_to"] == ["posts"]


BC_WITH_INSTRUCTIONS = {
    "brand_name": "Bukka",
    "playbook_instructions": [
        {"id": "1", "text": "Start every caption with a one-line hook in capitals.", "applies_to": ["posts"]},
        {"id": "2", "text": "Always write prices with the ₦ sign.", "applies_to": ["all"]},
        {"id": "3", "text": "Keep it under 4 tweets.", "applies_to": ["posts"], "platforms": ["twitter"]},
        {"id": "4", "text": "Sign off with '— Team Bukka'.", "applies_to": ["replies"]},
    ],
}


class TestEveryWriterFollowsThem:
    def test_posts_on_a_platform(self):
        assert bb.owner_instructions(BC_WITH_INSTRUCTIONS, "posts", "instagram") == [
            "Start every caption with a one-line hook in capitals.", "Always write prices with the ₦ sign."]
        assert "Keep it under 4 tweets." in bb.owner_instructions(BC_WITH_INSTRUCTIONS, "posts", "x")

    def test_without_a_platform_they_say_where_they_apply(self):
        assert "On X (Twitter): Keep it under 4 tweets." in bb.owner_instructions(BC_WITH_INSTRUCTIONS, "posts")

    def test_each_kind_gets_only_its_own(self):
        assert bb.owner_instructions(BC_WITH_INSTRUCTIONS, "ads") == ["Always write prices with the ₦ sign."]
        assert bb.owner_instructions(BC_WITH_INSTRUCTIONS, "replies") == [
            "Always write prices with the ₦ sign.", "Sign off with '— Team Bukka'."]

    def test_they_are_hard_rules_in_the_caption_prompt(self):
        prompt = ContentGenerationService.build_caption_prompt("New menu", "instagram", BC_WITH_INSTRUCTIONS)
        assert "THE OWNER'S OWN INSTRUCTIONS" in prompt
        assert "one-line hook in capitals" in prompt and "Keep it under 4 tweets" not in prompt

    def test_ads_blog_video_inbox_get_them_through_the_brief(self):
        from app.agents.inbox.agent import brand_style
        ads = bb.build(BC_WITH_INSTRUCTIONS, sections=("rules",), kind="ads")
        assert "₦ sign" in ads and "one-line hook" not in ads
        section, _ = brand_style({"brand_name": "Bukka", "playbook_instructions": BC_WITH_INSTRUCTIONS["playbook_instructions"]})
        assert "Team Bukka" in section and "one-line hook" not in section

    def test_saved_profile_reaches_the_writers(self):
        bc = BrandProfileService.to_brand_context({**PROFILE, **BC_WITH_INSTRUCTIONS})
        assert len(bc["playbook_instructions"]) == 4

    def test_no_instructions_no_extra_rule(self):
        assert not any("OWNER" in r for r in bb.hard_rules({"brand_name": "x"}))


class TestFreeTextSettings:
    @pytest.mark.parametrize("value,mode", [("no", "no"), ("None", "no"), ("No emojis", "no"), ("never", "no"),
                                            ("0", "no"), ("some", "some"), ("Minimal, 1–2 per post", "some"),
                                            ("yes", "free"), ("Lots of food emojis", "free"), ("", "")])
    def test_emoji(self, value, mode):
        assert bb.emoji_mode({"guardrails": {"emoji_usage": value}}) == mode

    def test_none_means_no_emojis_everywhere(self):
        bc = {"guardrails": {"emoji_usage": "None"}}
        assert "Use NO emojis under any circumstances." in bb.hard_rules(bc)
        assert bb.enforce("Hello 🎉", bc, append_cta=False) == "Hello"

    @pytest.mark.parametrize("value,limit", [("5", 5), ("e.g. 3", 3), ("No limit", None), ("none", 0),
                                             ("No hashtags", 0), ("0", 0), ("", None)])
    def test_hashtags(self, value, limit):
        assert bb.hashtag_limit({"guardrails": {"max_hashtags": value}}) == limit

    def test_no_hashtags_rule(self):
        assert "Use NO hashtags." in bb.hard_rules({"guardrails": {"max_hashtags": "none"}}, captions=True)

    def test_voice_profile_defaults_no_longer_contradict(self):
        bc = {"brand_name": "B", "guardrails": {"emoji_usage": "None", "max_hashtags": "0"},
              "voice_profile": {"emoji_usage": "light", "hashtag_style": "minimal"}}
        prompt = ContentGenerationService.build_caption_prompt("x", "instagram", bc)
        assert "Use 1-2 emojis" not in prompt and "Include 1-3 highly relevant hashtags" not in prompt
        assert "Do NOT use any emojis." in prompt and "Do NOT include hashtags." in prompt


# ── Through the API ──────────────────────────────────────────────────────────

@pytest.fixture()
def env(monkeypatch):
    db = AsyncMongoMockClient()["uri_test"]
    app = FastAPI()
    app.include_router(smm_router, prefix="/social-media")
    app.dependency_overrides[get_db_dependency] = lambda: db
    app.dependency_overrides[get_active_brand_context] = lambda: {"user_id": USER, "brand_id": BRAND, "agency_id": None}
    replies = []
    seen = []

    async def fake_model(text, profile):
        seen.append((text, profile))
        return replies.pop(0)

    monkeypatch.setattr(pi, "_ask_model", fake_model)
    with TestClient(app) as client:
        client.portal.call(db["brand_profiles"].insert_one, dict(PROFILE))
        yield client, db, replies, seen


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["responseData"]


def stored(client, db):
    return client.portal.call(db["brand_profiles"].find_one, {"brand_id": BRAND})


SAY = {"understood": True,
       "changes": [{"field": "guardrails.emoji_usage", "op": "set", "value": "no"},
                   {"field": "cta_styles", "op": "set", "value": ["Order on WhatsApp"]}],
       "remember": [{"text": "Start every caption with a one-line hook in capitals.", "applies_to": ["posts"]}],
       "reply": "Done — no emojis, posts end with 'Order on WhatsApp', and every caption opens with a hook."}


class TestTellJaneEndpoint:
    def test_updates_and_remembers(self, env):
        client, db, replies, seen = env
        replies.append(dict(SAY))
        out = data(client.post("/social-media/brand-profile/instruct",
                               json={"text": "No emojis, end with Order on WhatsApp, and open with a hook in caps"}))
        assert out["understood"] and out["change_id"] and out["reply"].startswith("Done")
        assert {c["label"] for c in out["changes"]} == {"Emoji usage", "Call to action"}
        assert out["added"][0]["text"].startswith("Start every caption")
        p = stored(client, db)
        assert p["guardrails"]["emoji_usage"] == "no" and p["guardrails"]["avoid_topics"] == "politics"
        assert p["cta_styles"] == ["Order on WhatsApp"]
        assert out["profile"]["cta_styles"] == ["Order on WhatsApp"]
        # The model saw the current Playbook, so it can add/remove properly.
        assert seen[0][1]["words_to_avoid"] == ["cheap"]
        # And the next caption is written with all of it.
        prompt = ContentGenerationService.build_caption_prompt(
            "Weekend menu", "instagram", BrandProfileService.to_brand_context(p))
        assert "one-line hook in capitals" in prompt and "Use NO emojis" in prompt and "Order on WhatsApp" in prompt

    def test_listed_and_removable(self, env):
        client, db, replies, _ = env
        replies.append(dict(SAY))
        client.post("/social-media/brand-profile/instruct", json={"text": "No emojis and open with a hook in caps"})
        listed = data(client.get("/social-media/brand-profile/instructions"))
        assert len(listed["instructions"]) == 1 and listed["history"][0]["text"].startswith("No emojis")
        ins_id = listed["instructions"][0]["id"]
        left = data(client.delete(f"/social-media/brand-profile/instructions/{ins_id}"))
        assert left["instructions"] == [] and stored(client, db)["playbook_instructions"] == []
        assert client.delete(f"/social-media/brand-profile/instructions/{ins_id}").status_code == 404

    def test_undo_puts_everything_back(self, env):
        client, db, replies, _ = env
        replies.append(dict(SAY))
        change_id = data(client.post("/social-media/brand-profile/instruct",
                                     json={"text": "No emojis, Order on WhatsApp, hook in caps"}))["change_id"]
        out = data(client.post("/social-media/brand-profile/instruct/undo", json={"change_id": change_id}))
        p = stored(client, db)
        assert p["guardrails"] == PROFILE["guardrails"] and p["cta_styles"] == ["DM us"]
        assert "playbook_instructions" not in p
        assert set(out["restored"]) == {"guardrails", "cta_styles", "playbook_instructions", "voice_profile"}
        assert client.post("/social-media/brand-profile/instruct/undo",
                           json={"change_id": change_id}).status_code == 409

    def test_undo_leaves_later_edits_alone(self, env):
        client, db, replies, _ = env
        replies.append(dict(SAY))
        change_id = data(client.post("/social-media/brand-profile/instruct", json={"text": "No emojis etc"}))["change_id"]
        client.portal.call(db["brand_profiles"].update_one, {"brand_id": BRAND},
                           {"$set": {"cta_styles": ["Call 0800 BUKKA"]}})
        out = data(client.post("/social-media/brand-profile/instruct/undo", json={"change_id": change_id}))
        assert out["skipped"] == ["cta_styles"]
        p = stored(client, db)
        assert p["cta_styles"] == ["Call 0800 BUKKA"] and p["guardrails"]["emoji_usage"] == "some"

    def test_a_content_request_is_pointed_to_create_content(self, env):
        client, db, replies, _ = env
        replies.append({"understood": False, "not_a_preference": "content_request", "changes": [], "remember": [],
                        "question": "What style do you want for feature announcements?"})
        out = data(client.post("/social-media/brand-profile/instruct", json={"text": "write a post about rice"}))
        assert out["understood"] is False and "Create Content" in out["reply"] and out["change_id"] is None
        assert stored(client, db)["cta_styles"] == ["DM us"]

    def test_vague_gets_the_models_specific_question(self, env):
        client, _, replies, _ = env
        replies.append({"understood": False, "not_a_preference": "vague",
                        "question": "Better how — shorter, more playful, or more formal?"})
        out = data(client.post("/social-media/brand-profile/instruct", json={"text": "make it better"}))
        assert out["reply"] == "Better how — shorter, more playful, or more formal?"

    def test_nothing_to_change(self, env):
        client, db, replies, _ = env
        replies.append({"understood": True, "changes": [{"field": "cta_styles", "op": "add", "value": "DM us"}]})
        out = data(client.post("/social-media/brand-profile/instruct", json={"text": "end with DM us"}))
        assert out["changes"] == [] and out["change_id"] is None and "already" in out["reply"]

    def test_empty_and_too_long_are_refused(self, env):
        client, _, _, _ = env
        assert client.post("/social-media/brand-profile/instruct", json={"text": " "}).status_code == 400
        assert client.post("/social-media/brand-profile/instruct", json={"text": "x" * 2001}).status_code == 400

    def test_model_failure_is_a_clean_error(self, env, monkeypatch):
        client, db, _, _ = env

        async def broken(text, profile):
            raise RuntimeError("timeout")

        monkeypatch.setattr(pi, "_ask_model", broken)
        r = client.post("/social-media/brand-profile/instruct", json={"text": "no emojis please"})
        assert r.status_code == 503 and "try again" in r.json()["detail"]

    def test_undo_of_someone_elses_change_is_not_found(self, env):
        client, db, _, _ = env
        client.portal.call(db[pi.CHANGES_COLLECTION].insert_one,
                           {"id": "chg_x", "user_id": "intruder", "brand_id": BRAND, "after": {}, "before": {}})
        assert client.post("/social-media/brand-profile/instruct/undo",
                           json={"change_id": "chg_x"}).status_code == 404


class TestAgencyBrandWithoutItsOwnProfile:
    def test_change_starts_the_brands_own_profile(self):
        import asyncio
        db = AsyncMongoMockClient()["t"]

        async def go():
            async def model(text, profile):
                return {"remember": [{"text": "Always mention same-day delivery.", "applies_to": ["all"]}]}
            borrowed = {**PROFILE, "brand_id": "agency_brand_9"}   # what get() returns: the owner's personal Playbook
            await pi.instruct(db, USER, "agency_brand_9", borrowed, "always mention same-day delivery", ask=model)
            return await db["brand_profiles"].find_one({"brand_id": "agency_brand_9"})

        doc = asyncio.run(go())
        assert doc["playbook_instructions"][0]["text"] == "Always mention same-day delivery."
        assert doc["user_id"] == USER


class TestThePromptGivenToTheModel:
    def test_lists_every_field_with_its_options(self):
        for key in ("guardrails.emoji_usage", "voice_profile.caption_length", "platform_tones.linkedin",
                    "posting_time_prefs.general", "cta_styles", "approval_workflow"):
            assert key in pi.SYSTEM_PROMPT
        assert "'storytelling'" in pi.SYSTEM_PROMPT


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
