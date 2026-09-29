"""The brand's chosen logo position is a user decision, not something the AI
image generator gets to override — these cover
ImageContentService.build_logo_space_note, the shared "reserve this corner"
instruction told to the image model, and confirm it's wired into the V2
custom-guide prompt path, which was previously missing it entirely. Also
covers _overlay_logo's padding-crop step: a logo file exported with extra
transparent margin around the actual mark otherwise makes "large" still
look small, since the size percentage is computed against the file's full
canvas rather than its visible content.

Deliberately NOT covered here: any kind of post-generation pixel patch/blur
behind the logo. That approach was tried and reverted — it left a visible
box/smudge behind the logo over detailed backgrounds and risked silently
destroying real headline text that happened to land in that rectangle,
which is worse than the original overlap. The logo must sit directly on
whatever the AI actually drew, no exceptions, no background of any kind.
The padding-crop step below is different in kind: it only ever trims the
logo's OWN transparent edges before pasting it — it never touches the
generated image's pixels."""
import base64
import io
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from PIL import Image

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


def _fake_response(image: "Image.Image"):
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    resp = Mock()
    resp.content = buf.getvalue()
    resp.raise_for_status = Mock()
    return resp


def _base_image(size=(1000, 1000)) -> str:
    img = Image.new("RGB", size, (20, 90, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class TestOverlayLogoCropsOwnPadding:
    def test_padded_logo_fills_the_configured_size_after_crop(self):
        """A logo file with a small visible mark centred in a much larger
        transparent canvas (the Canva/Figma-export pattern that makes "large"
        still look small) — after the fix, the pasted mark should occupy
        close to the FULL configured target width, not a fraction of it."""
        canvas = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
        # Solid red mark in the centre — the actual "visible logo".
        for x in range(80, 120):
            for y in range(80, 120):
                canvas.putpixel((x, y), (255, 0, 0, 255))

        base_b64 = _base_image((1000, 1000))

        with patch("requests.get", return_value=_fake_response(canvas)):
            result_b64 = ImageContentService._overlay_logo(
                base_b64, "https://example.com/logo.png", position="bottom_right", logo_size="large"
            )

        result_img = Image.open(io.BytesIO(base64.b64decode(result_b64))).convert("RGB")
        red_xs = [x for x in range(result_img.width) if result_img.getpixel((x, result_img.height - 100))[0] > 200]

        target_w = max(40, int(1000 * 0.16))  # "large" = 16%
        visible_span = (max(red_xs) - min(red_xs)) if red_xs else 0
        assert visible_span > target_w * 0.7, (
            f"visible logo mark only spans {visible_span}px against a {target_w}px target — "
            f"padding was not cropped out"
        )

    def test_logo_with_no_padding_is_unaffected(self):
        """A logo that already tightly fills its canvas (no transparent
        margin) must render identically to before — the crop is a no-op."""
        canvas = Image.new("RGBA", (100, 100), (255, 0, 0, 255))  # fully opaque, edge-to-edge
        base_b64 = _base_image((1000, 1000))

        with patch("requests.get", return_value=_fake_response(canvas)):
            result_b64 = ImageContentService._overlay_logo(
                base_b64, "https://example.com/logo.png", position="bottom_right", logo_size="large"
            )

        result_img = Image.open(io.BytesIO(base64.b64decode(result_b64))).convert("RGB")
        red_xs = [x for x in range(result_img.width) if result_img.getpixel((x, result_img.height - 100))[0] > 200]
        target_w = max(40, int(1000 * 0.16))
        assert red_xs and (max(red_xs) - min(red_xs)) > target_w * 0.9

    def test_opaque_rectangular_logo_background_is_not_cropped_away(self):
        """A logo deliberately designed with a solid opaque background plate
        (alpha=255 everywhere, e.g. a logo shipped on a solid colour card)
        must be left as the brand designed it — getbbox() on the alpha
        channel alone correctly reports the full canvas as content in this
        case, so nothing gets trimmed."""
        canvas = Image.new("RGBA", (150, 80), (10, 10, 10, 255))
        for x in range(50, 100):
            for y in range(20, 60):
                canvas.putpixel((x, y), (255, 255, 0, 255))
        base_b64 = _base_image((1000, 1000))

        with patch("requests.get", return_value=_fake_response(canvas)):
            result_b64 = ImageContentService._overlay_logo(
                base_b64, "https://example.com/logo.png", position="bottom_right", logo_size="small"
            )

        result_img = Image.open(io.BytesIO(base64.b64decode(result_b64))).convert("RGB")
        # The dark plate itself should be visible near the logo's corner —
        # if it had been (wrongly) cropped away, only the yellow mark would
        # show, with the base image's own colour immediately around it.
        dark_xs = [
            x for x in range(result_img.width)
            if sum(result_img.getpixel((x, result_img.height - 60))[:3]) < 60
        ]
        assert dark_xs, "logo's own opaque background plate was cropped away"
