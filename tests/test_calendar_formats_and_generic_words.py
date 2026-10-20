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
