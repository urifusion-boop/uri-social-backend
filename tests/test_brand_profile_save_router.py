"""save_brand_profile (the /brand-profile POST router) used to build its
payload with request.dict(exclude_none=True) — which strips a field the
client explicitly sent as null identically to a field the client never sent
at all, collapsing exactly the "explicitly cleared" vs "not touched"
distinction BrandProfileService.save() relies on (`if field in data`).
Confirmed live: removing a brand's logo and saving never actually cleared it
server-side — the old logo_url just stayed in the database, so it
reappeared after every save. exclude_unset preserves that distinction."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.social_media_manager.routers.complete_social_manager import (
    BrandProfileRequest,
    save_brand_profile,
)


class TestSaveBrandProfileNullHandling:
    @pytest.mark.asyncio
    async def test_explicit_null_logo_url_reaches_the_service(self):
        # Passing logo_url=None explicitly (vs. never mentioning it) is what
        # marks the field as "set" for Pydantic's exclude_unset — the same
        # distinction FastAPI's own JSON parsing of {"logo_url": null, ...}
        # makes, so this exercises the real "client explicitly cleared it"
        # case, not "field omitted from the request".
        request = BrandProfileRequest(brand_name="Test Brand", logo_url=None)

        ctx = {"user_id": "u1", "brand_id": "b1"}
        fake_db = MagicMock()
        captured = {}

        async def fake_save(user_id, data, db, brand_id=None, **kwargs):
            captured["data"] = data
            return {"status": True, "responseData": data}

        with patch(
            "app.agents.social_media_manager.routers.complete_social_manager.BrandProfileService.save",
            side_effect=fake_save,
        ):
            await save_brand_profile(request=request, ctx=ctx, db=fake_db)

        assert "logo_url" in captured["data"], "explicit null was stripped before reaching the service"
        assert captured["data"]["logo_url"] is None

    @pytest.mark.asyncio
    async def test_omitted_logo_url_does_not_reach_the_service(self):
        """The other half of the same distinction: a field genuinely never
        sent by the client must still be excluded, so a partial save (e.g.
        just updating industry) can't accidentally wipe out an existing
        logo_url that the client's payload simply didn't mention."""
        request = BrandProfileRequest(brand_name="Test Brand")

        ctx = {"user_id": "u1", "brand_id": "b1"}
        fake_db = MagicMock()
        captured = {}

        async def fake_save(user_id, data, db, brand_id=None, **kwargs):
            captured["data"] = data
            return {"status": True, "responseData": data}

        with patch(
            "app.agents.social_media_manager.routers.complete_social_manager.BrandProfileService.save",
            side_effect=fake_save,
        ):
            await save_brand_profile(request=request, ctx=ctx, db=fake_db)

        assert "logo_url" not in captured["data"], "an untouched field was sent and would overwrite the existing value"

    @pytest.mark.asyncio
    async def test_explicitly_sent_value_reaches_the_service(self):
        request = BrandProfileRequest(brand_name="Test Brand", logo_url="https://cdn/new-logo.png")

        ctx = {"user_id": "u1", "brand_id": "b1"}
        fake_db = MagicMock()
        captured = {}

        async def fake_save(user_id, data, db, brand_id=None, **kwargs):
            captured["data"] = data
            return {"status": True, "responseData": data}

        with patch(
            "app.agents.social_media_manager.routers.complete_social_manager.BrandProfileService.save",
            side_effect=fake_save,
        ):
            await save_brand_profile(request=request, ctx=ctx, db=fake_db)

        assert captured["data"]["logo_url"] == "https://cdn/new-logo.png"
