"""The brand's chosen logo position is a user decision, not something the AI
image generator gets to override — these cover ImageContentService.build_logo_space_note
(the shared "reserve this corner" instruction, a best-effort request to the
model) and _clear_region_for_logo (the deterministic guarantee: the prompt
instruction alone was confirmed live to not always be honored — headline text
still landed under the logo — so the logo's exact footprint is now forcibly
cleaned before every paste, regardless of what the model drew there)."""
import base64
import io
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from PIL import Image, ImageDraw, ImageStat

from app.agents.social_media_manager.services.image_content_service import (
    ImageContentService,
)


class TestBuildLogoSpaceNote:
    def test_no_logo_url_returns_empty_string(self):
        assert ImageContentService.build_logo_space_note({}) == ""
        assert ImageContentService.build_logo_space_note(None) == ""
        assert ImageContentService.build_logo_space_note({"logo_position": "top_right"}) == ""

    def test_with_logo_url_names_the_configured_corner(self):
        note = ImageContentService.build_logo_space_note(
            {"logo_url": "https://example.com/logo.png", "logo_position": "top_right", "logo_size": "small"}
        )
        assert "top-right corner" in note
        assert "CRITICAL" in note
        assert "MUST be 100% clear" in note

    def test_defaults_to_bottom_right_when_position_unset(self):
        note = ImageContentService.build_logo_space_note({"logo_url": "https://example.com/logo.png"})
        assert "bottom-right corner" in note

    def test_larger_logo_size_reserves_more_space(self):
        small_note = ImageContentService.build_logo_space_note(
            {"logo_url": "https://x/logo.png", "logo_position": "bottom_right", "logo_size": "small"}
        )
        large_note = ImageContentService.build_logo_space_note(
            {"logo_url": "https://x/logo.png", "logo_position": "bottom_right", "logo_size": "large"}
        )
        # Both should quote an explicit W% x H% figure; large's numbers must be bigger.
        import re

        small_pct = int(re.search(r"(\d+)% width", small_note).group(1))
        large_pct = int(re.search(r"(\d+)% width", large_note).group(1))
        assert large_pct > small_pct


class TestV2GuideRespectsLogoPosition:
    """generate_image_with_v2_guide previously built its prompt with zero
    knowledge of the logo at all — brand_context's logo_url/logo_position
    were only read AFTER generation, purely for the post-hoc paste. This
    confirms the reservation note now reaches the actual prompt sent to the
    image model."""

    @pytest.mark.asyncio
    async def test_final_prompt_includes_logo_reservation_when_logo_configured(self):
        from app.agents.social_media_manager.services.custom_visual_guide_v2_service import (
            CustomVisualGuideV2Service,
        )

        fake_db = MagicMock()
        fake_collection = MagicMock()
        fake_collection.find_one = AsyncMock(
            return_value={
                "style_profile": {},
                "original_image_url": "https://example.com/reference.png",
                "name": "Test Guide",
            }
        )
        fake_collection.update_one = AsyncMock(return_value=None)
        fake_db.__getitem__ = MagicMock(return_value=fake_collection)

        captured = {}

        async def fake_call_dalle_api(prompt, **kwargs):
            captured["prompt"] = prompt
            return {"success": True, "url": "data:image/png;base64,ZmFrZQ=="}

        brand_context = {
            "brand_name": "WHITE DIAMOND",
            "logo_url": "https://example.com/logo.png",
            "logo_position": "top_right",
            "logo_size": "small",
        }

        with patch.object(ImageContentService, "_call_dalle_api", side_effect=fake_call_dalle_api), patch.object(
            ImageContentService, "_overlay_logo", return_value="ZmFrZQ=="
        ), patch("cloudinary.uploader.upload", return_value={"secure_url": "https://cdn.example/final.png"}):
            result = await CustomVisualGuideV2Service.generate_image_with_v2_guide(
                guide_id="507f1f77bcf86cd799439011",
                brand_context=brand_context,
                seed_content="Announce our new listing",
                headline="New listing available",
                subtext="Book a viewing today",
                cta="Send CONSULT to begin",
                platform="instagram",
                db=fake_db,
            )

        assert result["success"] is True
        assert "prompt" in captured, "image generation call never happened"
        assert "top-right corner" in captured["prompt"]
        assert "CRITICAL - LOGO OVERLAY ZONE" in captured["prompt"]
        # Highest-weighted position in the prompt, matching the default
        # (non-V2) generation path's convention.
        assert captured["prompt"].strip().startswith("🚨 CRITICAL - LOGO OVERLAY ZONE")

    @pytest.mark.asyncio
    async def test_final_prompt_has_no_logo_note_when_brand_has_no_logo(self):
        from app.agents.social_media_manager.services.custom_visual_guide_v2_service import (
            CustomVisualGuideV2Service,
        )

        fake_db = MagicMock()
        fake_collection = MagicMock()
        fake_collection.find_one = AsyncMock(
            return_value={
                "style_profile": {},
                "original_image_url": "https://example.com/reference.png",
                "name": "Test Guide",
            }
        )
        fake_collection.update_one = AsyncMock(return_value=None)
        fake_db.__getitem__ = MagicMock(return_value=fake_collection)

        captured = {}

        async def fake_call_dalle_api(prompt, **kwargs):
            captured["prompt"] = prompt
            return {"success": True, "url": "data:image/png;base64,ZmFrZQ=="}

        brand_context = {"brand_name": "No Logo Brand"}

        with patch.object(ImageContentService, "_call_dalle_api", side_effect=fake_call_dalle_api):
            result = await CustomVisualGuideV2Service.generate_image_with_v2_guide(
                guide_id="507f1f77bcf86cd799439011",
                brand_context=brand_context,
                seed_content="Announce our new listing",
                headline="New listing available",
                subtext="Book a viewing today",
                cta="Send CONSULT to begin",
                platform="instagram",
                db=fake_db,
            )

        assert result["success"] is True
        assert "LOGO OVERLAY ZONE" not in captured["prompt"]


