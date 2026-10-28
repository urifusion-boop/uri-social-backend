"""
generate_content (complete_social_manager.py) — this is the real Create
Content screen's endpoint, the highest-traffic path in the app. Same
regression as test_calendar_v2_create_draft_include_images.py and
test_listening_create_from_card.py's
test_image_task_setup_failure_marks_drafts_failed_instead_of_stuck_forever:
anything that throws between the optimistic has_image=True write and the
asyncio.create_task calls used to leave the draft stuck at
has_image=True/image_failed=False forever — the one background task that
would ever flip image_failed never got created, so the frontend's "is the
image ready yet?" poll had nothing left to ever resolve it. Carousel
generation hits this via _resolve_carousel_shared_context; the
single-image generate_content path doesn't call any function with the same
kind of setup risk today, so the carousel branch is what's exercised here
— the same try/except now wraps both branches regardless.

Uses an in-memory MongoDB; credit/trial services and the real AI-backed
generation services are mocked — this tests the image-task error-handling
behavior, not AI output quality or credit math (those have their own
coverage elsewhere, e.g. test_credit_service_comp_exhaustion.py).
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from fastapi import BackgroundTasks, Response

from app.agents.social_media_manager.routers.complete_social_manager import (
    ContentGenerationRequest,
    generate_content,
)

USER_ID = "user-1"
BRAND_ID = "brand-1"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    client = mongomock_motor.AsyncMongoMockClient()
    return client["generate_content_test"]


CTX = {"user_id": USER_ID, "brand_id": BRAND_ID}

_COMPLETE_PROFILE = {
    "status": True,
    "responseData": {
        "brand_name": "Test Brand",
        "brand_colors": ["#000000"],
        "industry": "beauty",
    },
}


def _ok_carousel(draft_id="d1"):
    return {
        "status": True,
        "responseData": {
            "request_id": "req-1",
            "drafts": [
                {
                    "id": draft_id,
                    "platform": "instagram",
                    "content": "caption",
                    "slides": [
                        {"headline": "Slide 1", "body": "body 1"},
                        {"headline": "Slide 2", "body": "body 2"},
                    ],
                }
            ],
        },
    }


class TestGenerateContentImageTaskSetup:
    def test_carousel_image_setup_failure_marks_drafts_failed_instead_of_stuck_forever(self):
        db = fresh_db()
        # CarouselGenerationService.generate_multi_platform is mocked (this
        # test is about image-task setup, not real carousel generation), so
        # it won't insert the draft doc it normally would — pre-insert the
        # same shape a real call would have left behind, so the image-task
        # setup has a real document to mark image_failed on.
        run(db["content_drafts"].insert_one({"id": "d1", "platform": "instagram", "content": "caption"}))

        request = ContentGenerationRequest(
            seed_content="A seed sentence that is definitely long enough.",
            platforms=["instagram"],
            post_type="carousel",
            include_images=True,
            num_slides=2,
        )

        with (
            patch("app.services.CreditService.credit_service") as mock_credit_service,
            patch("app.services.TrialService.trial_service") as mock_trial_service,
            patch(
                "app.agents.social_media_manager.routers.complete_social_manager.BrandProfileService"
            ) as mock_brand_service,
            patch(
                "app.agents.social_media_manager.services.carousel_generation_service.CarouselGenerationService"
                ".generate_multi_platform",
                new=AsyncMock(return_value=_ok_carousel()),
            ),
            patch(
                "app.agents.social_media_manager.routers.complete_social_manager"
                "._resolve_carousel_shared_context",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(
                "app.agents.social_media_manager.routers.complete_social_manager._generate_image_bg",
                new_callable=AsyncMock,
            ),
        ):
            mock_trial_service.has_active_trial = AsyncMock(return_value=False)
            mock_credit_service.check_sufficient_credits = AsyncMock(return_value=True)
            mock_credit_service.deduct_credit = AsyncMock(return_value=True)
            mock_brand_service.get = AsyncMock(return_value=_COMPLETE_PROFILE)
            mock_brand_service.to_brand_context = lambda raw: dict(raw)

            result = run(
                generate_content(
                    request=request,
                    background_tasks=BackgroundTasks(),
                    response=Response(),
                    db=db,
                    ctx=CTX,
                )
            )

        # The request itself must still succeed — the draft's real text
        # content was genuinely generated; only the image-task setup failed.
        assert result["status"] is True
        assert result["responseData"]["drafts"][0]["id"] == "d1"

        draft = run(db["content_drafts"].find_one({"id": "d1"}))
        assert draft["image_failed"] is True

    def test_carousel_happy_path_still_creates_background_image_tasks(self):
        """Sanity check alongside the regression above: the fix must not
        swallow genuine successes — when nothing throws, image tasks are
        still created as before."""
        db = fresh_db()
        run(db["content_drafts"].insert_one({"id": "d1", "platform": "instagram", "content": "caption"}))

        request = ContentGenerationRequest(
            seed_content="A seed sentence that is definitely long enough.",
            platforms=["instagram"],
            post_type="carousel",
            include_images=True,
            num_slides=2,
        )

        with (
            patch("app.services.CreditService.credit_service") as mock_credit_service,
            patch("app.services.TrialService.trial_service") as mock_trial_service,
            patch(
                "app.agents.social_media_manager.routers.complete_social_manager.BrandProfileService"
            ) as mock_brand_service,
            patch(
                "app.agents.social_media_manager.services.carousel_generation_service.CarouselGenerationService"
                ".generate_multi_platform",
                new=AsyncMock(return_value=_ok_carousel()),
            ),
            patch(
                "app.agents.social_media_manager.routers.complete_social_manager"
                "._resolve_carousel_shared_context",
                new=AsyncMock(return_value={"brand_name": "Test Brand"}),
            ),
            patch(
                "app.agents.social_media_manager.routers.complete_social_manager._generate_image_bg",
                new_callable=AsyncMock,
            ) as mock_image_bg,
        ):
            mock_trial_service.has_active_trial = AsyncMock(return_value=False)
            mock_credit_service.check_sufficient_credits = AsyncMock(return_value=True)
            mock_credit_service.deduct_credit = AsyncMock(return_value=True)
            mock_brand_service.get = AsyncMock(return_value=_COMPLETE_PROFILE)
            mock_brand_service.to_brand_context = lambda raw: dict(raw)

            result = run(
                generate_content(
                    request=request,
                    background_tasks=BackgroundTasks(),
                    response=Response(),
                    db=db,
                    ctx=CTX,
                )
            )

        assert result["status"] is True
        # One task per slide (2 slides).
        assert mock_image_bg.await_count == 2

        draft = run(db["content_drafts"].find_one({"id": "d1"}))
        assert draft["has_image"] is True
        assert draft.get("image_failed") is False
