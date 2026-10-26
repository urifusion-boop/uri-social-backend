"""
POST /listening/cards/{card_id}/create — PRD §9.3's "Create" deep-link:
pass a card's evidence-backed brief straight into the content generator
instead of the owner re-typing it. For post/carousel/faq cards this runs
the SAME pipeline the normal Create Content screen uses — credit gate,
real carousel slide generation, credit deduction, background AI image
generation — not a stripped-down text-only shortcut (confirmed live: the
old shortcut produced a bare-text, non-carousel draft with no image at all
for a card labelled "Format: carousel"). Uses an in-memory MongoDB; the
AI-backed generation services, the Brand Playbook lookup, and background
image generation are all stubbed — this file tests routing/credits/card
bookkeeping, not real AI output.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from fastapi import HTTPException

from app.agents.market_intelligence.listening.router import create_from_card, CreateFromCardOverrides
from app.services.CreditService import credit_service
from app.services.TrialService import trial_service

BRAND_ID, USER_ID = "brand-1", "user-1"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["create_from_card_test"]


@pytest.fixture(autouse=True)
def _inject_credit_services():
    """credit_service/trial_service are process-wide singletons that resolve
    a real DB connection unless a test substitutes one — same pattern as
    tests/test_credit_service_comp_exhaustion.py. Reset after every test so
    nothing leaks into an unrelated one (these are module-level globals)."""
    yield
    credit_service._db = None
    trial_service._db = None


async def _card(db, card_id="c1", platforms=("instagram", "tiktok"), content_id=None, action_type="post"):
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
    "app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
    new=AsyncMock(return_value={"status": True, "responseData": {
        "brand_name": "WD", "brand_colors": ["#000"], "industry": "beauty",
    }}),
)
_PATCH_TO_BRAND_CONTEXT = patch(
    "app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.to_brand_context",
    return_value={"brand_name": "WD"},
)
# Real AI image generation is a fire-and-forget asyncio.create_task in
# production — stubbed in every test so nothing here hits a real image
# model or Cloudinary, and so dangling background tasks don't outlive the
# test's own event loop.
_PATCH_IMAGE_BG = patch(
    "app.agents.social_media_manager.routers.complete_social_manager._generate_image_bg",
    new=AsyncMock(),
)


def _ok_generation(draft_ids=("d1", "d2")):
    return {
        "status": True,
        "responseData": {
            "request_id": "req-1",
            "drafts": [{"id": d, "platform": "instagram", "content": "caption"} for d in draft_ids],
        },
    }


def _ok_carousel(draft_ids=("d1",)):
    return {
        "status": True,
        "responseData": {
            "request_id": "req-carousel-1",
            "drafts": [
                {"id": d, "platform": "instagram", "content": "caption",
                 "slides": [{"headline": "Slide 1", "body": "body 1"}, {"headline": "Slide 2", "body": "body 2"}]}
                for d in draft_ids
            ],
        },
    }


async def _grant_wallet(db, credits_remaining):
    await db["user_credits"].insert_one({
        "user_id": USER_ID, "subscription_tier": "starter", "subscription_source": None,
        "subscription_credits": credits_remaining, "bonus_credits": 0, "credits_used": 0,
        "total_credits": credits_remaining, "credits_remaining": credits_remaining,
        "start_date": datetime.utcnow(), "end_date": datetime.utcnow() + timedelta(days=30),
        "next_renewal": datetime.utcnow() + timedelta(days=30),
    })


class TestCreateFromCard:
    @pytest.mark.asyncio
    async def test_happy_path_generates_drafts_and_updates_the_card(self):
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
        await _card(db)
        # The real ContentGenerationService also inserts content_drafts docs
        # as a side effect — seeded here so create_from_card's own
        # update_many(has_image=True) has something real to find, matching
        # what happens with the real (unmocked) service in production.
        await db["content_drafts"].insert_many([{"id": "d1", "platform": "instagram"},
                                                 {"id": "d2", "platform": "instagram"}])
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, _PATCH_IMAGE_BG, patch(
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

        # Real Create Content pipeline, not the old shortcut: drafts get
        # has_image flagged so the frontend shimmer shows, same as
        # generate_content's own behaviour.
        draft = await db["content_drafts"].find_one({"id": "d1"})
        assert draft["has_image"] is True

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
    async def test_post_and_faq_actions_use_the_plain_text_generator(self):
        for action_type in ("post", "faq"):
            db = fresh_db()
            credit_service._db = db
            trial_service._db = db
            await _card(db, action_type=action_type)
            with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, _PATCH_IMAGE_BG, patch(
                "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
                ".generate_multi_platform_content",
                new=AsyncMock(return_value=_ok_generation()),
            ):
                result = await create_from_card("c1", ctx=CTX, db=db)
            assert result["responseData"]["draft_ids"] == ["d1", "d2"]

    @pytest.mark.asyncio
    async def test_carousel_action_routes_to_the_real_carousel_service(self):
        # The exact live bug this fixes: a "Format: carousel" card used to
        # call the plain text generator (no slides, no images) instead of
        # CarouselGenerationService — confirmed by reading the old code, now
        # asserted directly so it can't silently regress back to that.
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
        await _card(db, action_type="carousel")
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, _PATCH_IMAGE_BG, patch(
            "app.agents.social_media_manager.services.carousel_generation_service.CarouselGenerationService"
            ".generate_multi_platform",
            new=AsyncMock(return_value=_ok_carousel()),
        ) as mock_carousel, patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(),
        ) as mock_plain:
            result = await create_from_card("c1", ctx=CTX, db=db)

        mock_carousel.assert_called_once()
        mock_plain.assert_not_called()
        assert mock_carousel.call_args.kwargs["force_num_slides"] is True
        # The card's own outline has 2 beats -> 2 slides requested (clamped to >=2).
        assert mock_carousel.call_args.kwargs["num_slides"] == 2
        assert result["responseData"]["draft_ids"] == ["d1"]

    @pytest.mark.asyncio
    async def test_insufficient_credits_blocks_generation_with_a_clear_402(self):
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
        await _grant_wallet(db, credits_remaining=0)
        await _card(db)
        with patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(),
        ) as mock_gen:
            with pytest.raises(HTTPException) as exc:
                await create_from_card("c1", ctx=CTX, db=db)
        assert exc.value.status_code == 402
        mock_gen.assert_not_called()
        card = await db["mi_listen_cards"].find_one({"id": "c1"})
        assert card["status"] == "shown" and card["content_id"] is None

    @pytest.mark.asyncio
    async def test_successful_generation_deducts_a_real_credit(self):
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
        await _grant_wallet(db, credits_remaining=5)
        await _card(db)
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, _PATCH_IMAGE_BG, patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(return_value=_ok_generation()),
        ):
            await create_from_card("c1", ctx=CTX, db=db)
        wallet = await db["user_credits"].find_one({"user_id": USER_ID})
        assert wallet["credits_remaining"] == 4

    @pytest.mark.asyncio
    async def test_generation_failure_is_502_and_card_is_left_untouched(self):
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
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


def _patch_connections(connected):
    return patch(
        "app.agents.social_media_manager.services.social_account_service.SocialAccountService"
        ".get_user_connections",
        new=AsyncMock(return_value={"status": True, "responseData": {"connected_platforms": list(connected)}}),
    )


class TestPlatformAndFormatOverrides:
    """Live-reported bug: every card suggested Instagram only, with no way
    to pick a different platform or swap carousel<->post before a credit
    was spent — even when other platforms were genuinely connected. These
    cover the fix: an explicit override always wins over the AI's first
    guess, and is validated against what's actually connected."""

    @pytest.mark.asyncio
    async def test_platform_override_replaces_the_cards_own_platforms(self):
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
        await _card(db, platforms=("instagram",))
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, _PATCH_IMAGE_BG, _patch_connections(
            ["instagram", "facebook", "tiktok"]
        ), patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(return_value=_ok_generation(draft_ids=("d1",))),
        ) as mock_gen:
            await create_from_card(
                "c1", overrides=CreateFromCardOverrides(platforms=["facebook", "tiktok"]), ctx=CTX, db=db
            )
        assert mock_gen.call_args.kwargs["platforms"] == ["facebook", "tiktok"]

    @pytest.mark.asyncio
    async def test_platform_override_rejects_a_platform_that_isnt_connected(self):
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
        await _card(db, platforms=("instagram",))
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, _patch_connections(["instagram"]):
            with pytest.raises(HTTPException) as exc:
                await create_from_card(
                    "c1", overrides=CreateFromCardOverrides(platforms=["tiktok"]), ctx=CTX, db=db
                )
        assert exc.value.status_code == 400
        assert "tiktok" in exc.value.detail.lower()
        card = await db["mi_listen_cards"].find_one({"id": "c1"})
        assert card["status"] == "shown" and card["content_id"] is None

    @pytest.mark.asyncio
    async def test_platform_override_rejects_an_ad_account_as_a_postable_platform(self):
        """An ad account (facebook_ads) is a real connection but not
        something content can be posted to — rejected before even checking
        connections, so this never fails confusingly two steps later."""
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
        await _card(db, platforms=("instagram",))
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, _patch_connections(["instagram", "facebook_ads"]):
            with pytest.raises(HTTPException) as exc:
                await create_from_card(
                    "c1", overrides=CreateFromCardOverrides(platforms=["facebook_ads"]), ctx=CTX, db=db
                )
        assert exc.value.status_code == 400
        assert "facebook_ads" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_content_type_override_swaps_a_post_card_to_carousel(self):
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
        await _card(db, action_type="post")
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, _PATCH_IMAGE_BG, patch(
            "app.agents.social_media_manager.services.carousel_generation_service.CarouselGenerationService"
            ".generate_multi_platform",
            new=AsyncMock(return_value=_ok_carousel()),
        ) as mock_carousel, patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(),
        ) as mock_plain:
            await create_from_card(
                "c1", overrides=CreateFromCardOverrides(content_type="carousel"), ctx=CTX, db=db
            )
        mock_carousel.assert_called_once()
        mock_plain.assert_not_called()

    @pytest.mark.asyncio
    async def test_content_type_override_swaps_a_carousel_card_to_a_plain_post(self):
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
        await _card(db, action_type="carousel")
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, _PATCH_IMAGE_BG, patch(
            "app.agents.social_media_manager.services.carousel_generation_service.CarouselGenerationService"
            ".generate_multi_platform",
            new=AsyncMock(),
        ) as mock_carousel, patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(return_value=_ok_generation(draft_ids=("d1",))),
        ) as mock_plain:
            await create_from_card(
                "c1", overrides=CreateFromCardOverrides(content_type="post"), ctx=CTX, db=db
            )
        mock_plain.assert_called_once()
        mock_carousel.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_overrides_behaves_exactly_as_before(self):
        """Regression guard: omitting overrides entirely (the pre-existing
        call shape) must still use the card's own platforms/type untouched."""
        db = fresh_db()
        credit_service._db = db
        trial_service._db = db
        await _card(db, platforms=("instagram", "tiktok"), action_type="post")
        with _PATCH_PLAYBOOK, _PATCH_TO_BRAND_CONTEXT, _PATCH_IMAGE_BG, patch(
            "app.agents.social_media_manager.services.content_generation_service.ContentGenerationService"
            ".generate_multi_platform_content",
            new=AsyncMock(return_value=_ok_generation()),
        ) as mock_gen:
            await create_from_card("c1", ctx=CTX, db=db)
        assert mock_gen.call_args.kwargs["platforms"] == ["instagram", "tiktok"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
