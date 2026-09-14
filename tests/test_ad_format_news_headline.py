"""
News Headline (VSG-01 v3 §2.8) — the sensational-label guard ("the label
is not the format"), the requires_isolation flag §6 names explicitly, and
the same zone-overflow lesson every L2 format in this library has needed.
"""
import pytest

from app.agents.jane_ads.ad_formats.news_headline import (
    FORMAT,
    SensationalLabelRejected,
    ContentOverflowsZone,
    build_document,
    _scene_prompt,
    _check_bar_fits,
)
from app.agents.jane_ads.ad_formats.legibility import check_legibility
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS
from app.agents.jane_ads.visual_slots import InvalidSlotValue

PHOTO_URL = "https://example.com/scene.png"


class TestFormatDefinition:
    def test_generate_asset_source_l2_l4_requires_isolation(self):
        assert FORMAT.asset_source == "generate"
        assert FORMAT.layers_used == "L2-L4"
        assert FORMAT.requires_isolation is True  # §6 names SEED-077 explicitly


class TestScenePrompt:
    def test_lower_third_empty_and_editorial_style(self):
        prompt = _scene_prompt("a school handover ceremony", "a modern Lagos office interior")
        # A live render exposed the original wording's real failure: a
        # generic "clear empty space" instruction with no stated location
        # let the model put a huge dead zone wherever it liked (confirmed
        # in Work In Progress's own identical bug) — the prompt now states
        # exactly where the reserved space is AND requires the rest of the
        # frame to be filled edge to edge, not just that some space exists.
        assert "reserved space is a plain strip across the lower third" in prompt
        assert "Fill the frame edge to edge" in prompt
        # Deliberately NOT "Photojournalistic"/"candid"/"documentary" — see
        # the module docstring's "REVISED DECISION": the load-bearing rule
        # was always truthfulness, not a raw/candid photographic style. A
        # live comparison against a real published competitor ad showed the
        # candid style reading as amateur next to a polished, professional
        # advertising photograph of an equally real, non-fabricated subject.
        assert "Professional editorial advertising photograph" in prompt
        assert "Photojournalistic" not in prompt
        assert "a modern Lagos office interior" in prompt

    def test_rejects_a_setting_outside_the_controlled_vocabulary(self):
        with pytest.raises(InvalidSlotValue):
            _scene_prompt("a ceremony", "Lekki, Lagos")


class TestSensationalLabelGuard:
    @pytest.mark.parametrize("headline", [
        "BREAKING NEWS: new branch opens",
        "Breaking: prices held until October",
        "JUST IN: admissions close soon",
        "News Alert: new campus opens",
        "EXCLUSIVE: our biggest announcement yet",
    ])
    def test_named_labels_rejected(self, headline):
        with pytest.raises(SensationalLabelRejected):
            build_document(PHOTO_URL, headline)

    def test_rejected_on_secondary_line_too(self):
        with pytest.raises(SensationalLabelRejected):
            build_document(PHOTO_URL, "New branch now open in Yaba", secondary_line="Breaking news for our customers")

    @pytest.mark.parametrize("headline", [
        "Admissions close 14 September",
        "New branch now open in Yaba",
        "Prices held until October",
    ])
    def test_genuine_announcements_allowed(self, headline):
        doc = build_document(PHOTO_URL, headline)
        assert doc is not None


class TestBreakingNewsBanner:
    """See the module docstring's "REVISED DECISION" — an explicit,
    deliberate opt-in, not a default, and never a substitute for the
    headline itself stating real information (SensationalLabelGuard above
    is untouched by this)."""

    def test_omitted_by_default(self):
        doc = build_document(PHOTO_URL, "New branch now open in Yaba")
        assert not any(l.get("content") in ("BREAKING", "NEWS") for l in doc["layers"])

    def test_shown_when_requested(self):
        doc = build_document(
            PHOTO_URL, "New branch now open in Yaba", show_breaking_news_banner=True,
        )
        contents = [l.get("content") for l in doc["layers"]]
        assert "BREAKING" in contents
        assert "NEWS" in contents

    def test_still_enforces_the_sensational_label_guard_on_the_headline(self):
        """The banner is decorative framing — it never bypasses the
        requirement that the headline itself states real information,
        not a hype label standing in for actual news."""
        with pytest.raises(SensationalLabelRejected):
            build_document(
                PHOTO_URL, "BREAKING NEWS: huge announcement", show_breaking_news_banner=True,
            )

    def test_banner_text_passes_legibility(self):
        doc = build_document(
            PHOTO_URL, "New branch now open in Yaba", show_breaking_news_banner=True,
        )
        assert not check_legibility(doc, PLACEHOLDER_TOKENS)


