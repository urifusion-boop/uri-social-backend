"""
Scheduling a TikTok image / carousel post.

Live-reported (staging, 2026-11-05): a TikTok photo scheduled from the Content Library never went out.
The scheduler claimed it at the due time and failed with "TikTok post has no video_url", because the only
TikTok path it had was the direct VIDEO upload, and the photo flow (which needs the Post to TikTok
screen's choices) only existed for Publish Now.

Scheduling now saves those choices on the draft (TikTok allows no default), and the scheduler posts the
photo with them at the due time.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.agents.social_media_manager.services import rate_limit as rl
from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService
from app.agents.social_media_manager.services.tiktok_direct_service import (
    TikTokPostSettings,
    TikTokRateLimitError,
    TikTokStillProcessingError,
    schedule_problem,
)
from app.agents.social_media_manager.services.video_publish_service import VideoPublishService

SETTINGS = {
    "title": "Banana bread",
    "description": "Fresh from the oven #bread",
    "privacy_level": "SELF_ONLY",
    "allow_comment": True,
    "allow_duet": False,
    "allow_stitch": False,
    "brand_organic": False,
    "brand_content": False,
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    async def find_one(self, query, projection=None, sort=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return

    async def update_many(self, query, update):
        class _R:
            modified_count = 0
        return _R()


class FakeDb:
    def __init__(self, collections=None):
        self._colls = {n: FakeCollection(docs) for n, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def _photo(**over):
    base = {"id": "d1", "user_id": "u1", "platform": "tiktok", "post_type": "feed",
            "content": "Banana bread", "image_url": "https://cdn.test/p.png", "status": "draft"}
    base.update(over)
    return base


def _approve(db, settings):
    return _run(ApprovalWorkflowService.approve_content(
        db, "u1", ["d1"], schedule_option="schedule",
        scheduled_datetime=datetime.utcnow() + timedelta(hours=2), tiktok_settings=settings,
    ))["responseData"]


# ── scheduling ────────────────────────────────────────────────────────────────

def test_scheduling_a_photo_without_posting_choices_is_refused():
    db = FakeDb({"content_drafts": [_photo()]})
    data = _approve(db, None)
    assert data["approved_drafts"] == []
    assert "Choose who can view" in data["errors"][0]["error"]
    assert db["content_drafts"].docs[0]["status"] == "draft"


def test_scheduling_a_photo_saves_the_choices_and_leaves_posting_to_the_scheduler():
    outstand = {"id": "c1", "user_id": "u1", "platform": "tiktok", "connection_status": "active",
                "connected_via": "outstand", "outstand_account_id": "os1"}
    db = FakeDb({"content_drafts": [_photo()], "social_connections": [outstand]})
    with patch.object(ApprovalWorkflowService, "_trigger_immediate_publishing", new=AsyncMock()) as sent:
        data = _approve(db, SETTINGS)
    assert data["errors"] == []
    draft = db["content_drafts"].docs[0]
    assert draft["status"] in ("scheduled", "staging_scheduled")
    assert draft["tiktok_settings"] == SETTINGS
    # Never handed to Outstand's own scheduler, which would post with defaults the user didn't pick.
    sent.assert_not_called()


def test_branded_content_cannot_be_scheduled_as_private():
    db = FakeDb({"content_drafts": [_photo()]})
    data = _approve(db, {**SETTINGS, "brand_content": True})
    assert "cannot be set to private" in data["errors"][0]["error"]


def test_a_tiktok_video_is_scheduled_as_before_without_choices():
    db = FakeDb({"content_drafts": [_photo(video_url="https://cdn.test/v.mp4")]})
    data = _approve(db, None)
    assert data["errors"] == []
    assert "tiktok_settings" not in db["content_drafts"].docs[0]


def test_schedule_problem_rules():
    assert schedule_problem(SETTINGS, 1) is None
    assert "no image" in schedule_problem(SETTINGS, 0)
    assert "Choose who can view" in schedule_problem({**SETTINGS, "privacy_level": ""}, 1)
    assert "Title is too long" in schedule_problem({**SETTINGS, "title": "x" * 91}, 1)
    assert "Description is too long" in schedule_problem({**SETTINGS, "description": "x" * 4001}, 1)


# ── publishing at the due time ───────────────────────────────────────────────

def test_a_scheduled_photo_goes_out_through_the_photo_path_with_its_saved_choices():
    draft = _photo(tiktok_settings=SETTINGS)
    for via in ("tiktok_direct_oauth", "outstand"):
        conn = {"connected_via": via, "platform": "tiktok", "outstand_account_id": "os1"}
        ok = {"success": True, "post_id": "p1"}
        with patch.object(VideoPublishService, "publish_scheduled_tiktok_photo", new=AsyncMock(return_value=ok)) as photo:
            result = _run(ApprovalWorkflowService._publish_to_platform(
                platform="tiktok", draft=draft, connection=conn, scheduled_datetime=None, db=FakeDb()))
        assert result == ok
        photo.assert_awaited_once()


def test_an_old_scheduled_photo_without_choices_says_what_is_missing():
    conn = {"connected_via": "tiktok_direct_oauth", "platform": "tiktok"}
    result = _run(ApprovalWorkflowService._publish_to_platform(
        platform="tiktok", draft=_photo(), connection=conn, scheduled_datetime=None, db=FakeDb()))
    assert result["success"] is False
    assert "missing its posting choices" in result["error"]
    assert "video_url" not in result["error"]


DIRECT = {"connected_via": "tiktok_direct_oauth", "platform": "tiktok", "id": "c1"}


def _publish(draft, conn=DIRECT, direct=None):
    with patch("app.agents.social_media_manager.services.tiktok_media_proxy.signed_urls",
               return_value=["https://api.test/social-media/tiktok-media/d1/0.jpg?sig=x"]), \
         patch("app.agents.social_media_manager.services.tiktok_direct_service.get_valid_tiktok_access_token",
               new=AsyncMock(return_value="tok")), \
         patch("app.agents.social_media_manager.services.tiktok_direct_service.publish_tiktok_photo_direct",
               new=direct) as photo:
        result = _run(VideoPublishService.publish_scheduled_tiktok_photo(draft, conn, FakeDb()))
    return result, photo


def test_direct_post_sends_the_saved_choices_and_signed_image_links():
    result, photo = _publish(_photo(tiktok_settings=SETTINGS, tiktok_auto_add_music=True),
                             direct=AsyncMock(return_value=("pub1", "PUBLISH_COMPLETE")))
    assert result["success"] is True and result["post_id"] == "pub1"
    token, urls, settings_, music = photo.await_args.args
    assert token == "tok" and music is True
    assert urls == ["https://api.test/social-media/tiktok-media/d1/0.jpg?sig=x"]
    assert settings_ == TikTokPostSettings(**SETTINGS)


def test_a_post_tiktok_is_still_processing_is_recorded_not_failed_so_it_is_never_sent_twice():
    still = TikTokStillProcessingError("still processing")
    still.publish_id = "pub9"
    result, _ = _publish(_photo(tiktok_settings=SETTINGS), direct=AsyncMock(side_effect=still))
    assert result["success"] is True
    assert result["post_id"] == "pub9"


def test_a_rate_limit_is_carried_so_the_scheduler_can_wait_instead_of_failing():
    info = rl.classify_tiktok("rate_limit_exceeded")
    err = TikTokRateLimitError("slow down", info, "rate_limit_exceeded")
    result, _ = _publish(_photo(tiktok_settings=SETTINGS), direct=AsyncMock(side_effect=err))
    assert result["success"] is False
    assert result["rate_limit"] is info


def test_missing_images_or_choices_fail_with_a_clear_message():
    direct = AsyncMock()
    result, photo = _publish(_photo(tiktok_settings={"nonsense": 1}), direct=direct)
    assert "missing its posting choices" in result["error"]
    photo.assert_not_called()
    result, photo = _publish(_photo(tiktok_settings=SETTINGS, image_url=None), direct=direct)
    assert "no image" in result["error"]
    photo.assert_not_called()
