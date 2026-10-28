"""
_call_dalle_api's GPT-Image-2 branch (ImageContentService, the live
AI-image-generation pipeline — image_model is hardcoded to
"openai/gpt-image-2" everywhere it's called) could only generate 3 fixed
size buckets (1536x1024 / 1024x1536 / 1024x1024), none of which match most
platforms' real target dimensions. Every OTHER generation branch in this
function (gpt-image-1 edit, fal.ai, Gemini, gpt-image-1 fallback) already
calls _crop_to_ratio() before resizing to the exact target size — only this
branch (the one that actually runs) did a plain two-axis resize straight to
the target box, stretching width and height by different factors whenever
the generated bucket's ratio didn't match the target's.

Confirmed live: LinkedIn's 1200x628 (1.91:1) is the worst mismatch of any
platform against the landscape bucket (1536x1024, 1.5:1) — up to a 27%
width/height scale differential — producing a visibly warped subject/logo
in a real LinkedIn post.
"""
import asyncio
import base64
import io
from unittest.mock import patch

import pytest

pytest.importorskip("PIL")
from PIL import Image

from app.agents.social_media_manager.services.image_content_service import ImageContentService


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_openai_image_response(w, h):
    """A real PIL image at a GPT-Image-2 bucket size, base64-encoded, in the
    exact {"data": [{"b64_json": ...}]}-shaped object _call_dalle_api reads."""
    img = Image.new("RGB", (w, h), color=(200, 50, 100))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    class _Data:
        b64_json = b64

    class _Resp:
        data = [_Data()]

    return _Resp()


def _decode_size(data_url: str):
    header, b64 = data_url.split(",", 1)
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    return img.size


class TestGptImage2AspectRatio:
    def test_linkedin_target_is_not_stretched(self):
        """LinkedIn's 1200x628 (1.91:1) vs the landscape bucket (1536x1024,
        1.5:1) is the worst mismatch of any platform — the final image must
        come back at the exact target size with no stretch, meaning
        _crop_to_ratio must have run first."""
        fake_response = _fake_openai_image_response(1536, 1024)

        with (
            patch("app.services.AIService.client") as mock_client,
            patch.object(
                ImageContentService, "_crop_to_ratio",
                wraps=ImageContentService._crop_to_ratio,
            ) as spy_crop,
        ):
            mock_client.images.generate.return_value = fake_response
            result = run(ImageContentService._call_dalle_api(
                prompt="a photo of a person at a desk",
                size="1200x628",
                image_model="openai/gpt-image-2",
            ))

        assert result["success"] is True
        spy_crop.assert_called_once()
        w, h = _decode_size(result["url"])
        assert (w, h) == (1200, 628)

    def test_square_target_matching_the_bucket_skips_crop_but_still_exact(self):
        """A 1024x1024 target matches the square bucket exactly — no crop
        needed, but the final size must still be correct."""
        fake_response = _fake_openai_image_response(1024, 1024)

        with patch("app.services.AIService.client") as mock_client:
            mock_client.images.generate.return_value = fake_response
            result = run(ImageContentService._call_dalle_api(
                prompt="a product shot",
                size="1024x1024",
                image_model="openai/gpt-image-2",
            ))

        assert result["success"] is True
        w, h = _decode_size(result["url"])
        assert (w, h) == (1024, 1024)

    def test_instagram_portrait_target_is_not_stretched(self):
        """Instagram/Facebook's 1080x1350 vs the portrait bucket (1024x1536)
        is also a real mismatch (20% differential) — same fix must apply."""
        fake_response = _fake_openai_image_response(1024, 1536)

        with (
            patch("app.services.AIService.client") as mock_client,
            patch.object(
                ImageContentService, "_crop_to_ratio",
                wraps=ImageContentService._crop_to_ratio,
            ) as spy_crop,
        ):
            mock_client.images.generate.return_value = fake_response
            result = run(ImageContentService._call_dalle_api(
                prompt="a lifestyle photo",
                size="1080x1350",
                image_model="openai/gpt-image-2",
            ))

        assert result["success"] is True
        spy_crop.assert_called_once()
        w, h = _decode_size(result["url"])
        assert (w, h) == (1080, 1350)
