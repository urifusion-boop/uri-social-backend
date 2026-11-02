"""
The LinkedIn-logo-distortion fix (ImageContentService._crop_to_ratio, see
tests/test_image_content_service_gpt_image_2_aspect_ratio.py) traded a
whole-canvas stretch for a post-generation crop. That crop is correct for
photos, but it also crops whatever text the model drew near the edge it
cuts — and even after the prompt's safe-zone margin was made crop-aware
(base_pct=20, ported to prod), a real headline still got clipped at the top
(screenshots: MacariusVille, Smart Futures posts, Oct 2026). The remaining
gap: the crop itself split the removed height 50/50 between top and bottom,
giving the top edge the same odds of being clipped as the bottom despite
headlines reliably sitting near the top (by the prompt's own instruction)
while the bottom (CTA) is comparatively disposable and was never reported
clipped.

_crop_to_ratio now biases a top+bottom crop toward the bottom
(_TOP_CROP_BIAS, currently 0.2 — only 20% of the removed height comes off
the top), and _safe_zone_margins_pct computes top/bottom margins separately
so each one reflects what its own edge actually loses, rather than one flat
figure that assumed an even split.
"""
import asyncio
import base64
import io
from unittest.mock import patch

import pytest

pytest.importorskip("PIL")
from PIL import Image

from app.agents.social_media_manager.services.image_content_service import ImageContentService


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestSafeZoneMarginsPct:
    def test_linkedin_ratio_increases_top_and_bottom_margin_unevenly(self):
        """1200x628 vs the 1536x1024 bucket crops top/bottom only, biased
        toward the bottom — bottom's margin must grow more than top's."""
        margins = ImageContentService._safe_zone_margins_pct(1200, 628)
        assert margins["top"] > 20
        assert margins["bottom"] > 20
        assert margins["bottom"] > margins["top"]
        assert margins["left_right"] == 20

    def test_instagram_story_crops_left_right_not_top_bottom(self):
        """1080x1920 (0.5625) is narrower than the 1024x1536 (0.667) portrait
        bucket, so _crop_to_ratio crops the sides, not top/bottom — the
        left/right crop stays centered, so top and bottom stay at base."""
        margins = ImageContentService._safe_zone_margins_pct(1080, 1920)
        assert margins["left_right"] > 20
        assert margins["top"] == 20
        assert margins["bottom"] == 20

    def test_instagram_portrait_1080x1350_crops_top_bottom_unevenly(self):
        """1080x1350 (0.8) is less narrow than the 1024x1536 (0.667) portrait
        bucket, so _crop_to_ratio crops top/bottom here, same direction as
        _crop_to_ratio's own branch for this exact case — again biased
        toward the bottom."""
        margins = ImageContentService._safe_zone_margins_pct(1080, 1350)
        assert margins["top"] > 20
        assert margins["bottom"] > 20
        assert margins["bottom"] > margins["top"]
        assert margins["left_right"] == 20

    def test_square_target_matches_square_bucket_no_change(self):
        margins = ImageContentService._safe_zone_margins_pct(1024, 1024)
        assert margins == {"top": 20, "bottom": 20, "left_right": 20}

    def test_close_enough_ratio_skips_adjustment(self):
        """A landscape target very close to the 1.5:1 bucket (within
        _crop_to_ratio's own 2% tolerance) shouldn't get an inflated margin."""
        margins = ImageContentService._safe_zone_margins_pct(1536, 1024)
        assert margins == {"top": 20, "bottom": 20, "left_right": 20}

    def test_top_and_bottom_margins_match_crop_to_ratio_bias(self):
        """Each margin should equal the actual fraction _crop_to_ratio
        removes from ITS edge (split via _TOP_CROP_BIAS), rounded, plus the
        base 20%."""
        target_w, target_h = 1200, 628
        gen_w, gen_h = 1536, 1024
        target_ratio = target_w / target_h
        new_h = gen_w / target_ratio
        removed_frac = (gen_h - new_h) / gen_h
        bias = ImageContentService._TOP_CROP_BIAS
        expected_top = 20 + round(removed_frac * bias * 100)
        expected_bottom = 20 + round(removed_frac * (1 - bias) * 100)

        margins = ImageContentService._safe_zone_margins_pct(target_w, target_h)
        assert margins["top"] == expected_top
        assert margins["bottom"] == expected_bottom


