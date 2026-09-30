"""POST /drafts/{draft_id}/logo/reposition — thin router wrapper. Confirms
required-field validation happens before the service is even called, and
that a valid body delegates to LogoRepositionService.reposition with the
right arguments (including that a missing slide_index correctly becomes
None, not KeyError)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.social_media_manager.routers.complete_social_manager import (
    reposition_draft_logo,
)


def _fake_request(body: dict):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


class TestRepositionDraftLogoRouter:
    @pytest.mark.asyncio
    async def test_missing_field_is_rejected_before_calling_the_service(self):
        from fastapi import HTTPException

        req = _fake_request({"x": 1, "y": 2, "width": 10})  # height missing
        with patch(
            "app.agents.social_media_manager.services.logo_reposition_service.LogoRepositionService.reposition",
            new=AsyncMock(),
        ) as mock_reposition:
            with pytest.raises(HTTPException) as exc_info:
                await reposition_draft_logo(
                    draft_id="d1", request=req, db=MagicMock(), token={"user_id": "u1"}
                )
        assert exc_info.value.status_code == 400
        assert "height" in exc_info.value.detail
        mock_reposition.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_body_delegates_with_correct_args(self):
        req = _fake_request({"x": 10, "y": 20, "width": 30, "height": 40})
        fake_db = MagicMock()
        with patch(
            "app.agents.social_media_manager.services.logo_reposition_service.LogoRepositionService.reposition",
            new=AsyncMock(return_value={"status": True, "responseData": {"image_url": "https://x/y.png"}}),
        ) as mock_reposition:
            response = await reposition_draft_logo(
                draft_id="d1", request=req, db=fake_db, token={"user_id": "u1"}
            )

        assert response.status_code == 200
        mock_reposition.assert_awaited_once_with(
            draft_id="d1", user_id="u1", x=10, y=20, width=30, height=40,
            db=fake_db, slide_index=None,
        )

    @pytest.mark.asyncio
    async def test_slide_index_passed_through_when_present(self):
        req = _fake_request({"x": 1, "y": 2, "width": 3, "height": 4, "slide_index": 2})
        with patch(
            "app.agents.social_media_manager.services.logo_reposition_service.LogoRepositionService.reposition",
            new=AsyncMock(return_value={"status": True, "responseData": {}}),
        ) as mock_reposition:
            await reposition_draft_logo(
                draft_id="d1", request=req, db=MagicMock(), token={"user_id": "u1"}
            )
        assert mock_reposition.call_args.kwargs["slide_index"] == 2

    @pytest.mark.asyncio
    async def test_service_failure_returns_400(self):
        req = _fake_request({"x": 1, "y": 2, "width": 3, "height": 4})
        with patch(
            "app.agents.social_media_manager.services.logo_reposition_service.LogoRepositionService.reposition",
            new=AsyncMock(return_value={"status": False, "responseMessage": "No brand logo configured."}),
        ):
            response = await reposition_draft_logo(
                draft_id="d1", request=req, db=MagicMock(), token={"user_id": "u1"}
            )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_user_id_is_401(self):
        from fastapi import HTTPException

        req = _fake_request({"x": 1, "y": 2, "width": 3, "height": 4})
        with pytest.raises(HTTPException) as exc_info:
            await reposition_draft_logo(draft_id="d1", request=req, db=MagicMock(), token={})
        assert exc_info.value.status_code == 401
