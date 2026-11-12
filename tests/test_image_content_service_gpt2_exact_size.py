"""
gpt-image-2 posts were cut off at the side. The model drew a 2:3 portrait, and the post-generation
crop to TikTok's / Stories' 9:16 removed ~8% from each side, slicing text the model had set near the
edge. The branch now asks for the target's own shape, so there is nothing to crop, and falls back to the
fixed bucket (the old behaviour) if OpenAI refuses the size.
"""
import asyncio
import base64
import io
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PIL")
from PIL import Image

from app.agents.social_media_manager.services.image_content_service import ImageContentService

# Every platform target the app generates for (IMAGE_SPECS).
POST_TARGETS = [
    (1080, 1920),  # TikTok photo, Instagram story
    (1080, 1350),  # Instagram / Facebook portrait
    (1200, 628),   # LinkedIn
    (1200, 675),   # X / Twitter
    (1200, 630),   # Facebook link post
]


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _response(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 50, 100)).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return MagicMock(data=[MagicMock(b64_json=b64)])


class _SizeRefused(Exception):
    status_code = 400

    def __init__(self):
        super().__init__("Invalid value for 'size': this size is not supported.")


@pytest.mark.parametrize("w,h", POST_TARGETS)
def test_the_requested_size_has_the_targets_own_shape(w, h):
    size = ImageContentService._gpt2_exact_size(w, h)
    gw, gh = map(int, size.split("x"))
    assert gw % 16 == 0 and gh % 16 == 0
    assert abs((gw / gh) - (w / h)) / (w / h) < 0.02, "close enough that no crop runs"
    assert 655_360 <= gw * gh <= 8_294_400
    assert max(gw, gh) / min(gw, gh) <= 3


@pytest.mark.parametrize("w,h", [(1024, 1024), (1080, 1080), (1536, 1024), (1024, 1536), (1500, 500), (1584, 396)])
def test_square_bucket_shaped_and_banner_targets_keep_the_fixed_bucket(w, h):
    assert ImageContentService._gpt2_exact_size(w, h) is None


def test_a_9_16_post_is_generated_at_9_16_and_never_cropped():
    with patch("app.services.AIService.client") as client, patch.object(
        ImageContentService, "_crop_to_ratio", wraps=ImageContentService._crop_to_ratio
    ) as crop:
        size = ImageContentService._gpt2_exact_size(1080, 1920)
        client.images.generate.return_value = _response(*map(int, size.split("x")))
        result = run(ImageContentService._call_dalle_api(
            prompt="a weekend post", size="1080x1920", image_model="openai/gpt-image-2",
        ))

    assert client.images.generate.call_args.kwargs["size"] == size
    assert result["success"] is True
    # The ratio already matches, so the crop returned the picture untouched.
    gen_w, gen_h = map(int, size.split("x"))
    cropped = crop.call_args.args[0]
    assert Image.open(io.BytesIO(base64.b64decode(cropped))).size == (gen_w, gen_h)
    assert Image.open(io.BytesIO(base64.b64decode(result["url"].split(",", 1)[1]))).size == (1080, 1920)


def test_a_refused_size_falls_back_to_the_fixed_bucket():
    calls = []

    def generate(**kwargs):
        calls.append(kwargs["size"])
        if len(calls) == 1:
            raise _SizeRefused()
        return _response(1024, 1536)

    with patch("app.services.AIService.client") as client:
        client.images.generate.side_effect = generate
        result = run(ImageContentService._call_dalle_api(
            prompt="a weekend post", size="1080x1920", image_model="openai/gpt-image-2",
        ))

    assert calls == [ImageContentService._gpt2_exact_size(1080, 1920), "1024x1536"]
    assert result["success"] is True and result["model"] == "gpt-image-2"


def test_an_error_that_is_not_about_the_size_is_not_retried_at_the_bucket():
    err = Exception("moderation_blocked")
    err.status_code = 400
    fn = MagicMock(side_effect=err)
    with pytest.raises(Exception, match="moderation_blocked"):
        ImageContentService._gpt2_call(fn, "944x1680", "1024x1536", prompt="x")
    assert fn.call_count == 1


def test_reference_uploads_are_rewound_for_the_retry():
    upload = ("reference_0.png", io.BytesIO(b"png-bytes"), "image/png")
    seen = []

    def edit(**kwargs):
        seen.append(kwargs["image"][1].read())
        if len(seen) == 1:
            raise _SizeRefused()
        return "ok"

    assert ImageContentService._gpt2_call(edit, "944x1680", "1024x1536", image=upload) == "ok"
    assert seen == [b"png-bytes", b"png-bytes"]