def _text_like_box(draw: "ImageDraw.ImageDraw", box: tuple) -> None:
    """Dense black/white stripes standing in for real rendered headline
    text — high-contrast, high-frequency detail is what a real letterform
    looks like to a blur/variance check, whether or not it's literally text."""
    x0, y0, x1, y1 = box
    for x in range(x0, x1, 5):
        draw.rectangle([x, y0, x + 2, y1], fill=(255, 255, 255))


class TestClearRegionForLogo:
    def test_erases_text_like_content_inside_the_box(self):
        img = Image.new("RGBA", (1080, 1080), (20, 90, 60, 255))
        draw = ImageDraw.Draw(img)
        box = (800, 20, 1000, 120)  # top-right-ish region
        _text_like_box(draw, box)

        before_variance = ImageStat.Stat(img.crop(box).convert("L")).var[0]
        ImageContentService._clear_region_for_logo(img, box)
        after_variance = ImageStat.Stat(img.crop(box).convert("L")).var[0]

        assert after_variance < before_variance * 0.1, (
            f"text-like content was not smoothed away: variance {before_variance:.1f} -> {after_variance:.1f}"
        )

    def test_handles_box_touching_image_edge_without_error(self):
        img = Image.new("RGBA", (400, 400), (10, 10, 10, 255))
        draw = ImageDraw.Draw(img)
        box = (0, 0, 120, 120)  # true corner — no margin available on two sides
        _text_like_box(draw, box)

        # Must not raise despite the sampling margin being clipped by the
        # image bounds on the top and left.
        ImageContentService._clear_region_for_logo(img, box)
        assert img.size == (400, 400)


class TestOverlayLogoGuaranteesCleanSurface:
    """End-to-end: even when the base image has dense text-like content
    baked directly into the logo's configured footprint (the AI ignored the
    reservation instruction, exactly as seen live), the final composited
    image must not show that content peeking out around/under the logo, and
    the logo itself must stay at the brand's configured position."""

    @staticmethod
    def _fake_logo_response():
        logo = Image.new("RGBA", (200, 200), (255, 0, 0, 255))
        buf = io.BytesIO()
        logo.save(buf, format="PNG")
        resp = Mock()
        resp.content = buf.getvalue()
        resp.raise_for_status = Mock()
        return resp

    def test_logo_position_unchanged_and_surrounding_text_cleared(self):
        size = (1080, 1080)
        img = Image.new("RGB", size, (30, 120, 80))
        draw = ImageDraw.Draw(img)
        # Cover the ENTIRE top-right quadrant with text-like content — the
        # logo's reserved footprint sits inside this, simulating the AI
        # having completely ignored the reservation instruction.
        _text_like_box(draw, (700, 0, 1080, 200))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        base_b64 = base64.b64encode(buf.getvalue()).decode()

        with patch("requests.get", return_value=self._fake_logo_response()):
            result_b64 = ImageContentService._overlay_logo(
                base_b64, "https://example.com/logo.png", position="top_right", logo_size="small"
            )

        result_img = Image.open(io.BytesIO(base64.b64decode(result_b64))).convert("RGB")
        bw, bh = result_img.size
        edge_pad = max(20, int(bw * 0.03))

        # Logo (solid red) must be present at the configured top-right spot —
        # position was never moved to dodge the busy content.
        logo_sample = result_img.crop((bw - edge_pad - 60, edge_pad, bw - edge_pad, edge_pad + 60))
        assert any(
            px[0] > 200 and px[1] < 50 and px[2] < 50 for px in logo_sample.getdata()
        ), "logo was not pasted at the configured top-right position"

        # Locate the logo's actual bounding box dynamically (rather than
        # hardcoding badge math the test would otherwise have to duplicate
        # from the implementation) so the "cleared" check inspects exactly
        # where the badge landed, not an arbitrary guess at its coordinates.
        red_pixels = [
            (x, y)
            for x in range(700, bw)
            for y in range(0, 200)
            if (px := result_img.getpixel((x, y)))[0] > 200 and px[1] < 50 and px[2] < 50
        ]
        assert red_pixels, "could not locate pasted logo pixels"
        xs, ys = zip(*red_pixels)
        # Stay INSIDE the badge's inner padding band (badge_pad_inner in the
        # source is max(5, 0.5% of image width) — 4px here is safely under
        # that at this test's 1080px width) so this margin can't reach past
        # the cleared box into the still-striped region just outside it,
        # which would fail the assertion for a reason unrelated to the fix.
        margin = 4
        badge_box = (
            max(0, min(xs) - margin),
            max(0, min(ys) - margin),
            min(bw, max(xs) + margin),
            min(200, max(ys) + margin),
        )

        # Within the badge's own footprint (logo + its immediate padding),
        # the original pure-white stripe fill must be gone — blurred into a
        # blend with the background, not left sharp and pure white.
        badge_sample = result_img.crop(badge_box)
        pure_white_count = sum(
            1 for px in badge_sample.getdata() if px[0] > 250 and px[1] > 250 and px[2] > 250
        )
        assert pure_white_count == 0, (
            f"sharp text-like content ({pure_white_count} pure-white px) still visible "
            f"inside the logo's own badge footprint — clearing did not run or was too weak"
        )
