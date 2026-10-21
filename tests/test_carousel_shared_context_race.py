"""
Carousel posts only ever applied the brand's custom visual guide to slide 0
— the rest fell back to no style at all. Confirmed live via a customer
screenshot: slide 1 branded, slides 2+ not.

Root cause: each carousel slide's image generation fires as its own
concurrent asyncio task, and the custom-guide + CTA choice for the whole
carousel used to be resolved lazily inside that per-slide task — slide 0
would resolve it and write it to content_drafts, every other slide would
try to read it back. Since all slides' tasks start at essentially the same
instant with no ordering guarantee, a slide that reached its (cheap) read
before slide 0 finished its (multi-step) write found nothing yet and fell
through to no style/CTA at all.

The fix moves that resolution out of the per-slide task entirely:
_resolve_carousel_shared_context() runs it exactly once, in the caller,
before any per-slide task is created, and every slide receives the same
already-resolved brand_context from the start — nothing left to race on.
Per-slide LIBRARY style rotation (no custom guide) was never racy to begin
with, since it's a pure function of slide_index, and stays untouched.

This also has to go through _resolve_platform_style_pool — unlike prod,
dev supports per-platform style/guide overrides, so the resolver needs the
draft's platform to pick the right pool and rotation-index field.
"""
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.social_media_manager.routers import complete_social_manager as csm
from app.agents.social_media_manager.routers.complete_social_manager import (
    _resolve_carousel_shared_context,
)


def _fake_db(brand_profile=None, custom_guide_v2=None):
    collections = {
        "brand_profiles": MagicMock(
            find_one=AsyncMock(return_value=brand_profile or {}),
            update_one=AsyncMock(return_value=None),
        ),
        "custom_visual_guides": MagicMock(
            find_one=AsyncMock(return_value=custom_guide_v2),
        ),
        "content_drafts": MagicMock(
            update_one=AsyncMock(return_value=None),
        ),
    }
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(side_effect=lambda name: collections[name])
    return fake_db, collections


class TestResolveCarouselSharedContextCustomGuide:
    @pytest.mark.asyncio
    async def test_v2_guide_resolves_once_and_flags_as_resolved(self):
        db, cols = _fake_db(
            brand_profile={
                "selected_custom_guides_v2": ["507f1f77bcf86cd799439011"],
                "style_rotation_index": 0,
            },
            custom_guide_v2={"original_image_url": "https://example.com/ref.png", "name": "Guide A"},
        )
        brand_context = {"brand_id": "brand-1", "user_id": "u1"}

        resolved = await _resolve_carousel_shared_context(
            db=db, brand_context=brand_context, style_override=None,
            carousel_id="carousel-1", platform="instagram",
        )

        assert resolved["custom_guide_v2_id"] == "507f1f77bcf86cd799439011"
        assert resolved["custom_guide_v2_reference_image"] == "https://example.com/ref.png"
        assert resolved["style_slug"].startswith("custom_v2_")
        assert resolved["_carousel_context_resolved"] is True
        # The original brand_context passed in must be untouched — it's reused
        # across other drafts/carousels in the same request.
        assert "custom_guide_v2_id" not in brand_context

        assert cols["content_drafts"].update_one.await_count == 2
        guide_write = cols["content_drafts"].update_one.call_args_list[0]
        assert guide_write[0][0] == {"id": "carousel-1"}
        assert guide_write[0][1]["$set"]["carousel_custom_guide_v2_id"] == "507f1f77bcf86cd799439011"

    @pytest.mark.asyncio
    async def test_no_custom_guide_falls_back_to_default_link_cta(self):
        db, cols = _fake_db(brand_profile={})
        brand_context = {"brand_id": "brand-1", "default_link": "uri.social/book"}

        resolved = await _resolve_carousel_shared_context(
            db=db, brand_context=brand_context, style_override=None,
            carousel_id="carousel-2", platform="facebook",
        )

        assert "custom_guide_v2_id" not in resolved
        assert resolved["override_cta"] == "uri.social/book"
        assert resolved["_carousel_context_resolved"] is True

    @pytest.mark.asyncio
    async def test_style_override_skips_custom_guide_even_when_configured(self):
        db, cols = _fake_db(
            brand_profile={"selected_custom_guides_v2": ["507f1f77bcf86cd799439011"]},
        )
        brand_context = {"brand_id": "brand-1"}

        resolved = await _resolve_carousel_shared_context(
            db=db, brand_context=brand_context, style_override=["minimalist"],
            carousel_id="carousel-3", platform="instagram",
        )

        assert "custom_guide_v2_id" not in resolved

    @pytest.mark.asyncio
    async def test_cta_styles_rotation_picked_once(self):
        db, cols = _fake_db(brand_profile={})
        brand_context = {"brand_id": "brand-1", "cta_styles": ["DM us", "Call now"], "cta_rotation_index": 1}

        resolved = await _resolve_carousel_shared_context(
            db=db, brand_context=brand_context, style_override=None,
            carousel_id="carousel-4", platform="instagram",
        )

        assert resolved["override_cta"] == "Call now"

    @pytest.mark.asyncio
    async def test_per_platform_guide_override_used_when_present(self):
        db, cols = _fake_db(
            brand_profile={
                "selected_custom_guides_v2": [],  # no brand-wide guide
                "selected_custom_guides_v2_by_platform": {"instagram": ["507f1f77bcf86cd799439011"]},
                "style_rotation_index_by_platform": {"instagram": 0},
            },
            custom_guide_v2={"original_image_url": "https://example.com/ig-ref.png", "name": "IG-only Guide"},
        )
        brand_context = {"brand_id": "brand-1"}

        resolved = await _resolve_carousel_shared_context(
            db=db, brand_context=brand_context, style_override=None,
            carousel_id="carousel-5", platform="instagram",
        )

        assert resolved["custom_guide_v2_id"] == "507f1f77bcf86cd799439011"
        # Rotation-index write-back must target the per-platform field, not
        # the brand-wide one, when a per-platform override is what resolved.
        guide_write_bp = cols["brand_profiles"].update_one.call_args_list[0]
        assert "style_rotation_index_by_platform.instagram" in guide_write_bp[0][1]["$set"]


class TestGenerateImageBgSkipsWhenAlreadyResolved:
    def test_carousel_branch_checks_resolved_flag_before_redoing_work(self):
        source = inspect.getsource(csm._generate_image_bg)
        assert '_carousel_context_resolved' in source
        assert '_carousel_context_already_resolved' in source
        assert 'if _all_custom_guide_ids and _carousel_context_already_resolved:' in source
        assert 'if _carousel_context_already_resolved:' in source

    def test_caller_resolves_once_before_spawning_per_slide_tasks(self):
        source = inspect.getsource(csm)
        resolve_idx = source.index("carousel_brand_context = await _resolve_carousel_shared_context")
        loop_idx = source.index("for slide_index, slide in enumerate(slides):")
        task_call_idx = source.index("brand_context=carousel_brand_context")
        assert resolve_idx < loop_idx < task_call_idx


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
