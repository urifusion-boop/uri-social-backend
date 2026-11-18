"""
A picture uploaded for TikTok is fitted whole into the 9:16 frame, never centre-cropped. The crop kept only ~70% of
a 4:5 post's width, so text and logos at the sides were sliced off ("The 7 questions…" lost its first letters).
"""
import asyncio
import io
import sys
import types

import pytest
from PIL import Image

from app.agents.social_media_manager.services.image_content_service import ImageContentService as ICS

RED, BLUE, GREEN = (255, 0, 0), (0, 0, 255), (0, 200, 0)


def _post(w=1353, h=1600):
    """A 4:5 post with a red strip down its left edge and a blue one down its right."""
    img = Image.new("RGB", (w, h), GREEN)
    for x in range(0, 12):
        for y in range(h):
            img.putpixel((x, y), RED)
            img.putpixel((w - 1 - x, y), BLUE)
    return img


def _close(a, b, tol=40):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_the_result_is_exactly_tiktoks_frame():
    assert ICS._fit_with_blurred_backdrop(_post(), 1080, 1920).size == (1080, 1920)


def test_a_4_5_post_keeps_both_side_edges():
    out = ICS._fit_with_blurred_backdrop(_post(), 1080, 1920)
    mid = out.height // 2
    assert _close(out.getpixel((2, mid)), RED), "left edge of the post was cut off"
    assert _close(out.getpixel((1077, mid)), BLUE), "right edge of the post was cut off"


def test_a_landscape_post_keeps_its_top_and_bottom_edges_too():
    img = Image.new("RGB", (1600, 900), GREEN)
    for y in range(10):
        for x in range(1600):
            img.putpixel((x, y), RED)
            img.putpixel((x, 899 - y), BLUE)
    out = ICS._fit_with_blurred_backdrop(img, 1080, 1920)
    fg_h = round(900 * 1080 / 1600)
    top = (1920 - fg_h) // 2
    assert _close(out.getpixel((540, top + 2)), RED) and _close(out.getpixel((540, top + fg_h - 3)), BLUE)


def test_the_bars_above_and_below_are_filled_not_black_or_transparent():
    out = ICS._fit_with_blurred_backdrop(_post(), 1080, 1920)
    top_bar = out.getpixel((540, 5))
    assert out.mode == "RGB" and top_bar != (0, 0, 0) and sum(top_bar) > 60


def test_a_picture_already_9_16_fills_the_frame():
    img = Image.new("RGB", (540, 960), GREEN)
    img.putpixel((0, 480), RED)
    out = ICS._fit_with_blurred_backdrop(img, 1080, 1920)
    assert out.size == (1080, 1920) and _close(out.getpixel((540, 960)), GREEN)


def test_a_wide_panorama_is_never_stretched():
    out = ICS._fit_with_blurred_backdrop(Image.new("RGB", (3000, 500), GREEN), 1080, 1920)
    assert out.size == (1080, 1920)


# ── the re-upload ────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


def _png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def stubbed(monkeypatch):
    import httpx

    uploaded = {}

    class Http:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if url == "https://cdn/broken":
                raise httpx.ConnectError("down")
            return _Resp(_png(_post()))

    async def fake_upload(data, folder, resource_type):
        uploaded.update(data=data, folder=folder)
        return "https://cdn/fitted.webp"

    monkeypatch.setattr(httpx, "AsyncClient", Http)
    monkeypatch.setitem(sys.modules, "app.utils.s3_upload", types.SimpleNamespace(upload_bytes=fake_upload))
    return uploaded


def test_the_reupload_is_a_9_16_webp_with_nothing_cut_off(stubbed):
    url = asyncio.get_event_loop().run_until_complete(ICS.crop_and_reupload_for_tiktok("https://cdn/post.png", "u1"))
    assert url == "https://cdn/fitted.webp" and stubbed["folder"].endswith("/u1")
    out = Image.open(io.BytesIO(stubbed["data"])).convert("RGB")
    assert out.size == (1080, 1920)
    assert _close(out.getpixel((2, 960)), RED, tol=60) and _close(out.getpixel((1077, 960)), BLUE, tol=60)


def test_a_failure_falls_back_to_the_original_upload(stubbed):
    url = asyncio.get_event_loop().run_until_complete(ICS.crop_and_reupload_for_tiktok("https://cdn/broken", "u1"))
    assert url == "https://cdn/broken" and not stubbed
