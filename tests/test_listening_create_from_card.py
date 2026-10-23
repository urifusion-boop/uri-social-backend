"""
POST /listening/cards/{card_id}/create — PRD §9.3's "Create" deep-link:
pass a card's evidence-backed brief straight into the content generator
instead of the owner re-typing it. Scoped to text drafts only; no AI image
generation or credit deduction here (that stays on the normal Create
Content flow). Uses an in-memory MongoDB; the AI-backed generation service
and the Brand Playbook lookup are both stubbed.
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from fastapi import HTTPException

from app.agents.market_intelligence.listening.router import create_from_card

BRAND_ID, USER_ID = "brand-1", "user-1"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["create_from_card_test"]


async def _card(db, card_id="c1", platforms=("instagram", "tiktok"), content_id=None, action_type="carousel"):
    doc = {
        "id": card_id, "brand_id": BRAND_ID, "market": "hair_wigs:NG", "topic": "latasha wig", "type": "rising",
        "why": "Fits your catalogue.", "observation": "Rising in your market.",
        "action": {"type": action_type, "title": "Film 3 ways to style it", "platforms": list(platforms),
                   "format": "Reel 30s", "hook": "Everyone's asking about this wig.",
                   "outline": ["Show it on", "Style it two ways"], "cta": "DM to order", "caption": "Caption"},
        "scores": {"total": 80}, "status": "shown", "content_id": content_id,
    }
    await db["mi_listen_cards"].insert_one(dict(doc))
    return doc


CTX = {"brand_id": BRAND_ID, "user_id": USER_ID}

_PATCH_PLAYBOOK = patch(
    "app.agents.market_intelligence.listening.router._playbook", new=AsyncMock(return_value={"brand_name": "WD"})
)
_PATCH_TO_BRAND_CONTEXT = patch(
    "app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.to_brand_context",
    return_value={"brand_name": "WD"},
)


def _ok_generation(draft_ids=("d1", "d2")):
    return {
        "status": True,
        "responseData": {
            "request_id": "req-1",
            "drafts": [{"id": d, "platform": "instagram"} for d in draft_ids],
        },
    }


class TestCreateFromCard:
    @pytest.mark.asyncio
    async def test_happy_path_generates_drafts_and_updates_the_card(self):
        db = fresh_db()
        await _card(db)
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(return_value=_ok_generation()),
        ) as mock_gen:
            result = await create_from_card("c1", ctx=CTX, db=db)

        assert result["responseData"]["draft_ids"] == ["d1", "d2"]
        assert mock_gen.call_args.kwargs["platforms"] == ["instagram", "tiktok"]
        assert mock_gen.call_args.kwargs["user_id"] == USER_ID
        assert "Everyone's asking" in mock_gen.call_args.kwargs["seed_content"]
        assert "DM to order" in mock_gen.call_args.kwargs["seed_content"]

        card = await db["mi_listen_cards"].find_one({"id": "c1"})
        assert card["status"] == "acted" and card["content_id"] == "d1"
        assert card["created_draft_ids"] == ["d1", "d2"]
        action = await db["mi_listen_card_actions"].find_one({"card_id": "c1"})
        assert action["action"] == "created" and action["content_id"] == "d1"

    @pytest.mark.asyncio
    async def test_missing_card_is_404(self):
        db = fresh_db()
        with pytest.raises(HTTPException) as exc:
            await create_from_card("ghost", ctx=CTX, db=db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_already_created_is_409_and_does_not_regenerate(self):
        db = fresh_db()
        await _card(db, content_id="already-d1")
        with patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(),
        ) as mock_gen:
            with pytest.raises(HTTPException) as exc:
                await create_from_card("c1", ctx=CTX, db=db)
        assert exc.value.status_code == 409
        mock_gen.assert_not_called()

    @pytest.mark.asyncio
    async def test_card_with_no_platforms_is_400(self):
        db = fresh_db()
        await _card(db, platforms=())
        with pytest.raises(HTTPException) as exc:
            await create_from_card("c1", ctx=CTX, db=db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_another_brands_card_is_invisible(self):
        db = fresh_db()
        await _card(db)
        with pytest.raises(HTTPException) as exc:
            await create_from_card("c1", ctx={"brand_id": "someone-else", "user_id": "u2"}, db=db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_video_action_returns_a_prefilled_brief_not_a_fake_text_draft(self):
        # Confirmed live, once: a card labelled "Format: video" silently
        # generated a plain text draft when clicked, with nothing telling the
        # owner anything was off. Now it must hand back a real brief for the
        # storyboard screen instead — never a text draft masquerading as video,
        # and never an error either, since the brief itself is real and usable.
        db = fresh_db()
        await _card(db, action_type="video")
        with patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(),
        ) as mock_gen:
            result = await create_from_card("c1", ctx=CTX, db=db)
        mock_gen.assert_not_called()
        data = result["responseData"]
        assert "Everyone's asking" in data["optional_text"]
        assert "DM to order" in data["optional_text"]
        assert data["target_platform"] == "instagram"
        card = await db["mi_listen_cards"].find_one({"id": "c1"})
        # No draft/video was actually created — nothing to be idempotent about.
        assert card["status"] == "shown" and card["content_id"] is None

    @pytest.mark.asyncio
    async def test_ad_test_action_opens_a_prefilled_draft_thread_never_auto_launched(self):
        db = fresh_db()
        await _card(db, action_type="ad_test")
        with patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(),
        ) as mock_gen:
            result = await create_from_card("c1", ctx=CTX, db=db)
        mock_gen.assert_not_called()
        data = result["responseData"]
        assert data["thread"]["status"] == "draft"
        assert data["thread"]["brand_id"] == BRAND_ID
        assert "Everyone's asking" in data["seed_message"]
        card = await db["mi_listen_cards"].find_one({"id": "c1"})
        assert card["status"] == "acted" and card["content_id"] == data["thread"]["thread_id"]

    @pytest.mark.asyncio
    async def test_ad_test_action_is_idempotent_like_every_other_action(self):
        db = fresh_db()
        await _card(db, action_type="ad_test")
        first = await create_from_card("c1", ctx=CTX, db=db)
        with pytest.raises(HTTPException) as exc:
            await create_from_card("c1", ctx=CTX, db=db)
        assert exc.value.status_code == 409
        assert first["responseData"]["thread"]["thread_id"]

    @pytest.mark.asyncio
    async def test_post_and_faq_actions_still_generate_text_normally(self):
        # Both route through the same text generator as carousel — only
        # video/ad_test need their own, not-yet-built generators.
        for action_type in ("post", "faq"):
            db = fresh_db()
            await _card(db, action_type=action_type)
            with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, patch(
                "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
                ".generate_multi_platform_content",
                new=AsyncMock(return_value=_ok_generation()),
            ):
                result = await create_from_card("c1", ctx=CTX, db=db)
            assert result["responseData"]["draft_ids"] == ["d1", "d2"]

    @pytest.mark.asyncio
    async def test_generation_failure_is_502_and_card_is_left_untouched(self):
        db = fresh_db()
        await _card(db)
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(return_value={"status": False, "responseMessage": "all platforms failed"}),
        ):
            with pytest.raises(HTTPException) as exc:
                await create_from_card("c1", ctx=CTX, db=db)
        assert exc.value.status_code == 502
        card = await db["mi_listen_cards"].find_one({"id": "c1"})
        assert card["status"] == "shown" and card["content_id"] is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
