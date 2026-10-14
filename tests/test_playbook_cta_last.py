"""
"Always END posts with X": the call-to-action line closes the piece even when
the writer added another line after it. Only short CTA lines are moved.
"""
from app.agents.social_media_manager.services import brand_brief as bb

BC = {"cta_styles": ["DM us to start"]}


def test_a_line_after_the_cta_moves_above_it():
    text = "Hook line.\n\nDM us to start.\n\nDon't forget, we have a 7-day free trial!"
    assert bb.enforce(text, BC) == "Hook line.\n\nDon't forget, we have a 7-day free trial!\n\nDM us to start."


def test_hashtags_stay_at_the_very_end():
    text = "Hook.\n\nDM us to start and see more.\n\nAnd don't forget the trial!\n\n#A #B"
    assert bb.enforce(text, BC) == "Hook.\n\nAnd don't forget the trial!\n\nDM us to start and see more.\n\n#A #B"


def test_already_last_is_untouched():
    text = "Hook.\n\nDon't forget the trial!\n\nDM us to start."
    assert bb.enforce(text, BC) == text


def test_a_cta_inside_a_long_paragraph_is_left_alone():
    para = "We say DM us to start " + "and keep talking for a long while " * 5
    text = f"{para.strip()}.\n\nLast line."
    assert bb.enforce(text, BC) == text


def test_no_cta_at_all_still_gets_one_added():
    assert bb.enforce("Hook.\n\nMore.", BC).endswith("\n\nDM us to start.")


def test_threads_are_not_reordered():
    text = "1/ Hook. DM us to start.\n\n2/ More."
    assert bb.enforce(text, BC, append_cta=False) == text
