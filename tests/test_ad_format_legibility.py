"""
legibility.py (VSG-01 v3 §1.6's compression test, as an automated pre-
render check) — the checker's own logic against synthetic documents, plus
a cross-format regression guard: every shipped format's real
build_document() output must produce zero issues. This is what actually
caught the ink-quiet contrast defect and the Receipt/Us vs Them font-size
and hairline-stroke bugs fixed in the same commit as this test file.
"""
from app.agents.jane_ads.ad_formats.legibility import (
    MIN_BODY_FONT_SIZE,
    MIN_HAIRLINE_STROKE,
    IllegibleDocument,
    check_legibility,
    assert_legible,
)
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS
from app.agents.jane_ads.ad_formats import receipt, us_vs_them, borrowed_interface, review_card, day1_day30
from app.agents.jane_ads.ad_formats.brand_tokens import resolve_brand_tokens

import pytest


def _canvas(bg="#FFFFFF"):
    return {"width": 1080, "height": 1080, "background_color": bg}


class TestFontSize:
    def test_undersized_text_is_flagged(self):
        doc = {"canvas": _canvas(), "layers": [
            {"type": "text", "content": "hi", "x": 0, "y": 0, "font_size": 30, "color": "#1A1A1A"},
        ]}
        issues = check_legibility(doc)
        assert any(i.issue == "font_too_small" for i in issues)

    def test_compliant_text_is_not_flagged(self):
        doc = {"canvas": _canvas(), "layers": [
            {"type": "text", "content": "hi", "x": 0, "y": 0, "font_size": MIN_BODY_FONT_SIZE, "color": "#1A1A1A"},
        ]}
        assert check_legibility(doc) == []

    def test_empty_content_is_never_flagged(self):
        """Matches _render_text's own early-return-on-empty-content — a
        layer that never draws anything can't fail a legibility check."""
        doc = {"canvas": _canvas(), "layers": [
            {"type": "text", "content": "", "x": 0, "y": 0, "font_size": 10, "color": "#1A1A1A"},
        ]}
        assert check_legibility(doc) == []


class TestContrast:
    def test_low_contrast_ink_on_surface_is_flagged(self):
        doc = {"canvas": _canvas("#FAF7F2"), "layers": [
            {"type": "text", "content": "hi", "x": 0, "y": 0, "font_size": 44, "color": "#D8D2C8"},
        ]}
        issues = check_legibility(doc)
        assert any(i.issue == "contrast_too_low" for i in issues)

    def test_high_contrast_ink_on_surface_passes(self):
        doc = {"canvas": _canvas("#FAF7F2"), "layers": [
            {"type": "text", "content": "hi", "x": 0, "y": 0, "font_size": 44, "color": PLACEHOLDER_TOKENS["ink"]},
        ]}
        assert check_legibility(doc) == []

    def test_accent_role_uses_the_relaxed_4_5_bar_not_7(self):
        """A colour that clears 4.5:1 but not 7:1 against the canvas should
        pass when classified as `accent` (single emphasis) but would fail
        the stricter bar applied to ink/ink-quiet."""
        tokens = dict(PLACEHOLDER_TOKENS)
        doc = {"canvas": _canvas(tokens["surface"]), "layers": [
            {"type": "text", "content": "N500", "x": 0, "y": 0, "font_size": 44, "color": tokens["accent"]},
        ]}
        assert check_legibility(doc, tokens=tokens) == []

    def test_unrecognised_colour_gets_the_strict_7_1_bar(self):
        doc = {"canvas": _canvas("#FFFFFF"), "layers": [
            # a colour that clears 4.5:1 against white but not 7:1
            {"type": "text", "content": "hi", "x": 0, "y": 0, "font_size": 44, "color": "#767676"},
        ]}
        issues = check_legibility(doc)
        assert any(i.issue == "contrast_too_low" and "unrecognised" in i.detail for i in issues)


