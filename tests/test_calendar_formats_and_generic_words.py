"""
Found on prod (Oct 2026): the 30-day calendar ignored the Playbook's chosen
formats (Uri Social picked Carousel posts + Static image and still got a
video every week), and its copy was full of generic AI words ("discover" 17
times, "journey" 11, "transform" 18) that were only ever flagged.
"""
import collections

from app.agents.content_calendar_v2.services import content_calendar_v2_service as svc


def _concepts(n=30):
    # Mix of hints the format step would otherwise turn into video/carousel/image.
    hints = ["ai_video", "carousel", "image", "video", "product_video", "image"]
    return [{"day_index": i, "date": f"d{i}", "format_hint": hints[i % len(hints)],
             "creative_device": {"category": "", "device": ""}} for i in range(n)]


def _formats(brand):
    return collections.Counter(c["format"] for c in svc._assign_formats(_concepts(), brand, ""))


def test_playbook_formats_are_the_only_formats_used():
    got = _formats({"preferred_formats": ["Carousel posts", "Static image"]})
    assert set(got) == {"carousel", "image"}
    assert sum(got.values()) == 30
    assert min(got.values()) >= 10          # spread across both, not all one


def test_converted_carousels_get_a_slide_count():
    out = svc._assign_formats(_concepts(), {"preferred_formats": ["Carousel posts"]}, "")
    assert all(c["format"] == "carousel" and 2 <= c["carousel_slide_count"] <= 5 for c in out)


def test_choosing_reels_lifts_the_one_a_week_video_cap():
    out = svc._assign_formats(_concepts(), {"preferred_formats": ["Reels / Short video", "Static image"]}, "")
    per_week = collections.Counter(c["day_index"] // 7 for c in out if c["format"] in svc._VIDEO_FORMATS)
    assert max(per_week.values()) == svc.MAX_VIDEO_FORMATS_PER_WEEK_WHEN_CHOSEN
    assert {c["format"] for c in out} <= svc._VIDEO_FORMATS | {"image"}


def test_reels_only_brand_gets_only_video():
    out = svc._assign_formats(_concepts(), {"preferred_formats": ["Reels / Short video"]}, "")
    assert all(c["format"] in svc._VIDEO_FORMATS for c in out)   # the cap never forces a format they didn't pick


def test_no_choice_keeps_the_old_behaviour():
    out = svc._assign_formats(_concepts(), {}, "")
    per_week = collections.Counter(c["day_index"] // 7 for c in out if c["format"] in svc._VIDEO_FORMATS)
    assert max(per_week.values()) == svc.MAX_VIDEO_FORMATS_PER_WEEK
    assert svc._allowed_formats({"preferred_formats": ["Poll", "Live session"]}) is None


def test_prompt_carries_formats_and_the_generic_word_ban():
    block = svc._brand_rules_block({"preferred_formats": ["Static image"]})
    assert "FORMATS this brand posts in: image" in block
    assert "transform" in block and "journey" in block
    assert "NEVER use these generic AI words" in svc._brand_rules_block({})


def test_generic_words_force_a_rewrite():
    bad = {"title": "Discover the Uri Social Transformation", "exact_copy": {"caption": "Your content journey"}}
    assert set(svc._generic_words_used(bad)) == {"discover", "transform", "journey"}
    issues = svc._validate_item_v2({**bad, "description": "x"}, is_carousel=False)
    assert any("generic AI word" in i for i in issues)


def test_plain_copy_and_internal_notes_pass():
    ok = {"title": "Why your posts go quiet on Fridays", "reasoning": "a transformation angle",
          "exact_copy": {"caption": "Three posts a week beat seven rushed ones."}}
    assert svc._generic_words_used(ok) == []


# ── last pass: reword, then plain-word swap ─────────────────────────────────

import asyncio
from types import SimpleNamespace
from unittest.mock import patch


def _plan_item():
    return {"title": "Discover the Journey", "description": "We transform how you post.",
            "key_points": ["Unlock more reach", "Post at 7pm"],
            "exact_copy": {"headline": "Plain headline", "caption": "Seamlessly scheduled.\n#uri"},
            "carousel": {"slides": [{"headline": "Elevate it", "body": "ok"}]},
            "video_idea": {"hook": "fine", "cta": "Empower your team", "talking_points": ["Delve in"]}}


def _no_generic(item):
    from app.agents.social_media_manager.services.brand_brief import find_banned
    return all(not find_banned(get(), svc.GENERIC_AI_WORDS) for get, _ in svc._scrub_slots(item))


def test_model_rewrite_is_used_when_clean():
    item = _plan_item()

    async def fake(req):
        import json
        texts = json.loads(req["messages"][0]["content"].rsplit("\n\n", 1)[1]) if isinstance(req, dict) else {}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({k: "Plain words" for k in texts})))])

    with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
         patch.object(svc.AIService, "chat_completion", side_effect=fake):
        n = asyncio.run(svc._scrub_generic_words([item]))
    assert n == 7 and item["title"] == "Plain words" and item["key_points"][1] == "Post at 7pm"
    assert item["exact_copy"]["headline"] == "Plain headline"      # untouched: had no generic word
    assert _no_generic(item)


def test_code_swap_cleans_up_when_the_model_fails():
    item = _plan_item()
    with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
         patch.object(svc.AIService, "chat_completion", side_effect=RuntimeError("down")):
        asyncio.run(svc._scrub_generic_words([item]))
    assert item["title"] == "Find the Path"
    assert item["description"] == "We change how you post."
    assert item["exact_copy"]["caption"] == "Smoothly scheduled.\n#uri"
    assert _no_generic(item)


def test_plain_words_keeps_case_and_leaves_other_words():
    assert svc._plain_words("Transformation starts here; discovering a seamless flow") == \
        "Change starts here; finding a smooth flow"
    assert svc._plain_words("Book a call") == "Book a call"
