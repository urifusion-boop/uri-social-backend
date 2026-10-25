"""
TikTok photo Direct Post — the user's choices reach content/init as made, photo-only
rules are enforced (no Duet/Stitch, 90-char title), and the signed image links TikTok
PULLs from can't be forged, replayed after expiry, or used to read other drafts.
"""
import asyncio
import base64
import io
import time
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from app.agents.social_media_manager.services import tiktok_direct_service as svc
from app.agents.social_media_manager.services import tiktok_media_proxy as proxy
from app.agents.social_media_manager.services.tiktok_direct_service import (
    TikTokDirectAPIError,
    TikTokPostSettings,
    _validate_settings,
)

CREATOR_INFO = {
    "creator_nickname": "Test Creator",
    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
    "comment_disabled": False,
    "duet_disabled": False,
    "stitch_disabled": False,
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _settings(**overrides):
    base = dict(title="t", description="d", privacy_level="SELF_ONLY")
    base.update(overrides)
    return TikTokPostSettings(**base)


# ── validation ────────────────────────────────────────────────────────────────

def test_photo_title_limit_is_90():
    _validate_settings(_settings(title="x" * 90), CREATOR_INFO, photo=True)
    with pytest.raises(TikTokDirectAPIError, match="Title is too long"):
        _validate_settings(_settings(title="x" * 91), CREATOR_INFO, photo=True)


def test_same_title_is_fine_for_video():
    _validate_settings(_settings(title="x" * 91), CREATOR_INFO, photo=False)


def test_photo_description_limit_is_4000():
    with pytest.raises(TikTokDirectAPIError, match="Description is too long"):
        _validate_settings(_settings(description="x" * 4001), CREATOR_INFO, photo=True)


@pytest.mark.parametrize("flag", ["allow_duet", "allow_stitch"])
def test_duet_and_stitch_dont_apply_to_photos(flag):
    with pytest.raises(TikTokDirectAPIError, match="don't apply to photo"):
        _validate_settings(_settings(**{flag: True}), CREATOR_INFO, photo=True)


def test_branded_photo_cannot_be_private():
    with pytest.raises(TikTokDirectAPIError, match="cannot be set to private"):
        _validate_settings(_settings(brand_content=True), CREATOR_INFO, photo=True)


# ── publish payload ───────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _publish_photo(settings_, auto_add_music=False, urls=("https://api/x/0.jpg", "https://api/x/1.jpg")):
    sent = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            if url.endswith("/creator_info/query/"):
                return _FakeResp({"error": {"code": "ok"}, "data": CREATOR_INFO})
            if url.endswith("/content/init/"):
                sent["body"] = k["json"]
                return _FakeResp({"error": {"code": "ok"}, "data": {"publish_id": "pp1"}})
            if url.endswith("/status/fetch/"):
                return _FakeResp({"error": {"code": "ok"}, "data": {"status": "PUBLISH_COMPLETE"}})
            raise AssertionError(f"unexpected POST {url}")

    with patch.object(svc.httpx, "AsyncClient", FakeClient), patch.object(svc.asyncio, "sleep", AsyncMock()):
        result = _run(svc.publish_tiktok_photo_direct("tok", list(urls), settings_, auto_add_music))
    assert result == ("pp1", "PUBLISH_COMPLETE")
    return sent["body"]


def test_photo_init_carries_user_choices_with_comments_off_by_default():
    body = _publish_photo(_settings(title="Hello", description="Caption", privacy_level="PUBLIC_TO_EVERYONE"))
    assert body["media_type"] == "PHOTO"
    assert body["post_mode"] == "DIRECT_POST"
    assert body["source_info"] == {
        "source": "PULL_FROM_URL",
        "photo_cover_index": 0,
        "photo_images": ["https://api/x/0.jpg", "https://api/x/1.jpg"],
    }
    info = body["post_info"]
    assert info["title"] == "Hello"
    assert info["description"] == "Caption"
    assert info["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert info["disable_comment"] is True
    assert info["auto_add_music"] is False
    assert "brand_content_toggle" not in info and "brand_organic_toggle" not in info


def test_photo_init_sends_comment_music_and_disclosure_when_chosen():
    body = _publish_photo(
        _settings(privacy_level="PUBLIC_TO_EVERYONE", allow_comment=True, brand_organic=True),
        auto_add_music=True,
    )
    info = body["post_info"]
    assert info["disable_comment"] is False
    assert info["auto_add_music"] is True
    assert info["brand_organic_toggle"] is True
    assert info["brand_content_toggle"] is False


def test_photo_publish_without_images_is_rejected():
    with pytest.raises(TikTokDirectAPIError, match="No images"):
        _run(svc.publish_tiktok_photo_direct("tok", [], _settings()))


# ── signed media links ────────────────────────────────────────────────────────

def test_valid_signature_verifies():
    exp = int(time.time()) + 60
    sig = proxy._sign("draft1", 0, exp)
    assert proxy.verify("draft1", 0, exp, sig)


def test_signature_is_bound_to_draft_index_and_expiry():
    exp = int(time.time()) + 60
    sig = proxy._sign("draft1", 0, exp)
    assert not proxy.verify("draft2", 0, exp, sig)      # another draft
    assert not proxy.verify("draft1", 1, exp, sig)      # another image
    assert not proxy.verify("draft1", 0, exp + 1, sig)  # tampered expiry
    assert not proxy.verify("draft1", 0, exp, "")       # missing


def test_expired_link_is_rejected():
    exp = int(time.time()) - 1
    assert not proxy.verify("draft1", 0, exp, proxy._sign("draft1", 0, exp))


def test_signed_urls_sit_under_the_verified_prefix():
    urls = proxy.signed_urls("draft1", 2)
    assert len(urls) == 2
    for i, u in enumerate(urls):
        assert f"{proxy.MEDIA_PATH}draft1/{i}.jpg?exp=" in u and "&sig=" in u


def test_image_sources_single_and_carousel():
    assert proxy.draft_image_sources({"image_url": "a"}) == ["a"]
    carousel = {"post_type": "carousel", "slides": [{"image_url": "a"}, {"image_url": ""}, {"image_url": "c"}]}
    assert proxy.draft_image_sources(carousel) == ["a", "c"]
    assert proxy.draft_image_sources({}) == []


def _png_data_url(size):
    buf = io.BytesIO()
    Image.new("RGBA", size, (200, 30, 120, 255)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_render_converts_png_to_jpeg_and_caps_at_1080():
    draft = {"image_url": _png_data_url((2400, 1600))}
    jpeg = _run(proxy.render_jpeg(draft, 0))
    img = Image.open(io.BytesIO(jpeg))
    assert img.format == "JPEG"
    assert max(img.size) == 1080


def test_render_returns_none_for_missing_index():
    assert _run(proxy.render_jpeg({"image_url": _png_data_url((10, 10))}, 3)) is None


# ── error reporting ───────────────────────────────────────────────────────────

def test_tiktok_error_code_and_log_id_are_kept_in_the_message():
    data = {"error": {"code": "invalid_param", "message": "Please review our integration guidelines", "log_id": "L123"}}
    with pytest.raises(TikTokDirectAPIError) as exc:
        svc._raise_for_error(data, "photo init")
    assert "photo init: Please review our integration guidelines" in str(exc.value)
    assert "invalid_param" in str(exc.value) and "L123" in str(exc.value)
    assert exc.value.code == "invalid_param"


def test_ok_envelope_does_not_raise():
    svc._raise_for_error({"error": {"code": "ok", "message": "", "log_id": "x"}}, "photo init")
