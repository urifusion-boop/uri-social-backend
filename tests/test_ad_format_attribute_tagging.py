"""
attribute_tagging.py (VSG-01 v3 §4) — the full ad_format_attributes
schema assembled from a format's static AdFormatDef plus real inference
against an actual rendered document, verified against real documents from
several already-shipped formats, not just synthetic ones.
"""
from app.agents.jane_ads.ad_formats.attribute_tagging import (
    build_ad_format_attributes,
    default_aesthetic_register,
    infer_text_density,
    infer_shows_price,
)
from app.agents.jane_ads.ad_formats import receipt, review_card, humour_cartoon, day1_day30, censored_item


class TestDefaultAestheticRegister:
    def test_phone_native_formats(self):
        assert default_aesthetic_register("SEED-080") == "phone_native"  # Problem/Solution
        assert default_aesthetic_register("SEED-077") == "phone_native"  # News Headline

    def test_polished_formats(self):
        assert default_aesthetic_register("SEED-083") == "polished"  # The Censored Item

    def test_unknown_format_id_defaults_to_clean_simple(self):
        assert default_aesthetic_register("SEED-999") == "clean_simple"


class TestInferTextDensity:
    def test_no_text_layers_is_none(self):
        doc = {"layers": [{"type": "shape", "shape": "rect"}]}
        assert infer_text_density(doc) == "none"

    def test_a_few_words_is_minimal(self):
        doc = {"layers": [{"type": "text", "content": "Same-day delivery"}]}
        assert infer_text_density(doc) == "minimal"

    def test_many_words_is_moderate_or_heavy(self):
        doc = {"layers": [{"type": "text", "content": "word " * 40}]}
        assert infer_text_density(doc) == "heavy"

    def test_empty_content_does_not_count(self):
        doc = {"layers": [{"type": "text", "content": ""}]}
        assert infer_text_density(doc) == "none"


class TestInferShowsPrice:
    def test_naira_symbol_detected(self):
        doc = {"layers": [{"type": "text", "content": "₦32,500"}]}
        assert infer_shows_price(doc) is True

    def test_n_prefixed_amount_detected(self):
        doc = {"layers": [{"type": "text", "content": "Total: N24,000"}]}
        assert infer_shows_price(doc) is True

    def test_star_rating_is_not_mistaken_for_a_price(self):
        """Real integration finding: Review Card's star row also uses the
        `accent` token, the same colour Receipt uses for its price — a
        colour-based heuristic would have falsely flagged it. This checks
        content, not colour, precisely to avoid that."""
        doc = {"layers": [{"type": "text", "content": "★★★★★"}]}
        assert infer_shows_price(doc) is False

    def test_no_price_language_is_false(self):
        doc = {"layers": [{"type": "text", "content": "Same-day delivery across Lagos"}]}
        assert infer_shows_price(doc) is False


class TestBuildAdFormatAttributesAgainstRealDocuments:
    """Not synthetic documents — the actual build_document() output of
    several already-shipped formats."""

    def test_receipt_shows_a_real_price(self):
        doc = receipt.build_document(
            items=[("Ankara fabric", "N24,000")], total_label="Total", total_amount="N32,500",
        )
        attrs = build_ad_format_attributes(receipt.FORMAT, doc, has_person=False, person_is_real=False)
        assert attrs["format_id"] == "SEED-081"
        assert attrs["asset_source"] == "drawn"
        assert attrs["shows_price"] is True
        assert attrs["requires_isolation"] is False

    def test_review_card_star_rating_does_not_show_as_a_price(self):
        doc = review_card.build_document(
            product_image_url="https://x.png", quote="Great product",
            attribution="Amaka N.", star_rating=5,
        )
        attrs = build_ad_format_attributes(review_card.FORMAT, doc, has_person=False, person_is_real=False)
        assert attrs["shows_price"] is False

    def test_humour_cartoon_has_no_text_at_all(self):
        doc = humour_cartoon.build_document("https://x.png", human_reviewed=True)
        attrs = build_ad_format_attributes(humour_cartoon.FORMAT, doc, has_person=True, person_is_real=False)
        assert attrs["text_density"] == "none"
        assert attrs["layers_used"] == "L2-L4"

    def test_day1_day30_and_censored_item_carry_requires_isolation(self):
        doc = day1_day30.build_document("https://a.png", "https://b.png", category="repair_or_restoration")
        attrs = build_ad_format_attributes(day1_day30.FORMAT, doc, has_person=False, person_is_real=False)
        assert attrs["requires_isolation"] is True

        doc2 = censored_item.build_document(
            "https://p.png", 100, 100, 200, 100, "Reveals soon", "the new design",
        )
        attrs2 = build_ad_format_attributes(censored_item.FORMAT, doc2, has_person=False, person_is_real=False)
        assert attrs2["requires_isolation"] is True

    def test_caller_supplied_aesthetic_register_overrides_the_default(self):
        doc = receipt.build_document(
            items=[("Item", "N1,000")], total_label="Total", total_amount="N1,000",
        )
        attrs = build_ad_format_attributes(
            receipt.FORMAT, doc, has_person=False, person_is_real=False, aesthetic_register="polished",
        )
        assert attrs["aesthetic_register"] == "polished"

    def test_full_schema_keys_present(self):
        doc = receipt.build_document(
            items=[("Item", "N1,000")], total_label="Total", total_amount="N1,000",
        )
        attrs = build_ad_format_attributes(receipt.FORMAT, doc, has_person=False, person_is_real=False)
        assert set(attrs.keys()) == {
            "format_id", "asset_source", "layers_used", "aesthetic_register",
            "has_person", "person_is_real", "text_density", "shows_price", "requires_isolation",
        }
