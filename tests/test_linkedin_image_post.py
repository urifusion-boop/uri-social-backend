"""
Unit tests for LinkedInDirectService image upload and posting (legacy v2 API).

Tests that:
- _register_image calls v2/assets?action=registerUpload
- PUT upload does NOT send Authorization header (pre-signed CDN URL)
- create_post builds correct ugcPosts payload with image asset
- Falls back to text-only when image upload fails
"""

import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch

from app.agents.social_media_manager.services.linkedin_direct_service import (
    LinkedInDirectService,
    REGISTER_UPLOAD_URL,
    UGC_POSTS_URL,
)


TOKEN = "test-access-token"
PERSON_URN = "urn:li:person:abc123"
IMAGE_URL = "https://example.com/image.jpg"
ASSET_URN = "urn:li:digitalmediaAsset:D4E22AQ"
UPLOAD_URL = "https://www.linkedin.com/dms-uploads/upload-token"


def _make_response(status_code: int, json_data: dict = None, headers: dict = None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data or {}
    mock.headers = headers or {"content-type": "image/jpeg"}
    mock.content = b"fake-image-bytes"
    mock.raise_for_status = MagicMock()
    return mock


@pytest.fixture
def svc():
    with patch("app.agents.social_media_manager.services.linkedin_direct_service.settings") as s:
        s.LINKEDIN_CLIENT_ID = "test-id"
        s.LINKEDIN_CLIENT_SECRET = "test-secret"
        yield LinkedInDirectService()


class TestRegisterImage:
    @pytest.mark.asyncio
    async def test_calls_register_upload_endpoint(self, svc):
        """_register_image must POST to v2/assets?action=registerUpload."""
        reg_response = _make_response(200, {"value": {
            "asset": ASSET_URN,
            "uploadMechanism": {
                "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {
                    "uploadUrl": UPLOAD_URL
                }
            }
        }})
        posted_to = {}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, **kwargs):
                posted_to["url"] = url
                posted_to["json"] = kwargs.get("json")
                return reg_response

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await svc._register_image(TOKEN, PERSON_URN)

        assert posted_to["url"] == REGISTER_UPLOAD_URL
        assert result["asset"] == ASSET_URN
        assert result["upload_url"] == UPLOAD_URL


class TestCreatePost:
    @pytest.mark.asyncio
    async def test_posts_to_ugc_posts_endpoint(self, svc):
        """create_post must use v2/ugcPosts."""
        post_response = _make_response(201, {}, {"x-restli-id": "urn:li:share:999"})
        posted_to = {}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, **kwargs):
                posted_to["url"] = url
                return post_response

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await svc.create_post(TOKEN, PERSON_URN, "Hello LinkedIn!")

        assert posted_to["url"] == UGC_POSTS_URL
        assert result["post_id"] == "urn:li:share:999"

    @pytest.mark.asyncio
    async def test_payload_uses_ugc_schema(self, svc):
        """Payload must use specificContent / shareCommentary shape."""
        post_response = _make_response(201, {}, {"x-restli-id": "id"})
        captured = {}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, **kwargs):
                captured["json"] = kwargs.get("json", {})
                return post_response

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            await svc.create_post(TOKEN, PERSON_URN, "Test post")

        p = captured["json"]
        share = p["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareCommentary"]["text"] == "Test post"
        assert share["shareMediaCategory"] == "NONE"

    @pytest.mark.asyncio
    async def test_image_asset_wired_into_media(self, svc):
        """When image_url is given, asset URN must appear in media array."""
        post_response = _make_response(201, {}, {"x-restli-id": "urn:li:share:2"})
        captured_post = {}
        put_headers_captured = {}

        async def fake_register(access_token, author_urn):
            return {"upload_url": UPLOAD_URL, "asset": ASSET_URN}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kwargs):
                return _make_response(200)
            async def put(self, url, **kwargs):
                put_headers_captured.update(kwargs.get("headers", {}))
                return _make_response(201)
            async def post(self, url, **kwargs):
                captured_post["json"] = kwargs.get("json", {})
                return post_response

        svc._register_image = fake_register

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            await svc.create_post(TOKEN, PERSON_URN, "With image", image_url=IMAGE_URL)

        share = captured_post["json"]["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareMediaCategory"] == "IMAGE"
        assert share["media"][0]["media"] == ASSET_URN

    @pytest.mark.asyncio
    async def test_put_upload_has_no_auth_header(self, svc):
        """PUT to CDN upload URL must NOT include Authorization header (pre-signed URL)."""
        post_response = _make_response(201, {}, {"x-restli-id": "id"})
        put_headers_captured = {}

        async def fake_register(access_token, author_urn):
            return {"upload_url": UPLOAD_URL, "asset": ASSET_URN}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kwargs):
                return _make_response(200)
            async def put(self, url, **kwargs):
                put_headers_captured.update(kwargs.get("headers", {}))
                return _make_response(201)
            async def post(self, url, **kwargs):
                return post_response

        svc._register_image = fake_register

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            await svc.create_post(TOKEN, PERSON_URN, "With image", image_url=IMAGE_URL)

        assert "Authorization" not in put_headers_captured, \
            "PUT to CDN upload URL must not send Authorization header"

    @pytest.mark.asyncio
    async def test_falls_back_to_text_when_image_upload_fails(self, svc):
        """If image upload raises, post should still go through as text-only."""
        post_response = _make_response(201, {}, {"x-restli-id": "urn:li:share:3"})
        captured_post = {}

        async def failing_register(*args, **kwargs):
            raise Exception("Upload failed")

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, **kwargs):
                captured_post["json"] = kwargs.get("json", {})
                return post_response

        svc._register_image = failing_register

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await svc.create_post(TOKEN, PERSON_URN, "Fallback", image_url=IMAGE_URL)

        assert result["post_id"] == "urn:li:share:3"
        share = captured_post["json"]["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareMediaCategory"] == "NONE"
        assert "media" not in share
