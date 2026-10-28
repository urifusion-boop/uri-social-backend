"""
YarnGPT deletes every character that is not a letter (after spelling out digits), so what we
send it has to be plain spoken words first. See services/yarngpt_tts/normalize.py.
"""
import pytest

from services.yarngpt_tts.normalize import normalize_for_speech


@pytest.mark.parametrize("written,spoken", [
    ("Pay ₦31,000 today", "Pay 31000 naira today"),
    ("It costs ₦1,500.50 only", "It costs 1500.50 naira only"),
    ("Only N5000", "Only 5000 naira"),
    ("Save 50% now", "Save 50 percent now"),
    ("Posts & ads", "Posts and ads"),
    ("Let AI run your socials", "Let A I run your socials"),
    ("Don't wait, it's free", "Dont wait, its free"),
    ("Mon-Fri / weekends", "Mon Fri weekends"),
    ("Dr. Ade and Mr. Bola", "doctor Ade and mister Bola"),
])
def test_written_text_becomes_spoken_words(written, spoken):
    assert normalize_for_speech(written) == spoken


def test_a_number_with_commas_is_one_number_not_two():
    # "31,000" read as "31" and "000" would be a different amount entirely.
    assert "," not in normalize_for_speech("₦31,000").replace("naira", "")


def test_empty_and_whitespace_give_nothing_to_say():
    assert normalize_for_speech("") == ""
    assert normalize_for_speech("   ") == ""
    assert normalize_for_speech(None) == ""


def test_a_plain_sentence_is_left_alone():
    line = "Abeg, if you are tired of posting every single day, I have good news"
    assert normalize_for_speech(line) == line