class TestBarOverflowGuard:
    def test_overflow_is_rejected(self):
        """Tests the guard directly with a manufactured height rather than
        routing through wrap_text, whose line count depends on the real
        render font's metrics this machine's font-path fallback can't
        reproduce exactly (see _text_metrics.py's own docstring)."""
        with pytest.raises(ContentOverflowsZone):
            _check_bar_fits(bar_content_height=500, bar_zone_height=360)

    def test_within_budget_is_accepted(self):
        _check_bar_fits(bar_content_height=300, bar_zone_height=360)  # does not raise


class TestBuildDocument:
    def _doc(self, **kw):
        defaults = dict(
            photo_url=PHOTO_URL,
            headline="New campus now open in Yaba",
            secondary_line="Admissions close 14 September",
            date_stamp="Sept 6",
        )
        defaults.update(kw)
        return build_document(**defaults)

    def test_photo_fills_the_whole_canvas(self):
        doc = self._doc()
        photo = next(l for l in doc["layers"] if l["type"] == "ai_generated_background")
        assert photo["width"] == doc["canvas"]["width"]
        assert photo["height"] == doc["canvas"]["height"]

    def test_bar_is_the_lower_third(self):
        doc = self._doc()
        height = doc["canvas"]["height"]
        bar = next(l for l in doc["layers"] if l["type"] == "shape")
        assert bar["y"] >= height - height // 3 - 1  # allow integer-division rounding

    def test_headline_secondary_and_date_all_present_in_order(self):
        doc = self._doc()
        headline = next(l for l in doc["layers"] if l.get("content") == "New campus now open in Yaba")
        secondary = next(l for l in doc["layers"] if l.get("content") == "Admissions close 14 September")
        date = next(l for l in doc["layers"] if l.get("content") == "Sept 6")
        assert headline["y"] < secondary["y"] < date["y"]

    def test_headline_and_date_share_the_panel_text_colour(self):
        """The bottom panel is a dark, on-brand-tinted plate (same colour
        as the BREAKING NEWS banner, see _banner_colors), not flat white —
        so headline/date text is whichever of white/ink actually contrasts
        against it, not a fixed ink/accent pairing that assumed a light
        panel. Both share the SAME colour deliberately: the raw accent is
        too close in hue to a darkened version of itself to reliably
        contrast, so date_stamp no longer tries to stand out via colour
        (bold weight still gives it emphasis)."""
        from app.agents.jane_ads.ad_formats.news_headline import _banner_colors

        doc = self._doc()
        headline = next(l for l in doc["layers"] if l.get("content") == "New campus now open in Yaba")
        date = next(l for l in doc["layers"] if l.get("content") == "Sept 6")
        _, expected_text = _banner_colors(PLACEHOLDER_TOKENS)
        assert headline["color"] == expected_text
        assert date["color"] == expected_text

    def test_secondary_line_and_date_are_optional(self):
        doc = self._doc(secondary_line=None, date_stamp=None)
        texts = [l.get("content", "") for l in doc["layers"]]
        assert "Admissions close 14 September" not in texts
        assert "Sept 6" not in texts

    def test_no_masthead_or_logo_layer_exists(self):
        """§2.8: 'Do not imitate a specific broadcaster or masthead.'
        Enforced by construction — this module has no brand_asset/logo
        layer type at all."""
        doc = self._doc()
        assert not any(l["type"] == "brand_asset" for l in doc["layers"])

    def test_passes_its_own_legibility_self_check(self):
        doc = self._doc()
        assert check_legibility(doc, PLACEHOLDER_TOKENS) == []
