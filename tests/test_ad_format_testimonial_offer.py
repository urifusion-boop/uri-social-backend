"""
Testimonial + Offer (VSG-01 v3 §2.2) — both constructions (real person,
no-person-generated-scene), the permission-on-file hard check, and the
zone-overflow guards this format needed for the same reason
Problem/Solution did (text sharing a canvas with fixed-proportion zones).
"""
import pytest

from app.agents.jane_ads.ad_formats.testimonial_offer import (
    FORMAT,
    PermissionNotOnFile,
    ContentOverflowsZone,
    build_document,
    build_document_no_person,
    _scene_prompt,
    _check_proof_zone_fits,
    _check_offer_zone_fits,
)
from app.agents.jane_ads.ad_formats.legibility import check_legibility
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS
from app.agents.jane_ads.visual_slots import InvalidSlotValue

PHOTO_URL = "https://example.com/customer.png"
SCENE_URL = "https://example.com/scene.png"


class TestFormatDefinition:
    def test_upload_asset_source_requires_real_customer_photo(self):
        assert FORMAT.asset_source == "upload"
        assert FORMAT.requires == ["real_customer_photo"]
        assert FORMAT.layers_used == "L4"


class TestPermissionGuard:
    def test_false_is_rejected(self):
        with pytest.raises(PermissionNotOnFile):
            build_document(PHOTO_URL, "Great service", "Amaka", "Fast delivery", permission_on_file=False)

    def test_true_is_accepted(self):
        doc = build_document(PHOTO_URL, "Great service", "Amaka", "Fast delivery", permission_on_file=True)
        assert doc is not None

    def test_has_no_default_value(self):
        """Structural, not just documented — a caller cannot accidentally
        omit this and get an implicit yes."""
        import inspect
        param = inspect.signature(build_document).parameters["permission_on_file"]
        assert param.default is inspect.Parameter.empty


class TestScenePrompt:
    def test_no_people_in_frame_and_empty_lower_third(self):
        prompt = _scene_prompt("a tailoring workshop")
        assert "no people in frame" in prompt
        assert "clear empty space in the lower third" in prompt
        assert "a tailoring workshop" in prompt

    def test_rejects_a_setting_outside_the_controlled_vocabulary(self):
        with pytest.raises(InvalidSlotValue):
            _scene_prompt("Lekki, Lagos")


class TestBuildDocumentPersonPath(object):
    def _doc(self, **kw):
        defaults = dict(
            customer_photo_url=PHOTO_URL,
            quote="Great service, fast delivery every time",
            attribution="Amaka N.",
            offer_text="Same-day delivery across Lagos",
            permission_on_file=True,
        )
        defaults.update(kw)
        return build_document(**defaults)

    def test_photo_precedes_quote_precedes_offer(self):
        doc = self._doc()
        photo = next(l for l in doc["layers"] if l["type"] == "composited_product")
        quote = next(l for l in doc["layers"] if l.get("content", "").startswith("“"))
        offer = next(l for l in doc["layers"] if l.get("content") == "Same-day delivery across Lagos")
        assert photo["y"] < quote["y"] < offer["y"]

    def test_offer_zone_is_the_lower_third(self):
        doc = self._doc()
        offer = next(l for l in doc["layers"] if l.get("content") == "Same-day delivery across Lagos")
        height = doc["canvas"]["height"]
        assert offer["y"] >= (height * 2) // 3

    def test_price_uses_accent_offer_text_uses_ink(self):
        doc = self._doc(price_or_terms="Pay on delivery")
        offer = next(l for l in doc["layers"] if l.get("content") == "Same-day delivery across Lagos")
        price = next(l for l in doc["layers"] if l.get("content") == "Pay on delivery")
        assert offer["color"] == PLACEHOLDER_TOKENS["ink"]
        assert price["color"] == PLACEHOLDER_TOKENS["accent"]

    def test_divider_separates_quote_block_from_offer_band(self):
        """§2.2: the offer must read as different background weight, 'not
        merely a new paragraph' — both blocks are `field`, so a divider is
        required, not just spacing."""
        doc = self._doc()
        assert any(l.get("shape") == "line" for l in doc["layers"])

    def test_passes_its_own_legibility_self_check(self):
        doc = self._doc(price_or_terms="Pay on delivery or transfer")
        assert check_legibility(doc, PLACEHOLDER_TOKENS) == []

    def test_quote_block_overflow_is_rejected(self):
        """Tests the guard directly with manufactured heights rather than
        routing through wrap_text: how many lines a string wraps to
        depends on the real render font's metrics, which this machine's
        font-path fallback can't reproduce exactly (see _text_metrics.py's
        own docstring)."""
        with pytest.raises(ContentOverflowsZone):
            _check_proof_zone_fits(quote_block_height=800, proof_zone_height=720)

    def test_quote_block_within_budget_is_accepted(self):
        _check_proof_zone_fits(quote_block_height=300, proof_zone_height=720)  # does not raise


class TestBuildDocumentNoPersonPath:
    def _doc(self, **kw):
        defaults = dict(
            scene_image_url=SCENE_URL,
            quote="Every outfit arrives exactly as measured",
            offer_text="Custom tailoring, ready in 5 days",
        )
        defaults.update(kw)
        return build_document_no_person(**defaults)

    def test_scene_is_an_ai_generated_background_not_a_person_photo(self):
        doc = self._doc()
        bg = next(l for l in doc["layers"] if l["type"] == "ai_generated_background")
        assert bg["url"] == SCENE_URL

    def test_optional_product_image_is_composited_when_given(self):
        doc = self._doc(product_image_url="https://example.com/product.png")
        product = next(l for l in doc["layers"] if l["type"] == "composited_product")
        assert product["url"] == "https://example.com/product.png"

    def test_no_product_image_omits_the_composited_product_layer(self):
        doc = self._doc()
        assert not any(l["type"] == "composited_product" for l in doc["layers"])

    def test_quote_sits_on_a_field_block_within_the_proof_zone(self):
        doc = self._doc()
        height = doc["canvas"]["height"]
        proof_zone_height = (height * 2) // 3
        quote_block = next(
            l for l in doc["layers"]
            if l["type"] == "shape" and l.get("fill_color") == PLACEHOLDER_TOKENS["field"]
        )
        assert quote_block["y"] + quote_block["height"] == proof_zone_height

    def test_passes_its_own_legibility_self_check(self):
        doc = self._doc(price_or_terms="From N15,000")
        assert check_legibility(doc, PLACEHOLDER_TOKENS) == []

    def test_offer_zone_overflow_is_rejected(self):
        """Same reasoning as test_quote_block_overflow_is_rejected — the
        guard is tested directly rather than through wrap_text's
        environment-dependent line count."""
        with pytest.raises(ContentOverflowsZone):
            _check_offer_zone_fits(offer_content_height=500, offer_zone_height=360)

    def test_offer_zone_within_budget_is_accepted(self):
        _check_offer_zone_fits(offer_content_height=200, offer_zone_height=360)  # does not raise
