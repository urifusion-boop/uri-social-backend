"""
Content Calendar V2 — deterministic guardrail checks.

Covers bugs found by live end-to-end verification of real 30-day generation
runs:

1. A brand's words_to_avoid was only ever a prompt instruction — the model
   used a banned word ("guaranteed") anyway under pressure to satisfy other
   requirements. Per the PRD's own rule (code enforces hard constraints,
   the LLM handles creative judgment), this is now a deterministic,
   case-insensitive check wired into the existing validation-failure ->
   regenerate loop, not just prompt wording.
2. A series name template with an unresolved placeholder ("The [Industry]
   Myth") shipped verbatim in a real generated plan, because nothing ever
   substituted the bracket before it reached the model or the final item.
3. The generic-AI-headline check (ANTI_BORING_PHRASES) only matched fixed
   bigrams ("unlock the", "discover how"...) and missed ordinary conjugations
   of the same opener verb — a real plan shipped "Unlocking the Production
   Process" and "Discover Our Pricing Edge" untouched.
4. series_name was carried straight from the model's candidate output with no
   check that it actually recurred — a real plan carried 6 distinct series
   names, each used exactly once, which the prompt itself says not to do.
"""
from app.agents.content_calendar_v2.services.content_calendar_v2_service import (
    _anti_boring_check,
    _collect_item_text,
    _find_banned_words,
    _recurring_series_names,
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


# ── _anti_boring_check — generic-AI-headline detection ──────────────────────

def test_anti_boring_check_catches_the_live_bug_unlocking():
    items = [_item(title="Unlocking the Production Process", hook="See how it all comes together.")]
    flagged = _anti_boring_check(items, brand_name="Docerity")
    assert 0 in flagged and "unlocking" in flagged[0].lower()


def test_anti_boring_check_catches_the_live_bug_discover_our():
    items = [_item(title="Why Pay More? Discover Our Pricing Edge")]
    flagged = _anti_boring_check(items, brand_name="Docerity")
    assert 0 in flagged


def test_anti_boring_check_catches_other_conjugations_of_the_same_verbs():
    items = [
        _item(title="Discovering What Makes Us Different"),
        _item(title="Master the Art of Predictable Power"),
        _item(title="Revolutionizing How SMEs Handle Power"),
    ]
    flagged = _anti_boring_check(items, brand_name="Docerity")
    assert set(flagged.keys()) == {0, 1, 2}


def test_anti_boring_check_does_not_false_positive_on_the_word_mid_sentence():
    # The whole reason these stayed fixed phrases / opener-only instead of a
    # bare-word-anywhere check: "discover" and "master" are ordinary English
    # words that show up in completely normal, non-generic sentences.
    items = [
        _item(title="Your skin isn't necessarily dry. Your routine may just be fighting itself.",
              hook="Customers discover this the hard way — usually after switching products."),
        _item(title="What the electrician masters in year one"),
    ]
    flagged = _anti_boring_check(items, brand_name="Docerity")
    assert flagged == {}


def test_anti_boring_check_still_catches_the_original_fixed_phrase_bugs():
    items = [
        _item(title="Fine.", exact_copy={"headline": "Fine.", "caption": "We are excited to announce our new plan.", "hashtags": []}),
        _item(title="At Docerity, we believe in better tech.", hook=""),
    ]
    flagged = _anti_boring_check(items, brand_name="Docerity")
    assert set(flagged.keys()) == {0, 1}


# ── _recurring_series_names ──────────────────────────────────────────────────

def test_recurring_series_names_keeps_only_names_used_at_least_twice():
    # The exact shape of the live bug: 6 distinct series names, each used once.
    names = ["Ask Docerity", "Founder Truths", "Before You Buy", "Customer Question of the Week", "The Tech & SaaS Myth", "Would You Choose This?"]
    assert _recurring_series_names(names) == set()


def test_recurring_series_names_keeps_names_that_genuinely_recur():
    names = ["Ask Docerity", "Founder Truths", "Ask Docerity", None, "Ask Docerity", "Founder Truths"]
    assert _recurring_series_names(names) == {"Ask Docerity", "Founder Truths"}


def test_recurring_series_names_ignores_none_and_empty():
    assert _recurring_series_names([None, "", None]) == set()
    assert _recurring_series_names([]) == set()


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
