"""
The structured brief written by the prompt expander is actually READ by the script writer:
  - the writer's prompt tells it how to read the headed lines (SCENE n, Says exactly, Caption, PERSON, STYLE...);
  - a "STYLE: <name>." line picks the video style in code, unless the customer chose one explicitly;
  - the expander no longer leaks post-only rules (hashtags, emoji) or invented claims into a video brief.
"""
import ast
import asyncio
import json
import pathlib
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from app.agents.social_media_manager.services import prompt_expander as pe
from app.agents.social_media_manager.services import video_styles as vs

SERVICES = pathlib.Path(pe.__file__).parent


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── reading the style out of a brief ─────────────────────────────────────────

@pytest.mark.parametrize("brief,slug", [
    ("CONCEPT: x.\nSTYLE: Viral Fast Cut.\nSCENE 1 (0-5s): y.", "viral_fast_cut"),
    ("STYLE: Before / After.", "before_after"),
    ("style: product explosion", "product_explosion"),
    ("  STYLE:  Testimonial Style  ", "testimonial_style"),
    ("STYLE: countdown_hype", "countdown_hype"),
    ("CONCEPT: x.\r\nSTYLE: Mood Film.\r\nSCENE 1: y", "mood_film"),
])
def test_the_style_named_on_a_style_line_is_found(brief, slug):
    assert vs.style_from_brief(brief) == slug


@pytest.mark.parametrize("brief", [
    None, "", "A video in the style of a documentary", "STYLE: Not A Real Style.", "Our style: Viral Fast Cut",
    "CONCEPT: a street style shoot.\nSCENE 1: model walks.",
])
def test_nothing_is_taken_from_free_text_or_an_unknown_name(brief):
    assert vs.style_from_brief(brief) is None


def test_the_style_table_matches_the_styles_the_script_writer_actually_has():
    src = (SERVICES / "video_storyboard_service.py").read_text()
    keys = None
    for node in ast.parse(src).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if getattr(target, "id", "") == "VIDEO_STYLE_DIRECTIVES":
            keys = set(ast.literal_eval(node.value))
    assert keys and keys == set(vs.VIDEO_STYLES)


# ── the writer's prompt ──────────────────────────────────────────────────────

def _writer_prompt():
    for node in ast.parse((SERVICES / "video_creative_service.py").read_text()).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_SYSTEM_PROMPT":
            return " ".join(ast.literal_eval(node.value.func.value).split())     # a .replace(...) call; the literal inside
    raise AssertionError("no _SYSTEM_PROMPT")


def test_the_writer_is_told_how_to_read_the_headed_lines():
    prompt = _writer_prompt()
    for rule in ("STRUCTURED BRIEF", 'Each "SCENE n (a-bs):" line is scene n, in that order',
                 "use the words WORD FOR WORD", "A scene with no \"Says exactly\" has dialogue null",
                 "is that scene's text_overlay, word for word",
                 "copy that description word for word into the video_prompt of every scene",
                 "STYLE names the video style", "treat it as a free-text idea"):
        assert rule in prompt, rule


# ── the style is applied in code ─────────────────────────────────────────────

SCRIPT = {
    "creative_direction": "A fast, punchy product video.", "video_style": "clean_commercial",
    "suggested_outcome": "high_quality", "outcome_reason": "Polished.", "total_duration_seconds": 10,
    "target_platform": "instagram_reels", "aspect_ratio": "9:16",
    "scenes": [{"scene_number": i, "duration_seconds": 5, "shot_type": "product_hero", "motion": "snap zoom",
                "video_prompt": f"Scene {i}", "reference_image_index": 0, "text_overlay": None,
                "continuity_note": "x", "dialogue": None} for i in (1, 2)],
}


class _FakeOpenAI:
    def __init__(self):
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return NS(choices=[NS(message=NS(content=json.dumps(SCRIPT)))])


def _write(monkeypatch, brief, **kw):
    from app.agents.social_media_manager.services import video_creative_service as vcs
    fake = _FakeOpenAI()
    monkeypatch.setattr(vcs, "openai_client", fake)
    result = _run(vcs.VideoCreativeService.generate_creative_storyboard(
        brief=brief, reference_images=[], brand_context={}, **kw))
    return result, fake.calls[0]["messages"][0]["content"]


BRIEF = "CONCEPT: A can of juice bursts open.\nSTYLE: Viral Fast Cut.\nSCENE 1 (0-5s): Juice sprays across the frame."


def test_a_style_named_in_the_brief_is_applied_and_the_model_is_not_left_to_choose(monkeypatch):
    result, system = _write(monkeypatch, BRIEF)
    assert "VIDEO STYLE — VIRAL FAST CUT" in system and "CHOOSE ONE" not in system
    assert result["storyboard"]["video_style"] == "viral_fast_cut"       # not what the model echoed (clean_commercial)


def test_the_customers_own_style_choice_beats_the_one_in_the_brief(monkeypatch):
    result, system = _write(monkeypatch, BRIEF, video_style="clean_commercial")
    assert "VIDEO STYLE — CLEAN COMMERCIAL" in system and "VIRAL FAST CUT" not in system
    assert result["storyboard"]["video_style"] == "clean_commercial"


def test_a_brief_with_no_style_line_still_lets_the_model_choose(monkeypatch):
    _, system = _write(monkeypatch, "A short video about our new juice.")
    assert "CHOOSE ONE" in system


# ── the expander no longer leaks post-only rules or invents claims ───────────

@pytest.mark.parametrize("text,kept", [
    ("Emoji usage, more than 5 hashtags.", ""),
    ("Do not post about politics, cheap, deal of the century", "Do not post about politics; cheap; deal of the century"),
    ("no emojis; never say 'cheap'", "never say 'cheap'"),
    ("Use at most 3 #tags, no politics", "no politics"),
    ("competitor names (Bolt, Uber), cheap", "competitor names (Bolt, Uber); cheap"),
    (None, ""),
])
def test_only_the_parts_about_posts_are_dropped_from_what_a_video_must_avoid(text, kept):
    assert pe.video_avoid(text) == kept


def test_a_leaked_hashtag_rule_leaves_no_avoid_line_at_all():
    parsed = NS(concept="A juice can bursts open.", style="", people=[], setting="", look="",
                scenes=[NS(action="Juice sprays.", says="", caption="")], sound="", call_to_action="",
                avoid="Emoji usage, more than 5 hashtags.")
    assert "AVOID" not in pe.compose_video(parsed, 5)


def test_post_only_playbook_facts_are_not_sent_to_the_video_expander():
    bc = {"brand_name": "Bukka Express", "content_pillars": ["Recipes"], "default_link": "https://bukka.test",
          "guardrails": {"emoji_usage": "no emojis", "hashtags": "at most 5 hashtags", "avoid_topics": "politics"},
          "words_to_avoid": ["cheap"]}
    user = pe.build_video_messages("x", bc, "tiktok", 10, datetime(2026, 11, 9))[1]["content"]
    assert "Recipes" not in user and "bukka.test" not in user
    assert "hashtag" not in user.lower() and "emoji" not in user.lower()
    assert "politics" in user and "cheap" in user and "Bukka Express" in user


def test_the_expander_is_told_not_to_invent_claims_or_sound_events_and_to_spread_the_speech():
    system = " ".join(pe.build_video_messages("x", {}, "tiktok", 10, datetime(2026, 11, 9))[0]["content"].split())
    for rule in ('Never add a promise or claim of your own ("every time"', "never invent a story event such as a knock on the door",
                 "spread the spoken script across ALL the scenes"):
        assert rule in system, rule
