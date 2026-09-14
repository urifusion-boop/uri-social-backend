"""
brand_tokens.py (VSG-01 v3 §1.4/§10.1) — the contrast-ratio math itself
(checked against known WCAG reference values, not assumed correct), and
resolve_brand_tokens()'s accent-selection/fallback behaviour.
"""
from app.agents.jane_ads.ad_formats.brand_tokens import (
    contrast_ratio,
    resolve_brand_tokens,
)
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS


class TestContrastRatio:
    def test_black_on_white_is_the_wcag_reference_value_21(self):
        assert round(contrast_ratio("#FFFFFF", "#000000"), 2) == 21.0

    def test_identical_colours_have_a_ratio_of_1(self):
        assert round(contrast_ratio("#CD1B78", "#CD1B78"), 2) == 1.0

    def test_is_symmetric(self):
        a, b = contrast_ratio("#CD1B78", "#FFFFFF"), contrast_ratio("#FFFFFF", "#CD1B78")
        assert round(a, 6) == round(b, 6)

    def test_default_accent_clears_the_bar_against_surface_and_field(self):
        """The fallback this whole module leans on — if this ever regresses
        (e.g. PLACEHOLDER_TOKENS' accent or surface/field changes), every
        brand with no compliant swatch of their own silently gets an
        illegible accent, so this must hold for real, not by assumption."""
        assert contrast_ratio(PLACEHOLDER_TOKENS["accent"], PLACEHOLDER_TOKENS["surface"]) >= 4.5
        assert contrast_ratio(PLACEHOLDER_TOKENS["accent"], PLACEHOLDER_TOKENS["field"]) >= 4.5


class TestResolveBrandTokens:
    def test_no_brand_colours_falls_back_to_the_default_accent(self):
        for empty in (None, []):
            tokens = resolve_brand_tokens(empty)
            assert tokens["accent"] == PLACEHOLDER_TOKENS["accent"]

    def test_the_five_structural_roles_never_vary_by_brand(self):
        tokens = resolve_brand_tokens(["#0000FF", "#00FF00", "#FF00FF"])
        for role in ("surface", "field", "ink", "ink-quiet", "edge"):
            assert tokens[role] == PLACEHOLDER_TOKENS[role]

    def test_most_saturated_compliant_swatch_wins(self):
        # #003366 (a fully-saturated dark blue) is more saturated than
        # #6699CC (a muted, desaturated blue) — the vivid one should win.
        tokens = resolve_brand_tokens(["#6699CC", "#003366"])
        assert tokens["accent"] == "#003366"

    def test_a_swatch_that_fails_contrast_is_skipped_for_one_that_passes(self):
        # #FDF6EC is near-white — indistinguishable from `surface`/`field`,
        # fails contrast outright. #1B4D3E (dark green) passes easily.
        # Saturation alone would not obviously prefer one over the other,
        # but only the compliant one should ever be selectable.
        tokens = resolve_brand_tokens(["#FDF6EC", "#1B4D3E"])
        assert tokens["accent"] == "#1B4D3E"

    def test_all_swatches_failing_contrast_falls_back_to_default(self):
        # Both are near-white — neither clears 4.5:1 against surface/field.
        tokens = resolve_brand_tokens(["#FDF6EC", "#FFFEF9"])
        assert tokens["accent"] == PLACEHOLDER_TOKENS["accent"]

    def test_a_single_compliant_brand_colour_is_used_as_is(self):
        tokens = resolve_brand_tokens(["#1B4D3E"])
        assert tokens["accent"] == "#1B4D3E"

    def test_returns_all_six_roles(self):
        tokens = resolve_brand_tokens(["#003366"])
        assert set(tokens.keys()) == {"surface", "field", "ink", "ink-quiet", "accent", "edge"}
