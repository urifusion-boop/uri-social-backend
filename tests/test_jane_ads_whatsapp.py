"""
Unit tests for WhatsApp number normalization (whatsapp.py) — the pure part. The
brand get/set helpers are thin Mongo wrappers (covered live), but normalization is
the bit that decides whether a lead's wa.me link works, so it's tested directly.
"""
from app.agents.jane_ads.whatsapp import normalize_wa_number


def test_local_nigerian_number_gets_country_code():
    assert normalize_wa_number("0803 123 4567") == "2348031234567"


def test_already_international_with_plus():
    assert normalize_wa_number("+234 803 123 4567") == "2348031234567"


def test_bare_number_no_cc_no_leading_zero():
    assert normalize_wa_number("8031234567") == "2348031234567"


def test_already_country_coded_unchanged():
    assert normalize_wa_number("2348031234567") == "2348031234567"


def test_strips_punctuation_and_spaces():
    assert normalize_wa_number("(0803)-123-4567") == "2348031234567"


def test_empty_returns_none():
    assert normalize_wa_number("") is None
    assert normalize_wa_number("   ") is None


def test_gibberish_returns_none():
    assert normalize_wa_number("not a number") is None


def test_too_short_returns_none():
    assert normalize_wa_number("12345") is None


def test_too_long_returns_none():
    assert normalize_wa_number("1234567890123456789") is None
