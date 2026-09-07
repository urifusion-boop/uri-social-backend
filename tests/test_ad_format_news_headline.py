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
    def test_lower_third_empty_and_photojournalistic(self):
        prompt = _scene_prompt("a school handover ceremony", "a modern Lagos office interior")
        assert "clear empty space across the lower third" in prompt
        assert "Photojournalistic" in prompt
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

    def test_date_uses_ink_quiet_headline_uses_ink(self):
        doc = self._doc()
        headline = next(l for l in doc["layers"] if l.get("content") == "New campus now open in Yaba")
        date = next(l for l in doc["layers"] if l.get("content") == "Sept 6")
        assert headline["color"] == PLACEHOLDER_TOKENS["ink"]
        assert date["color"] == PLACEHOLDER_TOKENS["ink-quiet"]

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