class TestBackgroundResolution:
    def test_a_shape_fill_behind_text_is_used_as_the_background(self):
        tokens = dict(PLACEHOLDER_TOKENS)
        doc = {"canvas": _canvas(tokens["surface"]), "layers": [
            {"type": "shape", "shape": "rect", "z_index": 1, "x": 0, "y": 0, "width": 1080, "height": 1080,
             "fill_color": tokens["field"]},
            {"type": "text", "z_index": 2, "content": "hi", "x": 10, "y": 10,
             "font_size": 44, "color": tokens["ink"]},
        ]}
        # ink on field (white) is well above 7:1 — should pass, proving the
        # shape's fill (not the canvas colour) was actually used.
        assert check_legibility(doc, tokens=tokens) == []

    def test_a_line_shape_does_not_establish_a_background(self):
        """A line has no fill area — it must not be treated as 'what's
        behind' the text, or every divider would falsely become the
        text's background colour."""
        tokens = dict(PLACEHOLDER_TOKENS)
        doc = {"canvas": _canvas(tokens["surface"]), "layers": [
            {"type": "shape", "shape": "line", "z_index": 1, "x1": 0, "y1": 0, "x2": 100, "y2": 0,
             "color": tokens["edge"], "stroke_width": 2},
            {"type": "text", "z_index": 2, "content": "hi", "x": 10, "y": 0,
             "font_size": 44, "color": tokens["ink"]},
        ]}
        # Falls through to the canvas colour (surface) — ink on surface
        # clears 7:1 easily, confirming the line was skipped rather than
        # treated as an (undefined-colour) background.
        assert check_legibility(doc, tokens=tokens) == []

    def test_text_directly_on_a_photo_layer_is_flagged(self):
        doc = {"canvas": _canvas(), "layers": [
            {"type": "composited_product", "z_index": 1, "url": "https://example.com/p.png",
             "x": 0, "y": 0, "width": 1080, "height": 1080},
            {"type": "text", "z_index": 2, "content": "hi", "x": 10, "y": 10,
             "font_size": 44, "color": "#FFFFFF"},
        ]}
        issues = check_legibility(doc)
        assert any(i.issue == "text_over_photography_without_scrim" for i in issues)

    def test_a_solid_scrim_above_a_photo_clears_the_flag(self):
        tokens = dict(PLACEHOLDER_TOKENS)
        doc = {"canvas": _canvas(), "layers": [
            {"type": "composited_product", "z_index": 1, "url": "https://example.com/p.png",
             "x": 0, "y": 0, "width": 1080, "height": 1080},
            {"type": "shape", "shape": "rect", "z_index": 2, "x": 0, "y": 0, "width": 1080, "height": 200,
             "fill_color": tokens["field"]},
            {"type": "text", "z_index": 3, "content": "hi", "x": 10, "y": 10,
             "font_size": 44, "color": tokens["ink"]},
        ]}
        assert check_legibility(doc, tokens=tokens) == []


class TestHairlineStroke:
    def test_stroke_below_the_floor_is_flagged(self):
        doc = {"canvas": _canvas(), "layers": [
            {"type": "shape", "shape": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0,
             "color": "#000000", "stroke_width": MIN_HAIRLINE_STROKE - 1},
        ]}
        issues = check_legibility(doc)
        assert any(i.issue == "hairline_stroke" for i in issues)

    def test_stroke_at_the_floor_passes(self):
        doc = {"canvas": _canvas(), "layers": [
            {"type": "shape", "shape": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0,
             "color": "#000000", "stroke_width": MIN_HAIRLINE_STROKE},
        ]}
        assert check_legibility(doc) == []

    def test_border_width_is_also_checked(self):
        doc = {"canvas": _canvas(), "layers": [
            {"type": "shape", "shape": "rounded_rect", "x": 0, "y": 0, "width": 10, "height": 10,
             "border_color": "#000000", "border_width": 1},
        ]}
        issues = check_legibility(doc)
        assert any(i.issue == "hairline_stroke" for i in issues)


