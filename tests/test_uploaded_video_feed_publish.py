"""
A user-uploaded video on a normal (feed) post is stored as video_url with no image_url.

Live on 2026-11-13: Instagram refused every attempt with "Instagram requires an image for feed
posts" (the feed branch only read image_url), and the Facebook copy of the same post went out
as caption-only text with the video silently dropped. Instagram now gets it as a Reel and
Facebook as a Page video.
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService
from app.agents.social_media_manager.services.instagram_direct_service import InstagramDirectService
from app.services.FacebookService import FacebookService

VIDEO = "https://uri-social-media-prod.s3.eu-west-1.amazonaws.com/uri-social/user-uploads/clip.mp4"
IMAGE = "https://uri-social-media-prod.s3.eu-west-1.amazonaws.com/uri-social/user-uploads/photo.jpg"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def ig_conn():
    return {"user_id": "u1", "platform": "instagram", "connected_via": "instagram_direct_oauth",
            "ig_user_id": "ig1", "page_access_token": "tok", "page_id": "p1"}


def fb_conn():
    return {"id": "fb_p1", "user_id": "u1", "platform": "facebook", "connected_via": "facebook_direct_oauth",
            "connection_status": "active", "page_id": "p1", "page_access_token": "tok"}


def video_draft(platform):
    return {"id": "d1", "user_id": "u1", "platform": platform, "post_type": "feed", "content": "Hello",
            "image_url": None, "video_url": VIDEO, "content_source": "user_uploaded"}


class _Cursor:
    async def to_list(self, length=None):
        return []


class _Conns:
    async def find_one(self, *a, **k):
        return None

    def find(self, *a, **k):
        return _Cursor()


class Db(dict):
    def __init__(self):
        super().__init__(social_connections=_Conns())


# ── Instagram ───────────────────────────────────────────────────────────────

def test_instagram_feed_post_with_an_uploaded_video_goes_out_as_a_reel():
    reel = AsyncMock(return_value={"success": True, "post_id": "ig_media_1"})
    feed = AsyncMock()
    with patch.object(InstagramDirectService, "publish_reel", new=reel), \
         patch.object(InstagramDirectService, "publish_post", new=feed):
        result = _run(ApprovalWorkflowService._publish_to_platform(
            "instagram", video_draft("instagram"), ig_conn(), None, db=Db()))
    assert result["success"] is True
    assert reel.await_args.kwargs["video_url"] == VIDEO
    assert reel.await_args.kwargs["caption"] == "Hello"
    assert feed.await_count == 0


def test_instagram_feed_post_with_an_image_still_posts_the_image():
    reel = AsyncMock()
    feed = AsyncMock(return_value={"success": True, "post_id": "ig_media_2"})
    draft = {**video_draft("instagram"), "image_url": IMAGE, "video_url": None}
    with patch.object(InstagramDirectService, "publish_reel", new=reel), \
         patch.object(InstagramDirectService, "publish_post", new=feed):
        _run(ApprovalWorkflowService._publish_to_platform("instagram", draft, ig_conn(), None, db=Db()))
    assert feed.await_args.kwargs["image_url"] == IMAGE
    assert reel.await_count == 0


# ── Facebook ────────────────────────────────────────────────────────────────

def _graph_response(status, body):
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"x"
    resp.json.return_value = body
    return resp


def _patch_httpx(resp):
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return client, patch("httpx.AsyncClient", return_value=ctx)


def test_facebook_feed_post_with_an_uploaded_video_posts_the_video_not_just_text():
    client, p = _patch_httpx(_graph_response(200, {"id": "vid_77"}))
    text_only = AsyncMock()
    with p, patch.object(FacebookService, "post_on_facebook", new=text_only):
        result = _run(ApprovalWorkflowService._publish_to_platform(
            "facebook", video_draft("facebook"), fb_conn(), None, db=Db()))
    assert result["success"] is True and result["post_id"] == "vid_77"
    url = client.post.await_args.args[0]
    data = client.post.await_args.kwargs["data"]
    assert url.endswith("/p1/videos")
    assert data["file_url"] == VIDEO and data["description"] == "Hello" and data["published"] == "true"
    assert text_only.await_count == 0


def test_a_scheduled_facebook_video_is_handed_to_facebook_unpublished_with_its_time():
    client, p = _patch_httpx(_graph_response(200, {"id": "vid_78"}))
    when = datetime(2026, 11, 20, 9, 0, 0)
    with p:
        _run(ApprovalWorkflowService._publish_to_platform(
            "facebook", video_draft("facebook"), fb_conn(), when, db=Db()))
    data = client.post.await_args.kwargs["data"]
    assert data["published"] == "false" and data["scheduled_publish_time"] == 1795165200


def test_a_refused_facebook_video_says_what_facebook_said():
    body = {"error": {"message": "Confirm your identity before you can publish as this Page.",
                      "code": 368, "error_subcode": 4854002}}
    _, p = _patch_httpx(_graph_response(400, body))
    with p:
        result = _run(ApprovalWorkflowService._publish_to_platform(
            "facebook", video_draft("facebook"), fb_conn(), None, db=Db()))
    assert result["success"] is False
    assert "Confirm your identity" in result["error"]
