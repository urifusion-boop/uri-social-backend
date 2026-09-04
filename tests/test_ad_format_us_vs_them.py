"""
Us vs Them (VSG-01 v3 §2.4) — build_document structural correctness, and
the strictest hard check in the library: the left ("them") column must
reject anything that looks like a named business.
"""
import pytest

from app.agents.jane_ads.ad_formats.us_vs_them import (
    FORMAT,
    BrandNameRejected,
    build_document,
    _looks_like_a_brand_name,
)
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS


class TestFormatDefinition:
    def test_drawn_asset_source_no_requirements(self):
        assert FORMAT.asset_source == "drawn"
        assert FORMAT.requires == []


class TestBrandNameGuard:
    """Real inputs, not synthetic strings picked to flatter the regex —
    actual Nigerian consumer brand names a user might genuinely type here."""

    @pytest.mark.parametrize("name", [
        "Jumia Food", "Chicken Republic", "Coca-Cola Nigeria", "Konga Express",
        "MTN Nigeria", "Domino's Pizza",
    ])
    def test_rejects_real_brand_names(self, name):
        assert _looks_like_a_brand_name(name) is True
        with pytest.raises(BrandNameRejected):
            build_document(rows=[("Delivery", name, "with us")])

    @pytest.mark.parametrize("method", [
        "buying at the market", "generator", "solar", "notebook", "guesswork",
        "guesswork vs measured fitting", "delivered to your door",
        "Order in 5 minutes",  # single sentence-initial capital, not a proper-noun run
        "cash only, on the spot",
    ])
    def test_allows_generic_methods(self, method):
        assert _looks_like_a_brand_name(method) is False
        doc = build_document(rows=[("Delivery", method, "with us")])
        assert doc is not None  # did not raise

    def test_trademark_symbol_always_rejected(self):
        assert _looks_like_a_brand_name("some product™") is True
        with pytest.raises(BrandNameRejected):
            build_document(rows=[("Feature", "some product™", "ours")])

    def test_only_the_them_column_is_checked_not_the_us_column(self):
        """A brand-sounding name in the RIGHT column (the seller's own
        offer) is not what this guard is for — only the left/them column
        names a method the seller doesn't control."""
        doc = build_document(rows=[("Delivery", "buying at the market", "Jumia Food style delivery")])
        assert doc is not None


class TestBuildDocument:
    def test_shared_row_labels_appear_on_both_sides(self):
        doc = build_document(rows=[
            ("Delivery", "go yourself", "delivered"),
            ("Price", "higher", "lower"),
        ])
        labels = [l["content"] for l in doc["layers"] if l.get("content") in ("Delivery", "Price")]
        assert labels.count("Delivery") == 2
        assert labels.count("Price") == 2

    def test_them_column_uses_ink_quiet_us_column_uses_ink(self):
        doc = build_document(rows=[("Delivery", "go yourself", "delivered")])
        them_layer = next(l for l in doc["layers"] if l.get("content") == "go yourself")
        us_layer = next(l for l in doc["layers"] if l.get("content") == "delivered")
        assert them_layer["color"] == PLACEHOLDER_TOKENS["ink-quiet"]
        assert us_layer["color"] == PLACEHOLDER_TOKENS["ink"]

    def test_us_column_has_a_field_background(self):
        doc = build_document(rows=[("Delivery", "go yourself", "delivered")])
        field_rects = [
            l for l in doc["layers"]
            if l["type"] == "shape" and l.get("fill_color") == PLACEHOLDER_TOKENS["field"]
        ]
        assert len(field_rects) == 1

    def test_default_labels_used_when_not_given(self):
        doc = build_document(rows=[("Delivery", "go yourself", "delivered")])
        headers = [l["content"] for l in doc["layers"] if l.get("content") in ("The old way", "With us")]
        assert "The old way" in headers
        assert "With us" in headers

    def test_custom_column_headers(self):
        doc = build_document(
            rows=[("Delivery", "go yourself", "delivered")],
            them_label="Buying at the market",
            us_label="Ordering with us",
        )
        headers = [l.get("content", "") for l in doc["layers"]]
        assert "Buying at the market" in headers
        assert "Ordering with us" in headers
