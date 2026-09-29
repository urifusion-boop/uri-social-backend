"""The brand's chosen logo position is a user decision, not something the AI
image generator gets to override — these cover
ImageContentService.build_logo_space_note, the shared "reserve this corner"
instruction told to the image model, and confirm it's wired into the V2
custom-guide prompt path, which was previously missing it entirely.

Deliberately NOT covered here: any kind of post-generation pixel patch/blur
behind the logo. That approach was tried and reverted — it left a visible
box/smudge behind the logo over detailed backgrounds and risked silently
destroying real headline text that happened to land in that rectangle,
which is worse than the original overlap. The logo must sit directly on
whatever the AI actually drew, no exceptions, no background of any kind."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