class TestFontWeight:
    def test_weight_below_regular_is_flagged(self):
        doc = {"canvas": _canvas(), "layers": [
            {"type": "text", "content": "hi", "x": 0, "y": 0, "font_size": 44,
             "font_weight": 300, "color": "#1A1A1A"},
        ]}
        issues = check_legibility(doc)
        assert any(i.issue == "weight_too_light" for i in issues)


class TestAssertLegible:
    def test_raises_on_any_issue(self):
        doc = {"canvas": _canvas(), "layers": [
            {"type": "text", "content": "hi", "x": 0, "y": 0, "font_size": 10, "color": "#1A1A1A"},
        ]}
        with pytest.raises(IllegibleDocument):
            assert_legible(doc)

    def test_does_not_raise_on_a_clean_document(self):
        doc = {"canvas": _canvas("#FAF7F2"), "layers": [
            {"type": "text", "content": "hi", "x": 0, "y": 0, "font_size": 44, "color": PLACEHOLDER_TOKENS["ink"]},
        ]}
        assert_legible(doc)  # does not raise


class TestWholeLibraryPassesTheCompressionTest:
    """The regression guard this whole exercise was for: every shipped
    format's real build_document() output, checked with both the
    placeholder tokens and a real derived brand accent (since accent is
    the one role that varies and a per-brand hex could in principle fail
    contrast against a token set it was never checked against — though
    resolve_brand_tokens() itself already guards that at derivation time)."""

    def _all_documents(self, tokens):
        return {
            "receipt": receipt.build_document(
                items=[("Ankara fabric (6 yards) - premium quality lace blend", "N24,000"),
                       ("Tailoring", "N8,500")],
                total_label="Total", total_amount="N32,500", business_name="Adaeze Couture",
                delivery_line="Ready in 3 days", payment_line="Cash or transfer", tokens=tokens,
            ),
            "us_vs_them": us_vs_them.build_document(
                rows=[("Delivery", "go to the market yourself and carry everything home",
                       "delivered to your door same day"),
                      ("Price", "transport cost extra", "N2,500 flat")],
                tokens=tokens,
            ),
            "borrowed_interface_chat": borrowed_interface.build_document(
                turns=[("them", "Do you deliver to Lekki?", "9:14 AM"),
                       ("us", "Yes! Same-day delivery, N2,500 anywhere in Lekki", "9:15 AM")],
                tokens=tokens,
            ),
            "borrowed_interface_note": borrowed_interface.build_note_document(
                title="Order notes", meta_line="Edited just now",
                lines=["Medium size - N18,000", "Pay on delivery or transfer"], tokens=tokens,
            ),
            "review_card": review_card.build_document(
                product_image_url="https://example.com/p.png",
                quote="This cream cleared up my dry patches and the packaging looks so premium",
                attribution="Ngozi A.", star_rating=5,
                brand_logo_url="https://example.com/logo.png", tokens=tokens,
            ),
            "day1_day30": day1_day30.build_document(
                day1_image_url="https://example.com/1.png", day30_image_url="https://example.com/2.png",
                category="repair_or_restoration", tokens=tokens,
            ),
        }

    def test_every_format_passes_with_placeholder_tokens(self):
        for name, doc in self._all_documents(PLACEHOLDER_TOKENS).items():
            issues = check_legibility(doc, tokens=PLACEHOLDER_TOKENS)
            assert issues == [], f"{name} has legibility issues: {issues}"

    def test_every_format_passes_with_a_real_derived_brand_accent(self):
        brand_tokens = resolve_brand_tokens(["#0F766E"])
        for name, doc in self._all_documents(brand_tokens).items():
            issues = check_legibility(doc, tokens=brand_tokens)
            assert issues == [], f"{name} has legibility issues: {issues}"
