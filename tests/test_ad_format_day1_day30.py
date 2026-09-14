"""
Day 1 → Day 30 (VSG-01 v3 §2.9) — build_document structural correctness and
the strictest exclusion in the library: this format is excluded outright
for health, weight, skin and appearance, enforced two ways (a closed
category allowlist, plus a defense-in-depth scan of the labels themselves).
"""
import pytest

from app.agents.jane_ads.ad_formats.day1_day30 import (
    FORMAT,
    DisallowedCategory,
    build_document,
)
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS

DAY1_URL = "https://example.com/day1.png"
DAY30_URL = "https://example.com/day30.png"


class TestFormatDefinition:
    def test_upload_asset_source_requires_product_photo(self):
        assert FORMAT.asset_source == "upload"
        assert FORMAT.requires == ["product_photo"]
        assert FORMAT.layers_used == "L4"

    def test_requires_isolation_per_section_6(self):
        """§6 names SEED-078 (this format) explicitly as requiring the
        usage cap — missed on first ship, found rereading §6 later."""
        assert FORMAT.requires_isolation is True


class TestCategoryAllowlist:
    @pytest.mark.parametrize("category", [
        "installation_or_construction_progress",
        "space_before_after_fitting",
        "repair_or_restoration",
        "training_cohort",
    ])
    def test_permitted_categories_accepted(self, category):
        doc = build_document(DAY1_URL, DAY30_URL, category=category)
        assert doc is not None

    @pytest.mark.parametrize("category", [
        "weight_loss", "skin_care", "fitness_transformation", "before_after",
        "", "REPAIR_OR_RESTORATION",  # not case-normalised — must match exactly
    ])
    def test_everything_else_rejected(self, category):
        with pytest.raises(DisallowedCategory):
            build_document(DAY1_URL, DAY30_URL, category=category)


class TestLabelGuard:
    """A permitted category doesn't excuse a body-related caption slipped
    into the labels — the exclusion is on content, not just on the
    category field's literal value."""

    @pytest.mark.parametrize("label", [
        "Before my skin treatment", "After losing weight", "My acne journey",
    ])
    def test_health_appearance_labels_rejected_even_under_a_permitted_category(self, label):
        with pytest.raises(DisallowedCategory):
            build_document(DAY1_URL, DAY30_URL, category="repair_or_restoration", day1_label=label)

        with pytest.raises(DisallowedCategory):
            build_document(DAY1_URL, DAY30_URL, category="repair_or_restoration", day30_label=label)

    def test_ordinary_labels_pass(self):
        doc = build_document(
            DAY1_URL, DAY30_URL, category="space_before_after_fitting",
            day1_label="Before fit-out", day30_label="After fit-out",
        )
        assert doc is not None


class TestBuildDocument:
    def test_default_labels_are_day1_and_day30(self):
        doc = build_document(DAY1_URL, DAY30_URL, category="training_cohort")
        labels = [l.get("content") for l in doc["layers"] if l["type"] == "text"]
        assert "Day 1" in labels
        assert "Day 30" in labels

    def test_two_composited_product_layers_equal_width(self):
        doc = build_document(DAY1_URL, DAY30_URL, category="training_cohort")
        panels = [l for l in doc["layers"] if l["type"] == "composited_product"]
        assert len(panels) == 2
        assert panels[0]["width"] == panels[1]["width"]
        assert panels[0]["url"] == DAY1_URL
        assert panels[1]["url"] == DAY30_URL

    def test_panels_do_not_overlap(self):
        doc = build_document(DAY1_URL, DAY30_URL, category="training_cohort")
        panels = sorted(
            (l for l in doc["layers"] if l["type"] == "composited_product"),
            key=lambda l: l["x"],
        )
        left, right = panels
        assert left["x"] + left["width"] <= right["x"]

    def test_no_arrow_glow_or_enhancement_layer_types_are_ever_produced(self):
        """Enforced by construction (§2.9) — this module's only layer
        types are composited_product, a plain divider shape, and text."""
        doc = build_document(DAY1_URL, DAY30_URL, category="training_cohort")
        assert {l["type"] for l in doc["layers"]} <= {"composited_product", "shape", "text"}

    def test_labels_use_ink_quiet_and_are_horizontally_centred(self):
        doc = build_document(DAY1_URL, DAY30_URL, category="training_cohort")
        label_layers = [l for l in doc["layers"] if l.get("content") in ("Day 1", "Day 30")]
        assert len(label_layers) == 2
        for layer in label_layers:
            assert layer["color"] == PLACEHOLDER_TOKENS["ink-quiet"]
            assert layer["text_align"] == "ma"

    def test_all_label_text_meets_the_42px_legibility_floor(self):
        doc = build_document(DAY1_URL, DAY30_URL, category="training_cohort")
        for layer in doc["layers"]:
            if layer["type"] == "text":
                assert layer["font_size"] >= 42

    def test_divider_uses_edge_token(self):
        doc = build_document(DAY1_URL, DAY30_URL, category="training_cohort")
        divider = next(l for l in doc["layers"] if l.get("shape") == "line")
        assert divider["color"] == PLACEHOLDER_TOKENS["edge"]
