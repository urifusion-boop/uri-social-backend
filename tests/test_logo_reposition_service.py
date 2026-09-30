"""LogoRepositionService — the fresh, purpose-built path for moving/resizing
a logo on an already-generated draft (deliberately separate from the
existing, currently-broken Canvas Editor). Covers the real failure modes:
no saved background (a draft from before this shipped), no brand logo
configured, wrong brand's draft, and the happy path for both a regular
post and one carousel slide."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.social_media_manager.services.logo_reposition_service import (
    LogoRepositionService,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self, doc):
        self.doc = doc
        self.updates = []

    async def find_one(self, query):
        return self.doc

    async def update_one(self, query, update):
        self.updates.append((query, update))
        return MagicMock(matched_count=1)


class FakeDb(dict):
    def __getitem__(self, name):
        return self.setdefault(name, FakeCollection(None))


def _profile_result(logo_url):
    return {
        "status": True,
        "responseData": {"logo_url": logo_url} if logo_url else {},
    }


class TestReposition:
    def test_draft_not_found(self):
        db = FakeDb()
        db["content_drafts"] = FakeCollection(None)
        result = _run(LogoRepositionService.reposition(
            "missing", "u1", 10, 10, 50, 50, db
        ))
        assert result["status"] is False
        assert "not found" in result["responseMessage"].lower()

    def test_no_saved_background_gives_actionable_error(self):
        db = FakeDb()
        db["content_drafts"] = FakeCollection({"id": "d1", "brand_id": "b1", "image_url": "https://x/img.png"})
        result = _run(LogoRepositionService.reposition("d1", "u1", 10, 10, 50, 50, db))
        assert result["status"] is False
        assert "regenerate" in result["responseMessage"].lower()

    def test_non_positive_dimensions_rejected(self):
        db = FakeDb()
        db["content_drafts"] = FakeCollection({
            "id": "d1", "brand_id": "b1", "background_image_url": "https://x/bg.png",
        })
        result = _run(LogoRepositionService.reposition("d1", "u1", 10, 10, 0, 50, db))
        assert result["status"] is False

    def test_no_brand_logo_configured(self):
        db = FakeDb()
        db["content_drafts"] = FakeCollection({
            "id": "d1", "brand_id": "b1", "background_image_url": "https://x/bg.png",
        })
        with patch(
            "app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
            new=AsyncMock(return_value=_profile_result(None)),
        ):
            result = _run(LogoRepositionService.reposition("d1", "u1", 10, 10, 50, 50, db))
        assert result["status"] is False
        assert "logo" in result["responseMessage"].lower()

    def test_happy_path_regular_post_updates_image_url_and_placement(self):
        db = FakeDb()
        drafts = FakeCollection({
            "id": "d1", "brand_id": "b1", "background_image_url": "https://x/bg.png",
        })
        db["content_drafts"] = drafts

        with patch(
            "app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
            new=AsyncMock(return_value=_profile_result("https://x/logo.png")),
        ), patch(
            "app.agents.social_media_manager.services.image_content_service.ImageContentService.composite_logo_at_position",
            new=AsyncMock(return_value="ZmFrZQ=="),
        ), patch(
            "app.utils.cloudinary_upload.upload_base64",
            new=AsyncMock(return_value="https://s3.example.com/uri-social/content-drafts/new.webp"),
        ):
            result = _run(LogoRepositionService.reposition("d1", "u1", 12, 34, 100, 60, db))

        assert result["status"] is True
        assert result["responseData"]["image_url"] == "https://s3.example.com/uri-social/content-drafts/new.webp"
        assert result["responseData"]["logo_placement"] == {"x": 12, "y": 34, "width": 100, "height": 60}

        assert len(drafts.updates) == 1
        query, update = drafts.updates[0]
        assert query == {"id": "d1"}
        assert update["$set"]["image_url"] == "https://s3.example.com/uri-social/content-drafts/new.webp"
        assert update["$set"]["logo_placement"] == {"x": 12, "y": 34, "width": 100, "height": 60}

    def test_happy_path_carousel_slide_updates_only_that_slide(self):
        db = FakeDb()
        drafts = FakeCollection({
            "id": "d1",
            "brand_id": "b1",
            "slides": [
                {"background_image_url": None},
                {"background_image_url": "https://x/bg-slide1.png"},
            ],
        })
        db["content_drafts"] = drafts

        with patch(
            "app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
            new=AsyncMock(return_value=_profile_result("https://x/logo.png")),
        ), patch(
            "app.agents.social_media_manager.services.image_content_service.ImageContentService.composite_logo_at_position",
            new=AsyncMock(return_value="ZmFrZQ=="),
        ) as mock_composite, patch(
            "app.utils.cloudinary_upload.upload_base64",
            new=AsyncMock(return_value="https://s3.example.com/slide1-new.webp"),
        ):
            result = _run(LogoRepositionService.reposition(
                "d1", "u1", 5, 6, 40, 20, db, slide_index=1
            ))

        assert result["status"] is True
        mock_composite.assert_awaited_once_with("https://x/bg-slide1.png", "https://x/logo.png", 5, 6, 40, 20)

        _, update = drafts.updates[0]
        assert update["$set"]["slides.1.image_url"] == "https://s3.example.com/slide1-new.webp"
        assert update["$set"]["slides.1.logo_placement"] == {"x": 5, "y": 6, "width": 40, "height": 20}
        assert "slides.0.image_url" not in update["$set"]

    def test_invalid_slide_index(self):
        db = FakeDb()
        db["content_drafts"] = FakeCollection({
            "id": "d1", "brand_id": "b1", "slides": [{"background_image_url": "https://x/bg.png"}],
        })
        result = _run(LogoRepositionService.reposition("d1", "u1", 1, 1, 10, 10, db, slide_index=5))
        assert result["status"] is False

    def test_composite_failure_returns_error_not_exception(self):
        db = FakeDb()
        db["content_drafts"] = FakeCollection({
            "id": "d1", "brand_id": "b1", "background_image_url": "https://x/bg.png",
        })
        with patch(
            "app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
            new=AsyncMock(return_value=_profile_result("https://x/logo.png")),
        ), patch(
            "app.agents.social_media_manager.services.image_content_service.ImageContentService.composite_logo_at_position",
            new=AsyncMock(side_effect=Exception("logo host unreachable")),
        ):
            result = _run(LogoRepositionService.reposition("d1", "u1", 1, 1, 10, 10, db))
        assert result["status"] is False
        assert "logo host unreachable" in result["responseMessage"]
