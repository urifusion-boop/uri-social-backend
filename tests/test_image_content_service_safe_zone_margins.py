"""
The LinkedIn-logo-distortion fix (ImageContentService._crop_to_ratio, see
tests/test_image_content_service_gpt_image_2_aspect_ratio.py) traded a
whole-canvas stretch for a post-generation center-crop. That crop is correct
for photos, but it also crops whatever text the model drew near the edge it
cuts — and the prompt's safe-zone instruction was a flat 12% for every
platform, regardless of how much that platform's crop actually removes.

For LinkedIn specifically (1200x628, 1.91:1) against GPT-Image-2's fixed
1536x1024 (1.5:1) landscape bucket, _crop_to_ratio removes ~11% off the top
AND ~11% off the bottom before the image is even resized — eating almost the
entire 12% margin the model was told to respect, so a headline positioned
exactly at the (pre-crop) 12% line gets sliced by the crop. Confirmed live:
a generated LinkedIn image with its headline clipped at the top edge.

_safe_zone_margins_pct computes the real margin the model must be told to
leave on the axis that actually gets cropped, mirroring _crop_to_ratio's own
math, so the prompt's "safe zone" means safe after the crop too.
"""
import asyncio
from unittest.mock import patch

from app.agents.social_media_manager.services.image_content_service import ImageContentService


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestSafeZoneMarginsPct:
    def test_linkedin_ratio_increases_top_bottom_margin_not_left_right(self):
        """1200x628 vs the 1536x1024 bucket crops top/bottom only."""
        margins = ImageContentService._safe_zone_margins_pct(1200, 628)
        assert margins["top_bottom"] > 12
        assert margins["left_right"] == 12

    def test_instagram_story_crops_left_right_not_top_bottom(self):
        """1080x1920 (0.5625) is narrower than the 1024x1536 (0.667) portrait
        bucket, so _crop_to_ratio crops the sides, not top/bottom."""
        margins = ImageContentService._safe_zone_margins_pct(1080, 1920)
        assert margins["left_right"] > 12
        assert margins["top_bottom"] == 12

    def test_instagram_portrait_1080x1350_crops_top_bottom(self):
        """1080x1350 (0.8) is less narrow than the 1024x1536 (0.667) portrait
        bucket, so _crop_to_ratio crops top/bottom here, same direction as
        _crop_to_ratio's own branch for this exact case."""
        margins = ImageContentService._safe_zone_margins_pct(1080, 1350)
        assert margins["top_bottom"] > 12
        assert margins["left_right"] == 12

    def test_square_target_matches_square_bucket_no_change(self):
        margins = ImageContentService._safe_zone_margins_pct(1024, 1024)
        assert margins == {"top_bottom": 12, "left_right": 12}

    def test_close_enough_ratio_skips_adjustment(self):
        """A landscape target very close to the 1.5:1 bucket (within
        _crop_to_ratio's own 2% tolerance) shouldn't get an inflated margin."""
        margins = ImageContentService._safe_zone_margins_pct(1536, 1024)
        assert margins == {"top_bottom": 12, "left_right": 12}

    def test_margin_matches_crop_to_ratio_fraction(self):
        """The added margin should equal the actual fraction _crop_to_ratio
        removes from each side, rounded, plus the base 12%."""
        target_w, target_h = 1200, 628
        gen_w, gen_h = 1536, 1024
        target_ratio = target_w / target_h
        new_h = gen_w / target_ratio
        cropped_frac_each_side = ((gen_h - new_h) / gen_h) / 2
        expected = 12 + round(cropped_frac_each_side * 100)

        margins = ImageContentService._safe_zone_margins_pct(target_w, target_h)
        assert margins["top_bottom"] == expected


class TestSafeZoneMarginsInPrompt:
    """Confirms the computed margins actually reach the generation prompt,
    not just the standalone helper."""

    def test_linkedin_prompt_uses_inflated_top_bottom_margin(self):
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
        margins = ImageContentService._safe_zone_margins_pct(1200, 628, base_pct=20)
        assert f"{margins['top_bottom']}% safe-zone margin from the top edge" in prompt
        assert margins["top_bottom"] > 20
        assert "20% safe-zone margin from the top edge" not in prompt

    def test_square_instagram_prompt_keeps_flat_base_percent(self):
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
