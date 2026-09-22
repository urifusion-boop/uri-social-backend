"""
Content Calendar V2 — deterministic guardrail checks.

Covers two bugs found by live end-to-end verification of a real 30-day
generation run:

1. A brand's words_to_avoid was only ever a prompt instruction — the model
   used a banned word ("guaranteed") anyway under pressure to satisfy other
   requirements. Per the PRD's own rule (code enforces hard constraints,
   the LLM handles creative judgment), this is now a deterministic,
   case-insensitive check wired into the existing validation-failure ->
   regenerate loop, not just prompt wording.
2. A series name template with an unresolved placeholder ("The [Industry]
   Myth") shipped verbatim in a real generated plan, because nothing ever
   substituted the bracket before it reached the model or the final item.
"""
from app.agents.content_calendar_v2.services.content_calendar_v2_service import (
    _collect_item_text,
    _find_banned_words,
    _resolve_series_name,
    _validate_item_v2,
)


# ── _resolve_series_name ────────────────────────────────────────────────────

def test_resolve_series_name_substitutes_industry_placeholder():
    assert _resolve_series_name("The [Industry] Myth", "SunPay Solar", "solar financing") == \
        "The solar financing Myth"


def test_resolve_series_name_substitutes_brand_placeholder():
    assert _resolve_series_name("Ask [Brand]", "SunPay Solar", "solar financing") == "Ask SunPay Solar"


def test_resolve_series_name_leaves_plain_names_untouched():
    assert _resolve_series_name("Founder Truths", "SunPay Solar", "solar financing") == "Founder Truths"


def test_resolve_series_name_handles_none():
    assert _resolve_series_name(None, "SunPay Solar", "solar financing") is None


# ── _collect_item_text / _find_banned_words ─────────────────────────────────

def _item(**overrides):
    base = {
        "title": "Power costs shouldn't feel like a gamble",
        "hook": "Every NEPA outage costs you more than you think.",
        "description": "A look at what unreliable power really costs SMEs.",
        "caption_direction": "Focus on predictability, not fear.",
        "cta": "Book a free site assessment.",
        "key_points": ["Predictable monthly cost", "No fuel logistics"],
        "keywords": ["solar", "SME", "Nigeria"],
        "exact_copy": {
            "headline": "Predictable power, finally.",
            "caption": "Our system delivers stable power all month.",
            "hashtags": ["solar", "nigeria"],
        },
        "video_idea": {
            "format": "talking_head",
            "hook": "Every NEPA outage costs you more than you think.",
            "talking_points": ["Real monthly cost of downtime"],
            "scenes": ["Founder speaking direct to camera"],
            "cta": "Book a free site assessment.",
        },
        "carousel": None,
        "reasoning": "Ties into the business's core objection.",
        "primary_kpi": "leads",
        "ai_image_prompt": "A solar panel installation at sunrise, no text, no logos.",
        "creative_concept_name": "The Predictability Pitch",
    }
    base.update(overrides)
    return base


def test_find_banned_words_detects_case_insensitive_hit():
    idea = _item(exact_copy={
        "headline": "Predictable power, finally.",
        "caption": "Our system GUARANTEED seamless power, easy payments, and ongoing support.",
        "hashtags": [],
    })
    hits = _find_banned_words(idea, ["guaranteed"])
    assert hits == ["guaranteed"]


def test_find_banned_words_scans_carousel_slides_too():
    idea = _item(carousel={"slides": [
        {"headline": "Step 1", "body": "This deal is 100% risk-free for you."},
    ]})
    hits = _find_banned_words(idea, ["risk-free"])
    assert hits == ["risk-free"]


def test_find_banned_words_clean_copy_returns_empty():
    idea = _item()
    assert _find_banned_words(idea, ["guaranteed", "risk-free", "certified"]) == []


def test_find_banned_words_no_list_returns_empty():
    idea = _item(exact_copy={"headline": "", "caption": "guaranteed results", "hashtags": []})
    assert _find_banned_words(idea, []) == []
    assert _find_banned_words(idea, None) == []


def test_collect_item_text_includes_video_and_carousel_fields():
    idea = _item(
        video_idea={"hook": "Meet Bola.", "cta": "DM us today", "talking_points": ["real story"], "scenes": []},
        carousel={"slides": [{"headline": "Slide one", "body": "slide body text"}]},
    )
    text = _collect_item_text(idea).lower()
    assert "meet bola" in text
    assert "dm us today" in text
    assert "slide body text" in text


# ── _validate_item_v2 wiring ─────────────────────────────────────────────────

def test_validate_item_v2_flags_banned_word_as_an_issue():
    idea = _item(exact_copy={
        "headline": "Predictable power, finally.",
        "caption": "This plan is guaranteed to eliminate downtime.",
        "hashtags": [],
    })
    issues = _validate_item_v2(idea, is_carousel=False, words_to_avoid=["guaranteed"])
    assert any("guaranteed" in issue.lower() for issue in issues)


def test_validate_item_v2_passes_when_no_banned_words_present():
    idea = _item()
    issues = _validate_item_v2(idea, is_carousel=False, words_to_avoid=["guaranteed", "certified"])
    assert issues == []


def test_validate_item_v2_without_words_to_avoid_is_a_no_op():
    idea = _item()
    issues = _validate_item_v2(idea, is_carousel=False, words_to_avoid=None)
    assert issues == []


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
