"""
Unit tests for LinkedInDirectService video upload and posting.

Covers the bug reported in production: a video published to a customer's
LinkedIn account showed only the caption, never the video. Root cause was
twofold — create_post had no video_url parameter/upload path at all, and
the approval dispatcher never looked at draft["video_url"] for LinkedIn.
These tests cover the service-level fix; the dispatcher fix is covered by
the branch added in approval_workflow_service.py's LinkedIn block.
"""

import pytest
from unittest.mock import MagicMock, patch

from app.agents.social_media_manager.services.linkedin_direct_service import (
    LinkedInDirectService,
    REGISTER_UPLOAD_URL,
    ASSETS_URL,
)


TOKEN = "test-access-token"
PERSON_URN = "urn:li:person:abc123"
VIDEO_URL = "https://example.com/video.mp4"
ASSET_URN = "urn:li:digitalmediaAsset:D4E22AQvideo"
UPLOAD_URL = "https://www.linkedin.com/dms-uploads/upload-token"


def _make_response(status_code: int, json_data: dict = None, headers: dict = None, content: bytes = b"fake-video-bytes"):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data or {}
    mock.headers = headers or {"content-type": "video/mp4"}
    mock.content = content
    mock.raise_for_status = MagicMock()
    return mock


@pytest.fixture
def svc():
    with patch("app.agents.social_media_manager.services.linkedin_direct_service.settings") as s:
        s.LINKEDIN_CLIENT_ID = "test-id"
        s.LINKEDIN_CLIENT_SECRET = "test-secret"
        yield LinkedInDirectService()


class TestRegisterVideo:
    @pytest.mark.asyncio
    async def test_register_video_uses_video_recipe(self, svc):
        """_register_video must request the feedshare-video recipe, not the image one."""
        reg_response = _make_response(200, {"value": {
            "asset": ASSET_URN,
            "uploadMechanism": {
                "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {
                    "uploadUrl": UPLOAD_URL
                }
            }
        }})
        posted = {}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, **kwargs):
                posted["url"] = url
                posted["json"] = kwargs.get("json")
                return reg_response

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await svc._register_video(TOKEN, PERSON_URN)

        assert posted["url"] == REGISTER_UPLOAD_URL
        recipes = posted["json"]["registerUploadRequest"]["recipes"]
        assert recipes == ["urn:li:digitalmediaRecipe:feedshare-video"]
        assert result["asset"] == ASSET_URN
        assert result["upload_url"] == UPLOAD_URL


class TestWaitForVideoReady:
    @pytest.mark.asyncio
    async def test_returns_once_asset_is_available(self, svc):
        """Polling stops as soon as the asset's recipe status is AVAILABLE."""
        call_count = {"n": 0}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kwargs):
                call_count["n"] += 1
                assert url == f"{ASSETS_URL}/{ASSET_URN.rsplit(':', 1)[-1]}"
                return _make_response(200, {"recipes": [{"status": "AVAILABLE"}]})

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            await svc._wait_for_video_ready(TOKEN, ASSET_URN, timeout_seconds=10)

        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_raises_on_processing_error(self, svc):
        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kwargs):
                return _make_response(200, {"recipes": [{"status": "ERROR"}]})

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            with pytest.raises(RuntimeError):
                await svc._wait_for_video_ready(TOKEN, ASSET_URN, timeout_seconds=10)


class TestCreatePostWithVideo:
    @pytest.mark.asyncio
    async def test_video_asset_wired_into_media_as_video_category(self, svc):
        """When video_url is given, the post must use shareMediaCategory=VIDEO with the asset."""
        post_response = _make_response(201, {}, {"x-restli-id": "urn:li:share:video1"})
        captured_post = {}

        async def fake_register_video(access_token, author_urn):
            return {"upload_url": UPLOAD_URL, "asset": ASSET_URN}

        async def fake_wait_ready(access_token, asset, timeout_seconds=120):
            return None

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kwargs):
                return _make_response(200)
            async def put(self, url, **kwargs):
                return _make_response(201)
            async def post(self, url, **kwargs):
                captured_post["json"] = kwargs.get("json", {})
                return post_response

        svc._register_video = fake_register_video
        svc._wait_for_video_ready = fake_wait_ready

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await svc.create_post(TOKEN, PERSON_URN, "Check out this video", video_url=VIDEO_URL)

        share = captured_post["json"]["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareMediaCategory"] == "VIDEO"
        assert share["media"][0]["media"] == ASSET_URN
        assert result["post_id"] == "urn:li:share:video1"

    @pytest.mark.asyncio
    async def test_video_url_takes_priority_over_image_url(self, svc):
        """If both are somehow present, video wins — never silently drop the video for an image."""
        post_response = _make_response(201, {}, {"x-restli-id": "id"})
        captured_post = {}

        async def fake_register_video(access_token, author_urn):
            return {"upload_url": UPLOAD_URL, "asset": ASSET_URN}

        async def fake_wait_ready(access_token, asset, timeout_seconds=120):
            return None

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kwargs):
                return _make_response(200)
            async def put(self, url, **kwargs):
                return _make_response(201)
            async def post(self, url, **kwargs):
                captured_post["json"] = kwargs.get("json", {})
                return post_response

        svc._register_video = fake_register_video
        svc._wait_for_video_ready = fake_wait_ready

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            await svc.create_post(
                TOKEN, PERSON_URN, "Both given",
                image_url="https://example.com/should-be-ignored.jpg",
                video_url=VIDEO_URL,
            )

        share = captured_post["json"]["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareMediaCategory"] == "VIDEO"

    @pytest.mark.asyncio
    async def test_video_upload_failure_raises_instead_of_silently_posting_text(self, svc):
        """
        Unlike the image path, a failed video upload must NOT silently fall back to
        text-only — that silent fallback is exactly the production bug being fixed.
        """
        async def failing_register_video(*args, **kwargs):
            raise Exception("video upload failed")

        svc._register_video = failing_register_video

        with pytest.raises(Exception):
            await svc.create_post(TOKEN, PERSON_URN, "Should fail loudly", video_url=VIDEO_URL)
