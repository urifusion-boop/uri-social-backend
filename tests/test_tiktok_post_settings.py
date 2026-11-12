"""
TikTok Content Sharing Guidelines — the user's Post to TikTok choices must reach
video/init exactly as made, and be re-validated server-side against creator_info.

Covers: privacy level taken from the user (never defaulted), interactions off
unless turned on, commercial-content toggles only sent when used, and rejection
of choices creator_info doesn't allow (private branded content, disabled
interactions, unknown privacy level).
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.social_media_manager.services import tiktok_direct_service as svc
from app.agents.social_media_manager.services.tiktok_direct_service import (
    TikTokDirectAPIError,
    TikTokPostSettings,
    _validate_settings,
)

CREATOR_INFO = {
    "creator_nickname": "Test Creator",
    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
    "comment_disabled": False,
    "duet_disabled": True,
    "stitch_disabled": False,
    "max_video_post_duration_sec": 600,
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _settings(**overrides):
    base = dict(title="hello", privacy_level="PUBLIC_TO_EVERYONE")
    base.update(overrides)
    return TikTokPostSettings(**base)


def test_valid_settings_pass():
    _validate_settings(_settings(allow_comment=True, allow_stitch=True), CREATOR_INFO)


def test_privacy_level_not_offered_is_rejected():
    with pytest.raises(TikTokDirectAPIError):
        _validate_settings(_settings(privacy_level="FOLLOWER_OF_CREATOR"), CREATOR_INFO)


def test_no_privacy_options_means_cannot_post():
    with pytest.raises(TikTokDirectAPIError, match="try again later"):
        _validate_settings(_settings(), {**CREATOR_INFO, "privacy_level_options": []})


def test_branded_content_cannot_be_private():
    with pytest.raises(TikTokDirectAPIError, match="cannot be set to private"):
        _validate_settings(_settings(privacy_level="SELF_ONLY", brand_content=True), CREATOR_INFO)
    with pytest.raises(TikTokDirectAPIError, match="cannot be set to private"):
        _validate_settings(
            _settings(privacy_level="SELF_ONLY", brand_content=True, brand_organic=True), CREATOR_INFO
        )


def test_your_brand_alone_may_be_private():
    _validate_settings(_settings(privacy_level="SELF_ONLY", brand_organic=True), CREATOR_INFO)


def test_interaction_disabled_by_creator_is_rejected():
    with pytest.raises(TikTokDirectAPIError, match="Duet"):
        _validate_settings(_settings(allow_duet=True), CREATOR_INFO)


def test_title_over_limit_is_rejected():
    with pytest.raises(TikTokDirectAPIError, match="too long"):
        _validate_settings(_settings(title="x" * 2201), CREATOR_INFO)


class _FakeResp:
    def __init__(self, status_code=200, payload=None, content=b"video-bytes", text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = text

    def json(self):
        return self._payload


def _publish_and_capture_init_body(settings_):
    """Runs publish_tiktok_direct against a faked TikTok and returns the JSON
    sent to video/init."""
    sent = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            return _FakeResp(content=b"video-bytes")

        async def post(self, url, **k):
            if url.endswith("/creator_info/query/"):
                return _FakeResp(payload={"error": {"code": "ok"}, "data": CREATOR_INFO})
            if url.endswith("/video/init/"):
                sent["body"] = k["json"]
                return _FakeResp(payload={
                    "error": {"code": "ok"},
                    "data": {"upload_url": "https://upload.example/x", "publish_id": "p1"},
                })
            if url.endswith("/status/fetch/"):
                return _FakeResp(payload={"error": {"code": "ok"}, "data": {"status": "PUBLISH_COMPLETE"}})
            raise AssertionError(f"unexpected POST {url}")

        async def put(self, url, **k):
            return _FakeResp(status_code=201)

    with patch.object(svc.httpx, "AsyncClient", FakeClient), patch.object(svc.asyncio, "sleep", AsyncMock()):
        publish_id, status = _run(svc.publish_tiktok_direct("tok", "https://v/x.mp4", "legacy caption", settings_))
    assert (publish_id, status) == ("p1", "PUBLISH_COMPLETE")
    return sent["body"]["post_info"]


def test_user_choices_reach_video_init_with_interactions_off_by_default():
    post_info = _publish_and_capture_init_body(_settings(title="my title", privacy_level="MUTUAL_FOLLOW_FRIENDS"))
    assert post_info["title"] == "my title"
    assert post_info["privacy_level"] == "MUTUAL_FOLLOW_FRIENDS"
    assert post_info["disable_comment"] is True
    assert post_info["disable_duet"] is True
    assert post_info["disable_stitch"] is True
    assert "brand_content_toggle" not in post_info
    assert "brand_organic_toggle" not in post_info


def test_enabled_interactions_and_branded_content_are_sent():
    post_info = _publish_and_capture_init_body(
        _settings(allow_comment=True, allow_stitch=True, brand_content=True)
    )
    assert post_info["disable_comment"] is False
    assert post_info["disable_stitch"] is False
    assert post_info["disable_duet"] is True
    assert post_info["brand_content_toggle"] is True
    assert post_info["brand_organic_toggle"] is False


def test_without_settings_legacy_behaviour_is_unchanged():
    post_info = _publish_and_capture_init_body(None)
    assert post_info["title"] == "legacy caption"
    assert post_info["privacy_level"] == "PUBLIC_TO_EVERYONE"
