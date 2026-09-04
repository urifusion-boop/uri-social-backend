"""
Borrowed Interface (VSG-01 v3 §2.6) — build_document/build_note_document
structural correctness, the turn-count hard check, and the text-wrap helper
that exists specifically because an earlier version of this format's bubbles
silently overflowed their own edges on a realistic (not just a short test-
string) message — see the wrap tests below.
"""
import pytest

from app.agents.jane_ads.ad_formats.borrowed_interface import (
    FORMAT,
    TooManyTurns,
    build_document,
    build_note_document,
    _tint,
    _wrap_text,
)
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS


class TestFormatDefinition:
    def test_drawn_asset_source_no_requirements(self):
        assert FORMAT.asset_source == "drawn"
        assert FORMAT.requires == []
        assert FORMAT.layers_used == "L4"


class TestTurnCountGuard:
    def test_four_turns_allowed(self):
        doc = build_document(turns=[("them", "hi", "9:00"), ("us", "hey", "9:01"),
                                     ("them", "ok", "9:02"), ("us", "sure", "9:03")])
        assert doc is not None

    def test_five_turns_rejected(self):
        with pytest.raises(TooManyTurns):
            build_document(turns=[("them", "a", "t")] * 5)

    def test_invalid_speaker_rejected(self):
        with pytest.raises(ValueError):
            build_document(turns=[("customer", "hi", "9:00")])


class TestTextWrap:
    """The bug this guards against: a fixed-width bubble with unwrapped text
    overflows past its own edge (and the canvas) the moment a message is
    longer than a couple of words — found by rendering a realistic message,
    not a short synthetic one.

    These tests inject a fake `measure` (a fixed width per character)
    instead of relying on the real font-file measurement `_wrap_text`
    defaults to in production — the real one depends on a font file being
    present at DocumentRendererService's hardcoded path, which is true on
    the real (Linux) deployment target but not guaranteed on every machine
    that runs pytest, and Pillow's own fallback silently ignores font_size
    rather than raising, so a test built on the real measurer would pass or
    fail depending on what happens to be installed rather than on whether
    the wrap algorithm itself is correct."""

    @staticmethod
    def _char_width(s: str) -> int:
        return len(s) * 20

    def test_short_text_is_a_single_line(self):
        lines = _wrap_text("hi there", max_width=2000, font_size=44, measure=self._char_width)
        assert lines == ["hi there"]

    def test_long_text_wraps_into_multiple_lines(self):
        long_message = "Yes! Same-day delivery, N2,500 anywhere in Lekki, order now"
        lines = _wrap_text(long_message, max_width=400, font_size=44, measure=self._char_width)
        assert len(lines) > 1
        for line in lines:
            assert self._char_width(line) <= 400

    def test_wrapping_preserves_every_word(self):
        message = "How much for the medium size"
        lines = _wrap_text(message, max_width=200, font_size=44, measure=self._char_width)
        assert " ".join(lines).split() == message.split()

    def test_real_font_measurement_is_wired_by_default(self):
        """Not a wrap-correctness test — just confirms the production
        default path (no `measure` override) still calls through to the
        real _text_width rather than silently no-op'ing."""
        lines = _wrap_text("hi", max_width=2000, font_size=44)
        assert lines == ["hi"]


class TestBuildDocument:
    def test_them_bubble_is_left_aligned_us_bubble_is_right_aligned(self):
        doc = build_document(turns=[("them", "hi", "9:00"), ("us", "hey", "9:01")])
        bubbles = [l for l in doc["layers"] if l["type"] == "shape" and l["shape"] == "rounded_rect"]
        assert len(bubbles) == 2
        them_bubble, us_bubble = bubbles
        assert them_bubble["x"] == 72  # margin
        assert us_bubble["x"] + us_bubble["width"] == doc["canvas"]["width"] - 72

    def test_us_bubble_uses_tinted_accent_not_field(self):
        doc = build_document(turns=[("us", "hey", "9:01")])
        bubble = next(l for l in doc["layers"] if l.get("shape") == "rounded_rect")
        assert bubble["fill_color"] != PLACEHOLDER_TOKENS["field"]
        assert bubble["fill_color"] != PLACEHOLDER_TOKENS["accent"]  # tinted, not literal

    def test_them_bubble_uses_field_with_edge_border(self):
        doc = build_document(turns=[("them", "hi", "9:00")])
        bubble = next(l for l in doc["layers"] if l.get("shape") == "rounded_rect")
        assert bubble["fill_color"] == PLACEHOLDER_TOKENS["field"]
        assert bubble["border_color"] == PLACEHOLDER_TOKENS["edge"]

    def test_all_body_text_meets_the_42px_legibility_floor(self):
        doc = build_document(turns=[("them", "hi", "9:00"), ("us", "hey", "9:01")])
        for layer in doc["layers"]:
            if layer["type"] == "text":
                assert layer["font_size"] >= 42

    def test_timestamp_follows_its_own_bubble_alignment(self):
        doc = build_document(turns=[("us", "hey", "9:01")])
        ts = next(l for l in doc["layers"] if l.get("content") == "9:01")
        assert ts.get("text_align") == "ra"

    def test_no_functional_control_layer_types_are_ever_produced(self):
        """Enforced by construction (§2.6 point 2) — this module has no
        button/icon layer type in its vocabulary at all."""
        doc = build_document(turns=[("them", "hi", "9:00"), ("us", "hey", "9:01")])
        assert {l["type"] for l in doc["layers"]} <= {"shape", "text"}


class TestBuildNoteDocument:
    def test_title_uses_headline_tier_font_size(self):
        doc = build_note_document(title="Order notes", lines=["a line"])
        title_layer = next(l for l in doc["layers"] if l.get("content") == "Order notes")
        assert title_layer["font_size"] >= 72

    def test_card_background_is_the_lowest_z_index(self):
        doc = build_note_document(title="T", lines=["a", "b"])
        card = next(l for l in doc["layers"] if l.get("shape") == "rounded_rect")
        assert card["z_index"] == min(l["z_index"] for l in doc["layers"])

    def test_lines_render_without_title_or_meta(self):
        doc = build_note_document(lines=["Cash on delivery only"])
        texts = [l.get("content", "") for l in doc["layers"]]
        assert "Cash on delivery only" in texts


class TestTint:
    def test_zero_amount_returns_original_color(self):
        assert _tint("#CD1B78", "#FFFFFF", 0.0) == "#CD1B78"

    def test_full_amount_returns_target_color(self):
        assert _tint("#CD1B78", "#FFFFFF", 1.0) == "#FFFFFF"

    def test_partial_tint_is_not_the_platforms_literal_brand_green(self):
        # Guards §2.6 point 3: never hardcode WhatsApp's own bubble colour.
        tinted = _tint(PLACEHOLDER_TOKENS["accent"], "#FFFFFF", 0.85)
        assert tinted not in ("#DCF8C6", "#005C4B")
