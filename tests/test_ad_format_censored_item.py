"""
The Censored Item (VSG-01 v3 §2.10) — the price-obscuring guard, the
structural requirement that a reveal actually be stated, the redaction
bar itself (a plain `ink` rect, never generated), and the recomposite
path's layering (generated background behind the real, unmodified
product) — visually confirmed correct via a real render before writing
these structural assertions.
"""
import inspect

import pytest

from app.agents.jane_ads.ad_formats.censored_item import (
    FORMAT,
    ObscuresPriceRejected,
    ContentOverflowsZone,
    build_document,
    _background_prompt,
    _check_reveal_fits,
)
from app.agents.jane_ads.ad_formats.legibility import check_legibility
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS

PRODUCT_URL = "https://example.com/product.png"
BACKGROUND_URL = "https://example.com/scene.png"


class TestFormatDefinition:
    def test_upload_asset_source_requires_product_photo_and_isolation(self):
        assert FORMAT.asset_source == "upload"
        assert FORMAT.requires == ["product_photo"]
        assert FORMAT.layers_used == "L4"
        assert FORMAT.requires_isolation is True  # §6 names SEED-083 explicitly


class TestBackgroundPrompt:
    def test_matches_section_2_10_scene_treatment_verbatim(self):
        prompt = _background_prompt()
        assert "Dramatic single-source side lighting" in prompt
        assert "studio product photography" in prompt


class TestObscuresPriceGuard:
    @pytest.mark.parametrize("description", [
        "the price", "the cost", "₦ amount", "the naira fee", "how much it costs",
    ])
    def test_price_language_rejected(self, description):
        with pytest.raises(ObscuresPriceRejected):
            build_document(PRODUCT_URL, 100, 100, 200, 100, "Reveals soon", description)

    @pytest.mark.parametrize("description", [
        "the new lid design", "the packaging", "the logo placement",
    ])
    def test_non_price_descriptions_allowed(self, description):
        doc = build_document(PRODUCT_URL, 100, 100, 200, 100, "Reveals soon", description)
        assert doc is not None


class TestRevealTextRequired:
    def test_has_no_default_value(self):
        """§2.10: 'redacting where nothing is actually revealed' is
        prohibited — structural, not just documented: reveal_text has no
        default, confirmed via inspect.signature."""
        param = inspect.signature(build_document).parameters["reveal_text"]
        assert param.default is inspect.Parameter.empty


class TestRevealZoneOverflowGuard:
    def test_overflow_is_rejected(self):
        """Tests the guard directly with a manufactured height rather than
        routing through wrap_text, whose line count depends on the real
        render font's metrics this machine's font-path fallback can't
        reproduce exactly (see _text_metrics.py's own docstring)."""
        with pytest.raises(ContentOverflowsZone):
            _check_reveal_fits(reveal_content_height=400, reveal_zone_height=238)

    def test_within_budget_is_accepted(self):
        _check_reveal_fits(reveal_content_height=150, reveal_zone_height=238)  # does not raise


class TestBuildDocument:
    def _doc(self, **kw):
        defaults = dict(
            product_image_url=PRODUCT_URL,
            obscure_x=340, obscure_y=350, obscure_width=400, obscure_height=180,
            reveal_text="Reveals 20 September",
            what_is_obscured="the new lid design",
        )
        defaults.update(kw)
        return build_document(**defaults)

    def test_redaction_bar_is_a_plain_ink_rect_at_the_given_position(self):
        doc = self._doc()
        bar = next(
            l for l in doc["layers"]
            if l["type"] == "shape" and l.get("fill_color") == PLACEHOLDER_TOKENS["ink"]
        )
        assert (bar["x"], bar["y"], bar["width"], bar["height"]) == (340, 350, 400, 180)

    def test_redaction_bar_renders_above_the_product_photo(self):
        doc = self._doc()
        bar = next(l for l in doc["layers"] if l.get("fill_color") == PLACEHOLDER_TOKENS["ink"])
        product = next(l for l in doc["layers"] if l["type"] == "composited_product")
        assert bar["z_index"] > product["z_index"]

    def test_no_background_layer_when_none_given(self):
        doc = self._doc()
        assert not any(l["type"] == "ai_generated_background" for l in doc["layers"])

    def test_background_layer_present_and_beneath_the_product_when_given(self):
        doc = self._doc(background_image_url=BACKGROUND_URL)
        background = next(l for l in doc["layers"] if l["type"] == "ai_generated_background")
        product = next(l for l in doc["layers"] if l["type"] == "composited_product")
        assert background["url"] == BACKGROUND_URL
        assert background["z_index"] < product["z_index"]

    def test_reveal_text_uses_accent_on_a_field_band(self):
        doc = self._doc()
        reveal = next(l for l in doc["layers"] if l.get("content") == "Reveals 20 September")
        band = next(
            l for l in doc["layers"]
            if l["type"] == "shape" and l.get("fill_color") == PLACEHOLDER_TOKENS["field"]
        )
        assert reveal["color"] == PLACEHOLDER_TOKENS["accent"]
        assert band["y"] < reveal["y"]

    def test_passes_its_own_legibility_self_check(self):
        doc = self._doc()
        assert check_legibility(doc, PLACEHOLDER_TOKENS) == []
