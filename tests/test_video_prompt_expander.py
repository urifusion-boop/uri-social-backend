"""
The "Describe it" brief expander writes a STRUCTURED production brief, not a loose paragraph: fixed headings the script
writer, the picture model and the video model can each read, one scene per ~5 seconds, people described once, exact
spoken lines, short captions. Layout and fitting are done in code (compose_video) so they hold whatever the AI returns.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from app.agents.social_media_manager.services import prompt_expander as pe


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def scene(action, says="", caption=""):
    return NS(action=action, says=says, caption=caption)


def brief(**kw):
    base = dict(
        concept="A Lagos customer shows how fast her jollof arrived.",
        style="testimonial_style",
        people=["A woman in her 30s, medium-dark brown skin, oval face, short natural black hair, wearing a solid yellow top"],
        setting="A modest modern Lagos kitchen in daylight, a plain takeaway box on the table.",
        look="Close-up on her face, slow push-in, brand orange only on the logo and the box.",
        scenes=[scene("She opens the box and smiles.", "I ordered at noon and it was here", "Hot. Fresh. Fast."),
                scene("She takes a bite and nods at the camera.", "in twenty minutes.", "Order on WhatsApp")],
        sound="Warm, friendly Nigerian English voice; light upbeat music.",
        call_to_action="Order on WhatsApp, shown as the last caption.",
        avoid="cheap; politics",
        used=["brand voice"],
    )
    base.update(kw)
    return NS(**base)


# ── how many scenes ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("seconds,scenes", [(10, 2), (15, 3), (60, 12), (None, 3), (3, 1), (7, 1), (8, 2)])
def test_one_scene_per_five_seconds_like_the_pipeline(seconds, scenes):
    assert pe.scene_count(seconds) == scenes


# ── the layout ───────────────────────────────────────────────────────────────

def test_the_brief_is_laid_out_under_fixed_headings_one_per_line():
    text = pe.compose_video(brief(), 10)
    assert text.split("\n") == [
        "CONCEPT: A Lagos customer shows how fast her jollof arrived.",
        "STYLE: Testimonial Style.",
        "PERSON: A woman in her 30s, medium-dark brown skin, oval face, short natural black hair, wearing a solid yellow top.",
        "SETTING: A modest modern Lagos kitchen in daylight, a plain takeaway box on the table.",
        "LOOK: Close-up on her face, slow push-in, brand orange only on the logo and the box.",
        'SCENE 1 (0-5s): She opens the box and smiles. Says exactly: "I ordered at noon and it was here" Caption: "Hot. Fresh. Fast."',
        'SCENE 2 (5-10s): She takes a bite and nods at the camera. Says exactly: "in twenty minutes." Caption: "Order on WhatsApp"',
        "SOUND: Warm, friendly Nigerian English voice; light upbeat music.",
        "CLOSE: Order on WhatsApp, shown as the last caption.",
        "AVOID: cheap; politics.",
    ]


def test_two_people_are_numbered_and_a_third_is_dropped():
    text = pe.compose_video(brief(people=["A man.", "A woman.", "A child."]), 10)
    assert "PERSON 1: A man." in text and "PERSON 2: A woman." in text and "PERSON 3" not in text


def test_a_video_with_nobody_speaking_has_no_people_and_no_spoken_lines():
    text = pe.compose_video(brief(people=[], scenes=[scene("Rice steams on a plate."), scene("The box is sealed.")]), 10)
    assert "PERSON" not in text and "Says exactly" not in text and "Caption" not in text


def test_the_last_scene_runs_to_the_end_of_the_video():
    text = pe.compose_video(brief(scenes=[scene("a"), scene("b")]), 12)
    assert "SCENE 1 (0-5s)" in text and "SCENE 2 (5-12s)" in text


def test_extra_scenes_beyond_the_length_are_dropped():
    text = pe.compose_video(brief(scenes=[scene("a"), scene("b"), scene("c"), scene("d")]), 10)
    assert "SCENE 2" in text and "SCENE 3" not in text


def test_captions_are_kept_to_eight_words_and_quotes_inside_lines_cannot_break_them():
    text = pe.compose_video(brief(scenes=[scene("Action.", 'She says "wow" and laughs', "one two three four five six seven eight nine ten")]), 5)
    assert 'Caption: "one two three four five six seven eight"' in text
    assert "Says exactly: \"She says 'wow' and laughs\"" in text


def test_markdown_and_line_breaks_inside_a_field_are_flattened():
    text = pe.compose_video(brief(setting="A **bright**\nkitchen with [a link](https://x.test) on the wall"), 10)
    assert "SETTING: A bright kitchen with a link on the wall." in text and "**" not in text


def test_a_lead_in_like_create_a_is_not_left_on_the_concept():
    assert pe.compose_video(brief(concept="Create a warm testimonial about fast delivery"), 10).startswith(
        "CONCEPT: Warm testimonial about fast delivery.")


# ── fitting the 2000-character brief box ─────────────────────────────────────

def test_a_sixty_second_brief_fits_the_box_and_never_cuts_a_spoken_line():
    long_action = "She walks to the counter, picks up the warm takeaway box, and studies the lid with real interest. " * 3
    scenes = [scene(long_action, f"Line number {i} that must survive whole.", f"Caption {i}") for i in range(12)]
    text = pe.compose_video(brief(scenes=scenes, sound="x " * 100, avoid="y " * 100, setting="z " * 100, look="w " * 100), 60)
    assert len(text) <= pe.VIDEO_MAX_CHARS
    for i in range(12):
        assert f'Says exactly: "Line number {i} that must survive whole."' in text
    assert text.count("SCENE ") == 12


def test_a_short_brief_is_left_whole():
    text = pe.compose_video(brief(), 10)
    assert "AVOID:" in text and "SOUND:" in text and len(text) < pe.VIDEO_MAX_CHARS


# ── what the model is asked ──────────────────────────────────────────────────

def test_the_request_names_the_exact_scene_count_and_carries_the_pipeline_rules():
    msgs = pe.build_video_messages("A testimonial for our jollof", {"brand_name": "Bukka Express"}, "instagram_reels", 10,
                                   datetime(2026, 11, 9))
    user = msgs[1]["content"]
    assert "Scenes: EXACTLY 2 (one per 5 seconds)" in user and "Target length: about 10 seconds" in user
    assert "Bukka Express" in user and "A testimonial for our jollof" in user
    system = " ".join(msgs[0]["content"].split())      # the prompt wraps lines; compare the words
    for rule in ("ONE fixed outfit", "WORD FOR WORD", "8 words or fewer", "Brand colours go on the logo",
                 "ONLY when the idea calls for someone speaking to camera", "about 12 words at most"):
        assert rule in system, rule


# ── end to end ───────────────────────────────────────────────────────────────

class _Completion:
    def __init__(self, parsed):
        self.choices = [NS(message=NS(parsed=parsed))]


@pytest.fixture
def ai(monkeypatch):
    seen = {}

    def configure(parsed=None, error=None, raises=False):
        monkeypatch.setattr(pe.AIService, "build_ai_model", staticmethod(lambda **kw: seen.setdefault("req", kw)), raising=False)

        async def fake(req, response_model=None):
            seen["response_model"] = response_model
            if raises:
                raise RuntimeError("boom")
            return {"error": error} if error else _Completion(parsed)

        monkeypatch.setattr(pe.AIService, "structured_chat_completion", staticmethod(fake), raising=False)
        return seen
    return configure


def test_expand_video_returns_the_structured_brief_and_its_version(ai):
    seen = ai(parsed=brief())
    out = _run(pe.expand_video("A testimonial for our jollof", {"brand_name": "Bukka Express"}, "instagram_reels", 10))
    assert out["expanded_prompt"].startswith("CONCEPT: ") and "SCENE 2 (5-10s)" in out["expanded_prompt"]
    assert out["prompt_version"] == "video-prompt-expander-v3" and out["used"] == ["brand voice"]
    assert seen["response_model"] is pe._ExpandedVideo


@pytest.mark.parametrize("kwargs", [dict(error="rate limited"), dict(raises=True), dict(parsed=None),
                                    dict(parsed=NS(concept="  ", scenes=[]))])
def test_a_failed_or_empty_answer_returns_none_so_the_box_is_left_alone(ai, kwargs):
    ai(**kwargs)
    assert _run(pe.expand_video("A testimonial", {}, "instagram_reels", 10)) is None


# ── styles ───────────────────────────────────────────────────────────────────

def test_the_style_is_named_in_the_brief_so_the_script_writer_follows_it():
    assert "STYLE: Viral Fast Cut." in pe.compose_video(brief(style="viral_fast_cut", people=[]), 10)


@pytest.mark.parametrize("style", ["", "not_a_style", None, "  VIRAL_FAST_CUT "])
def test_an_unknown_or_missing_style_is_left_out_not_invented(style):
    text = pe.compose_video(brief(style=style), 10)
    if style and style.strip().lower() == "viral_fast_cut":
        assert "STYLE: Viral Fast Cut." in text
    else:
        assert "STYLE:" not in text


def test_the_style_table_matches_the_styles_the_script_writer_actually_has():
    """Read the slugs out of the storyboard service's source, so this holds without importing its heavy dependencies."""
    import ast
    import pathlib
    src = pathlib.Path(pe.__file__).with_name("video_storyboard_service.py").read_text()
    keys = None
    for node in ast.parse(src).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if getattr(target, "id", "") == "VIDEO_STYLE_DIRECTIVES":
            keys = set(ast.literal_eval(node.value))
    assert keys and keys == set(pe.VIDEO_STYLES)


def test_the_model_is_offered_every_style_and_told_fast_styles_move_the_camera_not_the_face():
    system = " ".join(pe.build_video_messages("x", {}, "tiktok", 10, datetime(2026, 11, 9))[0]["content"].split())
    for slug in pe.VIDEO_STYLES:
        assert f"{slug}:" in system, slug
    assert "{STYLE_MENU}" not in system
    assert "fast, dramatic CAMERA and PRODUCT movement" in system
    assert "Put the energy in the camera and the product, not in a face" in system
    assert "no face at all (or only hands)" in system
