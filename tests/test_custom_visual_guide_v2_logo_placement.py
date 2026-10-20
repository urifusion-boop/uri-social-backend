"""
Move Logo only ever worked for images generated through the standard
generation path. A brand with a Custom Visual Guide (V2) selected never saw
it at all, on any post, regardless of platform — confirmed live: an account
with 3 custom guides selected had zero logo_placement data across every
recent post, while a guide-free account had it on every post.

generate_image_with_v2_guide already composited the logo via the same
smart-overlay path AI-generated images use (overlay_logo_smart), but called
it without return_placement=True and never captured the pre-overlay
background, so there was nothing to persist and nothing to later re-paste
the logo onto. The result was also normalized down to just {"image_url":
...} one level up in complete_social_manager.py, silently discarding
background_image_url/logo_placement even after they existed.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.social_media_manager.services.custom_visual_guide_v2_service import (
    CustomVisualGuideV2Service,
)
from app.agents.social_media_manager.services.image_content_service import ImageContentService


def _fake_db():
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
    return fake_db


class TestV2GuideReturnsLogoPlacement:
    @pytest.mark.asyncio
    async def test_logo_placement_and_background_flow_through_when_logo_configured(self):
        async def fake_call_dalle_api(prompt, **kwargs):
            return {"success": True, "url": "data:image/png;base64,ZmFrZQ=="}

        brand_context = {
            "brand_name": "WHITE DIAMOND",
            "logo_url": "https://example.com/logo.png",
            "logo_position": "top_right",
            "logo_size": "large",
        }

        # overlay_logo_smart(return_placement=True) returns a flat
        # (b64, chosen_position, chosen_geometry) tuple, not a nested one —
        # chosen_position can differ from the preferred one if the smart
        # ranking moved off it to avoid a busy area.
        fake_geometry = {"x": 820, "y": 15, "width": 200, "height": 200}

        async def fake_overlay_logo_smart(b64, logo_url, logo_size, preferred_position, return_placement=False):
            assert return_placement is True
            return "ZmFrZQ==", "top_left", fake_geometry

        with patch.object(ImageContentService, "_call_dalle_api", side_effect=fake_call_dalle_api), patch.object(
            ImageContentService, "overlay_logo_smart", side_effect=fake_overlay_logo_smart
        ), patch("app.utils.s3_upload.upload_bytes", new=AsyncMock(return_value="https://cdn.example/final.png")):
            result = await CustomVisualGuideV2Service.generate_image_with_v2_guide(
                guide_id="507f1f77bcf86cd799439011",
                brand_context=brand_context,
                seed_content="Announce our new listing",
                headline="New listing available",
                subtext="Book a viewing today",
                cta="Send CONSULT to begin",
                platform="instagram",
                db=_fake_db(),
            )

        assert result["success"] is True
        # The corner the smart ranking actually chose, not the preferred one
        # — logo_placement must reflect where the logo really landed.
        assert result["logo_placement"] == {**fake_geometry, "position": "top_left", "logo_size": "large"}
        assert result["background_image_url"] == "data:image/webp;base64,ZmFrZQ=="
        # responseData mirrors the top-level fields — this is what the
        # caller (complete_social_manager.py) actually reads from.
        assert result["responseData"]["logo_placement"] == result["logo_placement"]
        assert result["responseData"]["background_image_url"] == result["background_image_url"]

    @pytest.mark.asyncio
    async def test_no_logo_configured_leaves_placement_fields_none(self):
        async def fake_call_dalle_api(prompt, **kwargs):
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
                db=_fake_db(),
            )

        assert result["success"] is True
        assert result["logo_placement"] is None
        assert result["background_image_url"] is None
