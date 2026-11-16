"""
An uploaded video keeps post_type "feed" (the upload form has no "reel" option) and carries `video_url` with no
`image_url`. Instagram's feed endpoint takes only images, so such a draft must publish as a Reel — with its music.
"""
import asyncio

import pytest

from app.agents.social_media_manager.services import instagram_direct_service as direct
from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService

CONNECTION = {"platform": "instagram", "ig_user_id": "178414", "page_access_token": "TOK", "page_id": "p1",
              "connected_via": "instagram_direct"}
AUDIO = {"audio_id": "587784541076604", "title": "Birthday Wish", "artist": "Shuba",
         "audio_volume": 80, "video_volume": 50}


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def reel(**kw):
        seen.append(("reel", kw))
        return {"success": True}

    async def post(**kw):
        seen.append(("post", kw))
        return {"success": True}

    async def story(**kw):
        seen.append(("story", kw))
        return {"success": True}

    monkeypatch.setattr(direct.InstagramDirectService, "publish_reel", staticmethod(reel))
    monkeypatch.setattr(direct.InstagramDirectService, "publish_post", staticmethod(post))
    monkeypatch.setattr(direct.InstagramDirectService, "publish_story", staticmethod(story))
    return seen


def _publish(draft):
    draft = {"content": "hi", **draft}
    return asyncio.get_event_loop().run_until_complete(
        ApprovalWorkflowService._publish_to_platform("instagram", draft, dict(CONNECTION), None))


def test_an_uploaded_video_publishes_as_a_reel_with_its_music(calls):
    _publish({"post_type": "feed", "video_url": "https://cdn/v.mp4", "image_url": None, "instagram_audio": AUDIO})
    kind, kw = calls[0]
    assert kind == "reel" and kw["video_url"] == "https://cdn/v.mp4" and kw["audio"] == AUDIO


def test_an_uploaded_video_without_music_is_still_a_reel(calls):
    _publish({"post_type": "feed", "video_url": "https://cdn/v.mp4"})
    assert calls[0][0] == "reel" and calls[0][1]["audio"] is None


def test_a_generated_reel_is_unchanged(calls):
    _publish({"post_type": "reel", "video_url": "https://cdn/v.mp4"})
    assert calls[0][0] == "reel"


def test_an_image_post_is_still_a_feed_post(calls):
    _publish({"post_type": "feed", "image_url": "https://cdn/i.jpg"})
    assert calls[0][0] == "post"


def test_a_story_is_still_a_story(calls):
    _publish({"post_type": "story", "image_url": "https://cdn/i.jpg"})
    assert calls[0][0] == "story"
