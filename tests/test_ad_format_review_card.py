"""
Review Card (VSG-01 v3 §2.1) — build_document structural correctness, the
two hard checks (star rating validity, no timed-outcome health/appearance
claims), and the product-zone/field-zone sizing that previously let a
realistic 18-word quote overlap the brand logo (see the module docstring
and the fixed-content-size tests below).
"""
import pytest

from app.agents.jane_ads.ad_formats.review_card import (
    FORMAT,
    InvalidStarRating,
    TimedOutcomeClaim,
    build_document,
)
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS

PRODUCT_URL = "https://example.com/product.png"


class TestFormatDefinition:
    def test_upload_asset_source_requires_product_photo(self):
        assert FORMAT.asset_source == "upload"
        assert FORMAT.requires == ["product_photo"]
        assert FORMAT.layers_used == "L4"


class TestStarRatingGuard:
    @pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
    def test_valid_ratings_accepted(self, rating):
        doc = build_document(PRODUCT_URL, "Great product, fast delivery", "Chidi O.", star_rating=rating)
        assert doc is not None

    def test_none_omits_the_star_row_entirely(self):
        doc = build_document(PRODUCT_URL, "Great product, fast delivery", "Chidi O.", star_rating=None)
        stars = [
            l for l in doc["layers"]
            if l["type"] == "text" and l.get("content") and set(l["content"]) <= {"★", "☆"}
        ]
        assert stars == []

    @pytest.mark.parametrize("bad_rating", [0, 6, -1, 3.5, "5"])
    def test_invalid_ratings_rejected(self, bad_rating):
        with pytest.raises(InvalidStarRating):
            build_document(PRODUCT_URL, "Great product, fast delivery", "Chidi O.", star_rating=bad_rating)


class TestTimedOutcomeClaimGuard:
    @pytest.mark.parametrize("quote", [
        "My skin cleared up in 2 weeks, I'm so happy",
        "Lost weight within a month of using this",
        "The acne was gone after 10 days",
        "My complexion changed in just 3 days",
    ])
    def test_rejects_timed_health_appearance_claims(self, quote):
        with pytest.raises(TimedOutcomeClaim):
            build_document(PRODUCT_URL, quote, "Amaka N.")

    @pytest.mark.parametrize("quote", [
        "Fast delivery, great quality, will buy again",
        "This cream cleared up my dry patches nicely",
        "In 2 weeks I already recommended it to my sister",  # time phrase, no outcome word
        "My skin feels amazing, best purchase this year",  # outcome word, no time phrase
    ])
    def test_allows_ordinary_reviews(self, quote):
        doc = build_document(PRODUCT_URL, quote, "Amaka N.")
        assert doc is not None


class TestBuildDocument:
    def test_quote_is_wrapped_in_curly_quotation_marks(self):
        doc = build_document(PRODUCT_URL, "Great product", "Chidi O.")
        quote_layer = next(l for l in doc["layers"] if l.get("content", "").startswith("“"))
        assert quote_layer["content"] == "“Great product”"

    def test_attribution_uses_ink_quiet(self):
        doc = build_document(PRODUCT_URL, "Great product", "Chidi O.")
        attribution_layer = next(l for l in doc["layers"] if l.get("content") == "Chidi O.")
        assert attribution_layer["color"] == PLACEHOLDER_TOKENS["ink-quiet"]

    def test_star_row_uses_accent(self):
        doc = build_document(PRODUCT_URL, "Great product", "Chidi O.", star_rating=5)
        star_layer = next(l for l in doc["layers"] if l.get("content") == "★★★★★")
        assert star_layer["color"] == PLACEHOLDER_TOKENS["accent"]

    def test_product_photo_is_a_composited_product_layer(self):
        doc = build_document(PRODUCT_URL, "Great product", "Chidi O.")
        product_layers = [l for l in doc["layers"] if l["type"] == "composited_product"]
        assert len(product_layers) == 1
        assert product_layers[0]["url"] == PRODUCT_URL

    def test_product_zone_stays_within_the_55_to_60_percent_band(self):
        for quote in ("Fast delivery, great quality", "a" * 5, "This cream cleared up my dry patches and the packaging looks so premium for the price"):
            doc = build_document(PRODUCT_URL, quote, "Chidi O.", star_rating=5, brand_logo_url=PRODUCT_URL)
            product = next(l for l in doc["layers"] if l["type"] == "composited_product")
            height = doc["canvas"]["height"]
            assert int(height * 0.55) <= product["height"] <= int(height * 0.60)

    def test_long_quote_with_logo_does_not_overlap_attribution(self):
        """Regression test for the real bug found by rendering: a fixed
        product-zone split plus a fixed-position logo let a met 18-word
        quote's wrapped attribution line collide with the logo."""
        doc = build_document(
            PRODUCT_URL,
            "This cream cleared up my dry patches and the packaging looks so premium for the price",
            "Ngozi A.",
            star_rating=5,
            brand_logo_url=PRODUCT_URL,
        )
        attribution = next(l for l in doc["layers"] if l.get("content") == "Ngozi A.")
        logo = next(l for l in doc["layers"] if l["type"] == "brand_asset")
        assert logo["y"] >= attribution["y"] + 44  # attribution's own font size — no overlap

    def test_no_logo_omits_the_brand_asset_layer(self):
        doc = build_document(PRODUCT_URL, "Great product", "Chidi O.")
        assert not any(l["type"] == "brand_asset" for l in doc["layers"])

    def test_all_body_text_meets_the_42px_legibility_floor(self):
        doc = build_document(PRODUCT_URL, "Great product, fast and reliable", "Chidi O.", star_rating=4)
        for layer in doc["layers"]:
            if layer["type"] == "text":
                assert layer["font_size"] >= 42
