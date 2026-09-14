"""
Humour / Cartoon (VSG-01 v3 §2.12) — the human-review gate this format
has and no other format in the library does, and the by-construction
guarantee that this format never composites caption text over the joke
("punchline in the image rather than the caption").
"""
import inspect

import pytest

from app.agents.jane_ads.ad_formats.humour_cartoon import (
    FORMAT,
    HumanReviewRequired,
    build_document,
    _illustration_prompt,
)
from app.agents.jane_ads.ad_formats.legibility import check_legibility
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS
from app.agents.jane_ads.visual_slots import InvalidSlotValue

ILLUSTRATION_URL = "https://example.com/cartoon.png"


class TestFormatDefinition:
    def test_generate_only_asset_source_l2_l4(self):
        assert FORMAT.asset_source == "generate"
        assert FORMAT.layers_used == "L2-L4"
        assert FORMAT.requires == []


class TestIllustrationPrompt:
    def test_describes_a_single_panel_cartoon(self):
        prompt = _illustration_prompt(
            "a customer waiting so long a plant grows on their doorstep",
            "a residential estate gate",
        )
        assert "Single-panel cartoon illustration" in prompt
        assert "one single clear visual punchline" in prompt
        assert "a residential estate gate" in prompt

    def test_rejects_a_setting_outside_the_controlled_vocabulary(self):
        with pytest.raises(InvalidSlotValue):
            _illustration_prompt("a joke", "Lekki, Lagos")


class TestHumanReviewGuard:
    def test_false_is_rejected(self):
        with pytest.raises(HumanReviewRequired):
            build_document(ILLUSTRATION_URL, human_reviewed=False)

    def test_true_is_accepted(self):
        doc = build_document(ILLUSTRATION_URL, human_reviewed=True)
        assert doc is not None

    def test_has_no_default_value(self):
        """Structural, not just documented — the one format in this
        library where §5's 'no human sees the asset before it ships'
        framing is explicitly overridden."""
        param = inspect.signature(build_document).parameters["human_reviewed"]
        assert param.default is inspect.Parameter.empty


class TestBuildDocument:
    def test_illustration_fills_the_whole_canvas(self):
        doc = build_document(ILLUSTRATION_URL, human_reviewed=True)
        illustration = next(l for l in doc["layers"] if l["type"] == "ai_generated_background")
        assert illustration["width"] == doc["canvas"]["width"]
        assert illustration["height"] == doc["canvas"]["height"]

    def test_no_caption_text_is_ever_composited(self):
        """§2.12: 'punchline in the image rather than the caption.'
        Enforced by construction — this module has no text layer type in
        its vocabulary at all."""
        doc = build_document(ILLUSTRATION_URL, human_reviewed=True)
        assert not any(l["type"] == "text" for l in doc["layers"])

    def test_brand_logo_is_optional(self):
        without_logo = build_document(ILLUSTRATION_URL, human_reviewed=True)
        with_logo = build_document(ILLUSTRATION_URL, human_reviewed=True, brand_logo_url="https://example.com/logo.png")
        assert not any(l["type"] == "brand_asset" for l in without_logo["layers"])
        assert any(l["type"] == "brand_asset" for l in with_logo["layers"])

    def test_brand_logo_sits_in_a_corner_not_over_the_centre(self):
        doc = build_document(ILLUSTRATION_URL, human_reviewed=True, brand_logo_url="https://example.com/logo.png")
        logo = next(l for l in doc["layers"] if l["type"] == "brand_asset")
        width, height = doc["canvas"]["width"], doc["canvas"]["height"]
        assert logo["x"] > width * 0.5
        assert logo["y"] > height * 0.5

    def test_passes_its_own_legibility_self_check(self):
        doc = build_document(ILLUSTRATION_URL, human_reviewed=True)
        assert check_legibility(doc, PLACEHOLDER_TOKENS) == []
