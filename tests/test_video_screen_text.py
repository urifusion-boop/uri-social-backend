"""
Text that is part of a scene (a dashboard on a laptop, a phone screen, a sign) is only readable when it is large in the
frame: the same dashboard is sharp in a push-in close-up and blurry in a wide shot. Both script writers are told to
frame such a scene for big text, and to keep screens in wide shots free of small print.
"""
import pytest

from app.agents.social_media_manager.services import video_creative_service as vcs
from app.agents.social_media_manager.services import video_storyboard_service as vss
from app.agents.social_media_manager.services import video_text_overlay as ov


@pytest.mark.parametrize("prompt", [vcs._SYSTEM_PROMPT, vss._SYSTEM_PROMPT])
def test_both_script_writers_get_the_screen_text_rules(prompt):
    assert ov.SCREEN_TEXT_PLACEHOLDER not in prompt                  # the placeholder was filled, not left in the prompt
    assert ov.SCREEN_TEXT_RULES in prompt
    assert "TEXT INSIDE THE SCENE" in prompt and "close-up, or a slow push-in" in prompt
    assert "never a wide shot with small print" in prompt


@pytest.mark.parametrize("prompt", [vcs._SYSTEM_PROMPT, vss._SYSTEM_PROMPT])
def test_the_screen_rules_sit_beside_the_caption_rule_they_qualify(prompt):
    assert prompt.index("text_overlay is a short on-screen caption") < prompt.index("TEXT INSIDE THE SCENE") \
        < prompt.index("shot_type must be one of")


@pytest.mark.parametrize("prompt", [vcs._SYSTEM_PROMPT, vss._SYSTEM_PROMPT])
def test_the_captions_still_go_through_the_editor_not_the_scene(prompt):
    assert "NEVER describe lettering, captions, titles or text cards" in prompt   # unchanged: captions are drawn by ffmpeg
