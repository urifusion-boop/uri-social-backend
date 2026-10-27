"""
create_draft_from_item_v2 (content_calendar_v2_router.py) — regression for a
live bug: checking "Include AI-generated image" on any non-carousel calendar
item crashed with a 500 (NameError: exact_copy), after the draft and its
credit deduction had already gone through. The frontend only ever showed
"Failed to create draft."

Root cause: `exact_copy` is a local variable inside `_build_draft_seed_content`
(a separate function this endpoint calls), not something the endpoint itself
ever defined — the single-image include_images branch referenced it anyway.
test_calendar_v2_single_image_headline.py already unit-tests
`_single_image_seed_with_headline` in isolation with hand-built args, which
is exactly why this was never caught: nothing exercised the real call site.
This test does, with an in-memory MongoDB and the credit/content-generation/
brand-profile services mocked — it isn't testing AI output quality, just that
the endpoint doesn't raise on this path.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.content_calendar_v2.models import CreateDraftRequestV2
from app.agents.content_calendar_v2.routers.content_calendar_v2_router import (
    create_draft_from_item_v2,
)

USER_ID = "user-1"
BRAND_ID = "brand-1"
PLAN_ID = "plan-1"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    client = mongomock_motor.AsyncMongoMockClient()
    return client["test_db"]


def _plan_doc(**item_overrides):
    item = {
        "day_index": 0,
        "format": "single_image",
        "title": "Why URI is Your Best Decision Yet",
        "description": "desc",
        "hook": "Struggling to choose the right platform? URI is the solution.",
        "exact_copy": {"headline": "Why URI is Your Best Decision Yet", "caption": "caption"},
        "ai_image_prompt": "A tidy desk photo. no text, no logos.",
    }
    item.update(item_overrides)
    return {
        "plan_id": PLAN_ID,
        "user_id": USER_ID,
        "brand_id": BRAND_ID,
        "status": "active",
        "items": [item],
    }


class TestCreateDraftIncludeImagesDoesNotCrash:
    def test_single_image_item_with_include_images_does_not_raise(self):
        db = fresh_db()
        run(db["content_calendar_v2_plans"].insert_one(_plan_doc()))

        fake_draft = {"draft_id": "draft-1", "platform": "instagram", "content": "post text"}
        fake_generation_result = {
            "status": True,
            "responseData": {"drafts": [fake_draft], "request_id": "req-1"},
        }

        with (
            # credit_service/trial_service are imported locally inside the
            # endpoint (`from app.services.CreditService import
            # credit_service`), so the patch has to target the source
            # module, not this router's own namespace — it has no
            # module-level attribute by that name for patch() to replace.
            patch("app.services.CreditService.credit_service") as mock_credit_service,
            patch("app.services.TrialService.trial_service") as mock_trial_service,
            patch(
                "app.agents.content_calendar_v2.routers.content_calendar_v2_router.BrandProfileService"
            ) as mock_brand_service,
            patch(
                "app.agents.content_calendar_v2.routers.content_calendar_v2_router.ContentGenerationService"
            ) as mock_content_service,
            patch(
                "app.agents.social_media_manager.routers.complete_social_manager._generate_image_bg",
                new_callable=AsyncMock,
            ),
            # mark_acted_on_v2's $[it] array-filter update isn't implemented
            # by mongomock (see test_calendar_regenerate_scope.py's own
            # _ArrayFilterCollection workaround) — irrelevant to what this
            # test checks, so it's mocked out rather than worked around.
            patch(
                "app.agents.content_calendar_v2.routers.content_calendar_v2_router.cal_v2_svc.mark_acted_on_v2",
                new_callable=AsyncMock,
            ),
        ):
            mock_trial_service.has_active_trial = AsyncMock(return_value=False)
            mock_credit_service.check_sufficient_credits = AsyncMock(return_value=True)
            mock_credit_service.deduct_credit = AsyncMock(return_value=True)
            mock_brand_service.get = AsyncMock(return_value={"status": True, "responseData": {}})
            mock_brand_service.to_brand_context = lambda raw: {}
            mock_content_service.generate_multi_platform_content = AsyncMock(
                return_value=fake_generation_result
            )

            result = run(
                create_draft_from_item_v2(
                    plan_id=PLAN_ID,
                    item_index=0,
                    request=CreateDraftRequestV2(platforms=["instagram"], include_images=True),
                    db=db,
                    ctx={"user_id": USER_ID, "brand_id": BRAND_ID},
                )
            )

        assert result["status"] is True
        assert result["responseData"]["drafts"][0]["draft_id"] == "draft-1"
