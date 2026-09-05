"""
Problem / Solution (VSG-01 v3 §2.3) — build_document structural
correctness, the scrim-over-photography requirement, and the
scrim-overflow guard this format needed that no other format in the
library does (the first one whose text sits directly over a generated
photo rather than a plain colour).
"""
import pytest

from app.agents.jane_ads.ad_formats.problem_solution import (
    FORMAT,
    TextOverflowsScrim,
    build_document,
    _problem_prompt,
    _solution_prompt,
    _check_fits_scrim,
)
from app.agents.jane_ads.ad_formats.legibility import check_legibility
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS
from app.agents.jane_ads.visual_slots import InvalidSlotValue

PROBLEM_URL = "https://example.com/problem.png"
SOLUTION_URL = "https://example.com/solution.png"


class TestFormatDefinition:
    def test_generate_asset_source_l2_l4_layers(self):
        assert FORMAT.asset_source == "generate"
        assert FORMAT.layers_used == "L2-L4"
        assert FORMAT.requires == []


class TestPrompts:
    def test_problem_prompt_uses_top_empty_band_and_muted_palette(self):
        prompt = _problem_prompt("a trader losing customers to network downtime", "a roadside food stand")
        assert "top 40 percent" in prompt
        assert "muted desaturated palette" in prompt
        assert "a roadside food stand" in prompt

    def test_solution_prompt_uses_bottom_empty_band_and_warm_palette(self):
        prompt = _solution_prompt("a trader serving customers without interruption", "a roadside food stand")
        assert "bottom 40 percent" in prompt
        assert "warm palette" in prompt
        assert "resolved and orderly" in prompt

    def test_prompts_reject_a_setting_outside_the_controlled_vocabulary(self):
        """§3's own named bug, same guard visual_slots.py already enforces
        — a resolved geo-target must not silently pass through here."""
        with pytest.raises(InvalidSlotValue):
            _problem_prompt("a problem", "Lekki, Lagos")


class TestBuildDocument:
    def _doc(self, **kw):
        defaults = dict(
            problem_image_url=PROBLEM_URL, solution_image_url=SOLUTION_URL,
            problem_text="Network downtime costs traders N5,000 a day",
            solution_text="Stay online, keep every sale",
        )
        defaults.update(kw)
        return build_document(**defaults)

    def test_two_generated_background_layers_top_and_bottom(self):
        doc = self._doc()
        bgs = sorted(
            (l for l in doc["layers"] if l["type"] == "ai_generated_background"),
            key=lambda l: l["y"],
        )
        assert len(bgs) == 2
        assert bgs[0]["url"] == PROBLEM_URL
        assert bgs[0]["y"] == 0
        assert bgs[1]["url"] == SOLUTION_URL
        assert bgs[1]["y"] == doc["canvas"]["height"] // 2

    def test_each_zone_has_a_solid_scrim_between_photo_and_text(self):
        doc = self._doc()
        scrims = [l for l in doc["layers"] if l["type"] == "shape" and l.get("fill_color") == PLACEHOLDER_TOKENS["field"]]
        assert len(scrims) == 2

    def test_problem_scrim_sits_at_the_top_of_its_zone(self):
        doc = self._doc()
        scrims = sorted(
            (l for l in doc["layers"] if l["type"] == "shape" and l.get("fill_color")),
            key=lambda l: l["y"],
        )
        assert scrims[0]["y"] == 0

    def test_solution_scrim_sits_at_the_bottom_of_its_zone(self):
        doc = self._doc()
        height = doc["canvas"]["height"]
        scrims = sorted(
            (l for l in doc["layers"] if l["type"] == "shape" and l.get("fill_color")),
            key=lambda l: l["y"],
        )
        bottom_scrim = scrims[1]
        assert bottom_scrim["y"] + bottom_scrim["height"] == height

    def test_passes_its_own_legibility_self_check(self):
        """build_document calls assert_legible internally — this confirms
        its own real output actually clears check_legibility too, not just
        that it didn't raise."""
        doc = self._doc()
        assert check_legibility(doc, PLACEHOLDER_TOKENS) == []

    def test_long_text_that_would_overflow_the_scrim_is_rejected(self):
        """Tests _check_fits_scrim directly with a manufactured line count
        rather than routing through wrap_text: how many lines a given
        string wraps to depends on the real render font's metrics, which
        this machine's font-path fallback can't reproduce exactly (see
        _text_metrics.py's own docstring) — a fixed line count makes this
        deterministic regardless of which font happens to be measuring."""
        with pytest.raises(TextOverflowsScrim):
            _check_fits_scrim(["line one", "line two", "line three", "line four"], "problem", scrim_height=216)

    def test_text_within_the_scrim_band_is_accepted(self):
        _check_fits_scrim(["one line"], "problem", scrim_height=216)  # does not raise

    def test_short_text_fits_comfortably(self):
        doc = self._doc(problem_text="Costs traders N5,000 daily", solution_text="Never lose a sale again")
        assert doc is not None
