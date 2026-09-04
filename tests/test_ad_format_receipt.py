"""
The Receipt (VSG-01 v3 §2.5) — build_document structural correctness, plus
the format's own hard-check-relevant properties (§2.5's absolute rule: must
never resemble a bank alert — this module never introduces bank language on
its own, since that's a caller-side guarantee, but it must not silently
strip or corrupt whatever real content the caller supplies either).

A real render was already visually verified by hand (see the commit this
ships in); these are the structural assertions that don't need eyeballing
a PNG every time.
"""
from app.agents.jane_ads.ad_formats.receipt import FORMAT, build_document
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS


class TestFormatDefinition:
    def test_drawn_asset_source_no_requirements(self):
        # A drawn format needs no photo of anything — nothing to gate.
        assert FORMAT.asset_source == "drawn"
        assert FORMAT.requires == []
        assert FORMAT.layers_used == "L4"


class TestBuildDocument:
    def _doc(self, **kw):
        defaults = dict(
            items=[("Item A", "N1,000"), ("Item B", "N2,500")],
            total_label="Total",
            total_amount="N3,500",
        )
        defaults.update(kw)
        return build_document(**defaults)

    def test_canvas_uses_surface_token_by_default(self):
        doc = self._doc()
        assert doc["canvas"]["background_color"] == PLACEHOLDER_TOKENS["surface"]

    def test_every_item_produces_name_leader_and_price_layers(self):
        doc = self._doc(items=[("A", "N1"), ("B", "N2"), ("C", "N3")])
        texts = [l["content"] for l in doc["layers"] if l["type"] == "text"]
        lines = [l for l in doc["layers"] if l["type"] == "shape" and l["shape"] == "line"]
        for name in ("A", "B", "C"):
            assert name in texts
        for price in ("N1", "N2", "N3"):
            assert price in texts
        # 3 dashed leaders + 1 solid rule-above-total = 4 line shapes
        assert len(lines) == 4
        assert sum(1 for l in lines if l.get("dashed")) == 3
        assert sum(1 for l in lines if not l.get("dashed")) == 1

    def test_prices_are_right_aligned(self):
        doc = self._doc()
        price_layers = [
            l for l in doc["layers"]
            if l["type"] == "text" and l["content"] in ("N1,000", "N2,500")
        ]
        assert len(price_layers) == 2
        assert all(l.get("text_align") == "ra" for l in price_layers)

    def test_total_uses_accent_token_and_is_right_aligned(self):
        doc = self._doc()
        total_layer = next(l for l in doc["layers"] if l.get("content") == "N3,500")
        assert total_layer["color"] == PLACEHOLDER_TOKENS["accent"]
        assert total_layer["text_align"] == "ra"

    def test_no_bank_alert_language_introduced_by_this_module(self):
        """This module must never itself add bank/transaction-confirmation
        language — §2.5's absolute rule. It has no reason to (it only ever
        renders exactly what the caller passed in), but this guards against
        a future edit accidentally hardcoding something like a success tick
        or 'transaction successful' string."""
        doc = self._doc()
        all_text = " ".join(
            str(l.get("content", "")) for l in doc["layers"] if l["type"] == "text"
        ).lower()
        for forbidden in ("transaction successful", "account number", "bank alert", "debit alert"):
            assert forbidden not in all_text

    def test_delivery_and_payment_lines_use_ink_quiet_when_present(self):
        doc = self._doc(delivery_line="Ready in 2 days", payment_line="Cash or transfer")
        footer_layers = [
            l for l in doc["layers"]
            if l.get("content") in ("Ready in 2 days", "Cash or transfer")
        ]
        assert len(footer_layers) == 2
        assert all(l["color"] == PLACEHOLDER_TOKENS["ink-quiet"] for l in footer_layers)

    def test_delivery_and_payment_lines_omitted_when_not_given(self):
        doc = self._doc()  # no delivery_line/payment_line passed
        texts = [l.get("content", "") for l in doc["layers"]]
        assert not any("Ready" in t or "Cash" in t for t in texts)

    def test_brand_logo_reserves_head_space_when_given(self):
        with_logo = self._doc(brand_logo_url="https://example.com/logo.png", business_name="Ignored When Logo Given")
        asset_layers = [l for l in with_logo["layers"] if l["type"] == "brand_asset"]
        assert len(asset_layers) == 1
        assert asset_layers[0]["url"] == "https://example.com/logo.png"
        # business_name text must NOT also render — logo takes priority
        text_contents = [l.get("content", "") for l in with_logo["layers"] if l["type"] == "text"]
        assert "Ignored When Logo Given" not in text_contents

    def test_business_name_renders_when_no_logo_given(self):
        doc = self._doc(business_name="Adaeze Couture")
        texts = [l.get("content", "") for l in doc["layers"] if l["type"] == "text"]
        assert "Adaeze Couture" in texts

    def test_custom_tokens_override_placeholders(self):
        custom = {**PLACEHOLDER_TOKENS, "accent": "#00FF00"}
        doc = self._doc(tokens=custom)
        total_layer = next(l for l in doc["layers"] if l.get("content") == "N3,500")
        assert total_layer["color"] == "#00FF00"