class TestCropToRatioTopBias:
    """Direct pixel-level proof that a top+bottom crop actually keeps the
    top edge, not just that the margin math says it should — a center-crop
    (the pre-fix behaviour) would remove 110 rows off the top here; this
    must remove far fewer."""

    def test_crop_keeps_the_exact_row_the_bias_formula_predicts(self):
        gen_w, gen_h, target_w, target_h = 1536, 1024, 1200, 628
        target_ratio = target_w / target_h
        new_h = int(gen_w / target_ratio)
        removed = gen_h - new_h
        expected_top = int(removed * ImageContentService._TOP_CROP_BIAS)
        old_center_crop_top = removed // 2
        assert expected_top < old_center_crop_top  # the fix must remove less off the top than a center-crop would

        # A thick red band straddling the row the new (biased) crop should
        # keep as its new top edge, and a thick green band straddling the
        # row a plain center-crop (the pre-fix behaviour) would have kept
        # instead — wide enough to survive _crop_to_ratio's own lossy WebP
        # re-encode, which a single marker pixel/row would not.
        band = 16
        img = Image.new("RGB", (gen_w, gen_h), color=(128, 128, 128))
        for x in range(gen_w):
            for dy in range(-band, band):
                y = expected_top + dy
                if 0 <= y < gen_h:
                    img.putpixel((x, y), (255, 0, 0))
            for dy in range(-band, band):
                y = old_center_crop_top + dy
                if 0 <= y < gen_h:
                    img.putpixel((x, y), (0, 255, 0))
        buf = io.BytesIO()
        img.save(buf, format="WEBP", lossless=True)
        b64 = base64.b64encode(buf.getvalue()).decode()

        cropped_b64 = ImageContentService._crop_to_ratio(b64, target_w, target_h)
        cropped = Image.open(io.BytesIO(base64.b64decode(cropped_b64))).convert("RGB")

        # _crop_to_ratio's own output save is lossy (matches production), so
        # compare against which pure marker colour each pixel lands closer
        # to rather than requiring byte-exact equality.
        def closer_to(pixel, color_a, color_b):
            dist = lambda c: sum((p - q) ** 2 for p, q in zip(pixel, c))
            return dist(color_a) < dist(color_b)

        # The new top edge should land inside (or very near) the red band.
        top_pixel = cropped.getpixel((cropped.width // 2, 0))[:3]
        assert closer_to(top_pixel, (255, 0, 0), (0, 255, 0))

        # The row the OLD center-crop would have made the new top edge must
        # now read as the green band (still present, further down), not grey.
        row_for_old_center_crop = old_center_crop_top - expected_top
        old_crop_row_pixel = cropped.getpixel((cropped.width // 2, row_for_old_center_crop))[:3]
        assert closer_to(old_crop_row_pixel, (0, 255, 0), (128, 128, 128))


class TestSafeZoneMarginsInPrompt:
    """Confirms the computed margins actually reach the generation prompt,
    not just the standalone helper."""

    def test_linkedin_prompt_uses_inflated_top_and_bottom_margins(self):
        captured = {}

        async def fake_call_dalle_api(**kwargs):
            captured["prompt"] = kwargs.get("prompt", "")
            return {"success": False, "error": "stub — test only needs the prompt"}

        with patch.object(
            ImageContentService, "_call_dalle_api", side_effect=fake_call_dalle_api
        ):
            run(ImageContentService._generate_platform_image(
                platform="linkedin",
                content="Why simplicity beats complexity in marketing.",
                seed_content="Why simplicity beats complexity in marketing.",
                brand_context={"brand_name": "Acme", "brand_colors": ["#E6007E"]},
                image_type="post_image",
                image_model="openai/gpt-image-2",
            ))

        prompt = captured["prompt"]
        margins = ImageContentService._safe_zone_margins_pct(1200, 628)
        assert margins["top"] > 20
        assert margins["bottom"] > 20
        assert f"{margins['top']}% safe-zone margin from the top edge" in prompt
        assert f"{margins['bottom']}% safe-zone margin from the bottom edge" in prompt
        assert "20% safe-zone margin from the top edge" not in prompt
        assert "cropped to fit" in prompt

    def test_square_instagram_prompt_keeps_flat_20_percent(self):
        captured = {}

        async def fake_call_dalle_api(**kwargs):
            captured["prompt"] = kwargs.get("prompt", "")
            return {"success": False, "error": "stub — test only needs the prompt"}

        with patch.object(
            ImageContentService, "_call_dalle_api", side_effect=fake_call_dalle_api
        ):
            run(ImageContentService._generate_platform_image(
                platform="instagram",
                content="A clean product shot.",
                seed_content="A clean product shot.",
                brand_context={"brand_name": "Acme", "brand_colors": ["#E6007E"]},
                image_type="post_square",
                image_model="openai/gpt-image-2",
            ))

        prompt = captured["prompt"]
        assert "20% safe-zone margin from the top edge" in prompt
        assert "20%\nsafe-zone margin from the left and right edges" in prompt
        assert "20% safe-zone margin from the bottom edge" in prompt
        assert "cropped to fit" not in prompt
