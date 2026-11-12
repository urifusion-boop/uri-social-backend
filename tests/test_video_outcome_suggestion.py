"""
The video type preselected after a script is written fits the creative direction, costs no more than it must,
and can always be changed by the customer.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents.social_media_manager.services import video_creative_service as vcs

V = vcs.validated_outcome


def _board(pick=None, dialogue=None, reason="because"):
    board = {"scenes": [{"scene_number": 1, "dialogue": dialogue}, {"scene_number": 2, "dialogue": None}]}
    if pick:
        board["suggested_outcome"] = pick
        board["outcome_reason"] = reason
    return board


def test_a_valid_cheap_pick_is_kept_with_its_reason():
    board = _board("quick_video", reason="  Simple lifestyle scenes.  ")
    V(board, False)
    assert board["suggested_outcome"] == "quick_video" and board["outcome_reason"] == "Simple lifestyle scenes."


@pytest.mark.parametrize("pick", ["quick_video", "animate_product", "social_video", "high_quality", "complex_cinematic"])
def test_every_ordinary_outcome_is_accepted(pick):
    board = _board(pick)
    V(board, False)
    assert board["suggested_outcome"] == pick


def test_a_script_with_spoken_dialogue_always_gets_talking_dialogue():
    board = _board("quick_video", dialogue="Hello, I am Ada.")
    V(board, False)
    assert board["suggested_outcome"] == "talking_dialogue"
    assert "speaks on camera" in board["outcome_reason"]


def test_talking_dialogue_without_any_dialogue_is_dropped():
    board = _board("talking_dialogue")
    V(board, False)
    assert "suggested_outcome" not in board and "outcome_reason" not in board


def test_advanced_references_without_reference_images_steps_down():
    board = _board("advanced_references")
    V(board, False)
    assert board["suggested_outcome"] == "high_quality" and "no reference images" in board["outcome_reason"]


def test_advanced_references_is_kept_when_reference_images_exist():
    board = _board("advanced_references")
    V(board, True)
    assert board["suggested_outcome"] == "advanced_references"


@pytest.mark.parametrize("junk", ["", "veo_3", None, 7, "Quick Video"])
def test_an_unknown_pick_is_dropped_so_the_app_falls_back(junk):
    board = _board()
    board["suggested_outcome"], board["outcome_reason"] = junk, "x"
    V(board, False)
    assert "suggested_outcome" not in board and "outcome_reason" not in board


def test_a_missing_reason_is_not_invented():
    board = _board("quick_video", reason="   ")
    V(board, False)
    assert board["suggested_outcome"] == "quick_video" and "outcome_reason" not in board


# ── through the script writer ─────────────────────────────────────────────────

class _FakeOpenAI:
    def __init__(self, script):
        self.script, self.calls = script, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.script)))])


def _write(monkeypatch, script, reference_images=()):
    fake = _FakeOpenAI(script)
    monkeypatch.setattr(vcs, "openai_client", fake)
    result = asyncio.get_event_loop().run_until_complete(vcs.VideoCreativeService.generate_creative_storyboard(
        brief="A fun ad for our lunch combo", reference_images=list(reference_images), brand_context={}))
    return result["storyboard"], fake


SCRIPT = {"creative_direction": "A calm story.", "video_style": "before_after",
          "suggested_outcome": "advanced_references", "outcome_reason": "Matched framing.",
          "scenes": [{"scene_number": 1, "duration_seconds": 5, "dialogue": None, "video_prompt": "x"}]}


def test_the_writer_is_told_to_pick_the_cheapest_type_that_fits(monkeypatch):
    _, fake = _write(monkeypatch, dict(SCRIPT))
    system = fake.calls[0]["messages"][0]["content"]
    assert "CHEAPEST type that fully delivers the creative direction" in system
    for slug in vcs.VIDEO_OUTCOMES:
        assert slug in system
    assert '"suggested_outcome"' in system and '"outcome_reason"' in system


def test_a_concept_written_from_words_alone_never_defaults_to_advanced_references(monkeypatch):
    board, _ = _write(monkeypatch, dict(SCRIPT))
    assert board["suggested_outcome"] == "high_quality"


def test_with_reference_images_advanced_references_can_stand(monkeypatch):
    board, _ = _write(monkeypatch, dict(SCRIPT), reference_images=["data:image/png;base64,AAAA"])
    assert board["suggested_outcome"] == "advanced_references"
