"""
Unit tests for the automated policy-review gate (split-doc "Policy Review").

Deterministic keyword/pattern matching, no live Meta call. Errs conservative: any
BLOCK-severity hit fails the whole creative, since one violation risks the entire
pooled account. Formatting issues (WARN) never block on their own.
"""
from app.agents.jane_ads.policy import Severity, review_ad_creative


def test_approves_clean_ad_copy():
    r = review_ad_creative("Fresh Lunch Daily", "Hot meals near your office.", "a bowl of jollof rice")
    assert r.approved is True
    assert r.violations == []


def test_blocks_prohibited_content_category():
    r = review_ad_creative("Big Sale", "marijuana for sale this weekend only")
    assert r.approved is False
    cats = {v.category for v in r.violations}
    assert "illegal_drugs" in cats


def test_blocks_exaggerated_claims():
    r = review_ad_creative("Skin Treatment", "Guaranteed to cure your skin condition overnight!")
    assert r.approved is False
    cats = {v.category for v in r.violations}
    assert "exaggerated_claims" in cats


def test_blocks_personal_attribute_assertion():
    r = review_ad_creative("Lose weight fast", "Are you overweight? Try our tea today.")
    assert r.approved is False
    cats = {v.category for v in r.violations}
    assert "personal_attributes" in cats


def test_checks_image_prompt_too():
    r = review_ad_creative("Weekend Deals", "Everything must go", image_prompt="firearms for sale on a table")
    assert r.approved is False
    assert any(v.category == "weapons" for v in r.violations)


def test_case_insensitive_matching():
    r = review_ad_creative("BUY COCAINE NOW", "")
    assert r.approved is False


def test_warns_on_excessive_caps_but_does_not_block():
    r = review_ad_creative("BUY NOW AMAZING DEALS TODAY", "")
    assert r.approved is True
    assert any(v.category == "excessive_caps" and v.severity == Severity.WARN for v in r.violations)


def test_warns_on_excessive_punctuation_but_does_not_block():
    r = review_ad_creative("Amazing deals!!!", "")
    assert r.approved is True
    assert any(v.category == "excessive_punctuation" and v.severity == Severity.WARN for v in r.violations)


def test_multiple_violations_all_reported():
    r = review_ad_creative("BUY COCAINE NOW!!!", "guaranteed to cure everything")
    cats = {v.category for v in r.violations}
    assert "illegal_drugs" in cats
    assert "exaggerated_claims" in cats
    assert "excessive_caps" in cats
    assert "excessive_punctuation" in cats
    assert r.approved is False


def test_empty_fields_are_safe_and_approved():
    r = review_ad_creative("", "", "")
    assert r.approved is True
    assert r.violations == []
