"""
Starter Pack (VSG-01 v3 §2.11) — the item-count guard, the label-overflow
bug found by actually looking at a rendered grid (a wrapped label was
silently truncated to its first line), and the structural guarantee that
the real product gets the exact same cell size/shadow as every generated
item rather than reading as elevated above the pack.
"""
import pytest

from app.agents.jane_ads.ad_formats.starter_pack import (
    FORMAT,
    InvalidItemCount,
    LabelNotOneLine,
    build_document,
    _item_prompt,
    _check_label_one_line,
)
from app.agents.jane_ads.ad_formats.legibility import check_legibility
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS

ITEM_URLS = ["https://example.com/i1.png", "https://example.com/i2.png", "https://example.com/i3.png"]
ITEM_LABELS = ["Zobo drink", "Jollof rice", "Ankara sandals"]
PRODUCT_URL = "https://example.com/product.png"


class TestFormatDefinition:
    def test_generate_asset_source_l2_l3_l4_requires_product_photo(self):
        assert FORMAT.asset_source == "generate"
        assert FORMAT.layers_used == "L2-L3-L4"
        assert FORMAT.requires == ["product_photo"]


class TestItemPrompt:
    def test_describes_a_single_item_flat_lay(self):
        prompt = _item_prompt("a bottle of zobo drink")
        assert "a bottle of zobo drink" in prompt
        assert "Overhead flat lay" in prompt
        assert "Nigerian everyday object" in prompt


class TestItemCountGuard:
    def test_below_minimum_rejected(self):
        with pytest.raises(InvalidItemCount):
            build_document(ITEM_URLS[:2], ITEM_LABELS[:2], PRODUCT_URL, "Product")  # 3 total

    def test_above_maximum_rejected(self):
        urls = [f"https://example.com/i{i}.png" for i in range(9)]
        labels = [f"Item {i}" for i in range(9)]
        with pytest.raises(InvalidItemCount):
            build_document(urls, labels, PRODUCT_URL, "Product")  # 10 total

    @pytest.mark.parametrize("n", [3, 4, 8])  # 4, 5, 9 total with the product
    def test_within_range_accepted(self, n):
        urls = [f"https://example.com/i{i}.png" for i in range(n)]
        labels = [f"Item {i}" for i in range(n)]
        doc = build_document(urls, labels, PRODUCT_URL, "Product")
        assert doc is not None

    def test_mismatched_url_and_label_counts_rejected(self):
        with pytest.raises(ValueError):
            build_document(ITEM_URLS, ITEM_LABELS[:2], PRODUCT_URL, "Product")


class TestLabelOneLineGuard:
    def test_regression_wrapped_label_is_rejected_not_truncated(self):
        """The real bug: an earlier version rendered only wrap_text's
        first line, so 'Our snack pack' silently printed as 'Our snack' —
        found by actually looking at a rendered grid, not assumed correct.

        Deliberately an extreme case (very long text, very narrow width)
        rather than a borderline one: wrap_text's line count depends on
        the real render font's metrics, which this machine's font-path
        fallback can't reproduce exactly (see _text_metrics.py's own
        docstring) — this wraps under any plausible font metric."""
        with pytest.raises(LabelNotOneLine):
            _check_label_one_line(
                "This label is deliberately extremely long and repeats itself "
                "over and over again so that it wraps no matter what font "
                "measures it because it simply will not stop going on and on",
                cell_width=60,
            )

    def test_short_label_passes(self):
        _check_label_one_line("Zobo drink", cell_width=301)  # does not raise

    def test_build_document_rejects_a_too_long_product_label(self):
        """Deliberately extreme rather than borderline — see
        test_regression_wrapped_label_is_rejected_not_truncated above for
        why (wrap_text's environment-dependent measurement)."""
        with pytest.raises(LabelNotOneLine):
            build_document(
                ITEM_URLS, ITEM_LABELS, PRODUCT_URL,
                "This product label is deliberately extremely long and repeats itself "
                "over and over again so that it wraps no matter what font measures it "
                "because it simply will not stop going on and on and on forever",
            )


class TestBuildDocument:
    def _doc(self, **kw):
        defaults = dict(
            item_image_urls=ITEM_URLS, item_labels=ITEM_LABELS,
            product_image_url=PRODUCT_URL, product_label="Our snack box",
        )
        defaults.update(kw)
        return build_document(**defaults)

    def test_every_item_including_product_gets_the_same_cell_size(self):
        """§2.11: 'consistent scale' — the product must not read as
        elevated above the pack."""
        doc = self._doc()
        products = [l for l in doc["layers"] if l["type"] == "composited_product"]
        sizes = {(l["width"], l["height"]) for l in products}
        assert len(sizes) == 1

    def test_every_item_including_product_gets_the_same_shadow(self):
        doc = self._doc()
        products = [l for l in doc["layers"] if l["type"] == "composited_product"]
        shadows = [l["shadow"] for l in products]
        assert all(s == shadows[0] for s in shadows)

    def test_product_defaults_to_a_middle_position_not_a_corner(self):
        doc = self._doc()
        products = sorted((l for l in doc["layers"] if l["type"] == "composited_product"), key=lambda l: l["z_index"])
        product_layer = next(l for l in products if l["url"] == PRODUCT_URL)
        first_layer = products[0]
        last_layer = products[-1]
        assert product_layer not in (first_layer, last_layer)

    def test_explicit_product_index_is_honoured(self):
        doc = self._doc(product_index=0)
        products = sorted((l for l in doc["layers"] if l["type"] == "composited_product"), key=lambda l: l["z_index"])
        assert products[0]["url"] == PRODUCT_URL

    def test_every_item_has_a_label_beneath_it_in_ink_quiet(self):
        doc = self._doc()
        labels = [l for l in doc["layers"] if l["type"] == "text"]
        assert len(labels) == 4  # 3 items + 1 product
        assert all(l["color"] == PLACEHOLDER_TOKENS["ink-quiet"] for l in labels)

    def test_grid_cells_do_not_overlap(self):
        doc = self._doc()
        products = [l for l in doc["layers"] if l["type"] == "composited_product"]
        for i, a in enumerate(products):
            for b in products[i + 1:]:
                x_overlap = a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
                y_overlap = a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]
                assert not (x_overlap and y_overlap)

    def test_passes_its_own_legibility_self_check(self):
        doc = self._doc()
        assert check_legibility(doc, PLACEHOLDER_TOKENS) == []
