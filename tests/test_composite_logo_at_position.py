"""composite_logo_at_position — the deterministic, no-AI paste primitive
behind manual logo reposition. Confirms it pastes at the exact given box
(not a computed/preset one) and resizes the logo to exactly width/height."""
import asyncio
import base64
import io
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from app.agents.social_media_manager.services.image_content_service import (
    ImageContentService,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _png_bytes(w, h, color):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _fake_get_responses(background_bytes, logo_bytes):
    """The service does two sequential client.get() calls (background, then
    logo) — return a fresh mock response for each in that order."""
    calls = iter([background_bytes, logo_bytes])

    async def fake_get(url, *a, **kw):
        resp = MagicMock()
        resp.content = next(calls)
        resp.raise_for_status = MagicMock()
        return resp

    return fake_get


class TestCompositeLogoAtPosition:
    def test_pastes_logo_at_exact_requested_box(self):
        bg_bytes = _png_bytes(400, 400, (30, 120, 80, 255))
        logo_bytes = _png_bytes(50, 50, (255, 0, 0, 255))

        with patch("httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__.return_value
            instance.get = AsyncMock(side_effect=_fake_get_responses(bg_bytes, logo_bytes))

            result_b64 = _run(ImageContentService.composite_logo_at_position(
                "https://x/bg.png", "https://x/logo.png", x=100, y=150, width=80, height=60
            ))

        result_img = Image.open(io.BytesIO(base64.b64decode(result_b64))).convert("RGB")
        assert result_img.size == (400, 400)

        # Center of the requested box must be the logo's red, not the
        # background's green.
        center_pixel = result_img.getpixel((100 + 40, 150 + 30))
        assert center_pixel[0] > 200 and center_pixel[1] < 50, "logo not found at the requested position"

        # Well outside the box, the background must be untouched (allowing
        # a few units of tolerance for WEBP's lossy re-encode, not the
        # actual paste logic).
        corner_pixel = result_img.getpixel((5, 5))
        expected = (30, 120, 80)
        assert all(abs(a - b) <= 3 for a, b in zip(corner_pixel, expected)), corner_pixel

    def test_resizes_logo_to_exact_requested_dimensions(self):
        bg_bytes = _png_bytes(400, 400, (0, 0, 0, 255))
        logo_bytes = _png_bytes(200, 100, (255, 255, 0, 255))  # 2:1 native aspect

        with patch("httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__.return_value
            instance.get = AsyncMock(side_effect=_fake_get_responses(bg_bytes, logo_bytes))

            result_b64 = _run(ImageContentService.composite_logo_at_position(
                "https://x/bg.png", "https://x/logo.png", x=10, y=10, width=200, height=200,  # forced 1:1
            ))

        result_img = Image.open(io.BytesIO(base64.b64decode(result_b64))).convert("RGB")
        # Bottom-right of the (forced square) logo box should still be
        # yellow — proves it was stretched to the requested box, not kept
        # at its native 2:1 aspect ratio (a caller providing width==height
        # here means they explicitly want that, e.g. from a drag-resize).
        assert result_img.getpixel((10 + 190, 10 + 190))[:2] == (255, 255)

    def test_raises_on_download_failure_rather_than_silently_returning_original(self):
        with patch("httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__.return_value

            async def fail_get(url, *a, **kw):
                raise Exception("background host down")

            instance.get = AsyncMock(side_effect=fail_get)

            try:
                _run(ImageContentService.composite_logo_at_position(
                    "https://x/bg.png", "https://x/logo.png", 0, 0, 50, 50
                ))
                assert False, "expected an exception"
            except Exception as e:
                assert "background host down" in str(e)
