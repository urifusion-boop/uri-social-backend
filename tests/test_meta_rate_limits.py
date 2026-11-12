"""
Posting to Instagram / Facebook when Meta throttles us.

Before: a Meta rate limit looked like any other error (or, for Facebook feed/carousel posts, like
nothing at all — FacebookService threw the error body away), so it was reported generically and the
scheduler retried it blind every five minutes. Now the error code and the usage headers are read, a
throttle comes back as a typed `rate_limit` (with the wait Meta gave us when it gave one), and the
account is paused until then instead of being hammered. Existing behaviour — including the
"media not ready" (2207027) retry — is unchanged.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.agents.social_media_manager.services import instagram_direct_service as ids
from app.agents.social_media_manager.services import publish_dispatch as pd
from app.agents.social_media_manager.services import rate_limit as rl
from app.agents.social_media_manager.services import video_publish_service as vps
from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService
from app.services.FacebookService import FacebookService
from tests.test_account_gate import FakeGateCollection

IDS = ids.InstagramDirectService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class Resp:
    def __init__(self, payload, status=200, headers=None):
        self._payload, self.status_code, self.headers = payload, status, headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload


def graph_error(code, message="x", subcode=None, status=400, headers=None):
    err = {"message": message, "code": code}
    if subcode:
        err["error_subcode"] = subcode
    return Resp({"error": err}, status=status, headers=headers)


def usage_header(minutes):
    return {"X-Business-Use-Case-Usage": f'{{"123": [{{"type": "instagram", "estimated_time_to_regain_access": {minutes}}}]}}'}


def scripted(script, calls):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def _go(self, method, url):
            for suffix, responses in script.items():
                if url.endswith(suffix):
                    calls.append(suffix)
                    return responses.pop(0) if len(responses) > 1 else responses[0]
            raise AssertionError(f"unexpected {method} {url}")

        async def post(self, url, **k):
            return await self._go("POST", url)

        async def get(self, url, **k):
            return await self._go("GET", url)

    return FakeClient


def _reel(script, calls):
    with patch.object(ids.httpx, "AsyncClient", scripted(script, calls)), patch.object(asyncio, "sleep", AsyncMock()):
        return _run(IDS.publish_reel("ig1", "tok", "https://v/x.mp4", "caption"))


# ── Instagram publishing ──────────────────────────────────────────────────────

def test_a_throttled_container_creation_comes_back_as_a_typed_limit_with_metas_wait():
    calls = []
    script = {"/media": [graph_error(80002, "There have been too many calls", headers=usage_header(30))]}
    result = _reel(script, calls)
    assert result["success"] is False and result["error"].startswith("Reel container error: There have been")
    info = result["rate_limit"]
    assert info.kind == rl.LimitKind.WINDOW and info.platform == "instagram" and info.code == "80002"
    assert info.retry_after_s == 31 * 60       # Meta's 30 minutes plus a one-minute margin
    assert calls == ["/media"]


def test_a_throttled_publish_stops_straight_away_instead_of_retrying():
    calls = []
    script = {"/media": [Resp({"id": "c1"})], "/media_publish": [graph_error(4, "Application request limit reached")]}
    result = _reel(script, calls)
    assert result["rate_limit"].code == "4" and "Application request limit reached" in result["error"]
    assert calls.count("/media_publish") == 1


def test_media_not_ready_is_still_retried_and_still_succeeds():
    """The one error that was already handled (2207027) must behave exactly as before."""
    calls = []
    script = {"/media": [Resp({"id": "c1"})],
              "/media_publish": [graph_error(9007, "Media is not ready", subcode=2207027), Resp({"id": "post1"})]}
    result = _reel(script, calls)
    assert result == {"success": True, "post_id": "post1", "raw_response": {"id": "post1"}}
    assert calls.count("/media_publish") == 2


def test_an_ordinary_error_has_no_rate_limit_and_the_same_message():
    calls = []
    result = _reel({"/media": [graph_error(100, "Invalid parameter")]}, calls)
    assert result == {"success": False, "error": "Reel container error: Invalid parameter"}


def test_carousel_and_story_failures_carry_the_limit_too():
    # the slide images are prepared and re-hosted first; those steps are not what is under test.
    # (The real upload helper uses syntax this test interpreter can't import, so stand one in.)
    import sys
    import types
    fake_upload = types.ModuleType("app.utils.s3_upload")
    fake_upload.upload_bytes = AsyncMock(return_value="https://cdn/x.jpg")
    slides = [{"image_url": "https://x/1.jpg"}, {"image_url": "https://x/2.jpg"}]

    def carousel(script):
        with patch.object(ids.httpx, "AsyncClient", scripted(script, [])), patch.object(asyncio, "sleep", AsyncMock()), \
             patch.object(IDS, "_download_as_jpeg", AsyncMock(return_value=b"jpeg")), \
             patch.dict(sys.modules, {"app.utils.s3_upload": fake_upload}):
            return _run(IDS.publish_carousel("ig1", "tok", "cap", slides, page_id="p1"))

    # the second slide's container is throttled
    result = carousel({"/media": [Resp({"id": "i1"}), graph_error(17, "User request limit reached")]})
    assert result["success"] is False and result["rate_limit"].code == "17"
    assert result["error"].startswith("Carousel item 1 container error")

    # and the carousel's own publish step
    result = carousel({"/media": [Resp({"id": "i1"}), Resp({"id": "i2"}), Resp({"id": "carousel1"})],
                       "/media_publish": [graph_error(80002, "too many calls", headers=usage_header(15))]})
    assert result["success"] is False and result["rate_limit"].retry_after_s == 16 * 60


# ── Reel / Facebook video jobs ────────────────────────────────────────────────

def _vps_client(script):
    return scripted(script, [])


def test_a_throttled_reel_job_raises_a_typed_error_with_the_same_message_as_before():
    script = {"/media": [graph_error(17, "User request limit reached")]}
    with patch.object(vps.httpx, "AsyncClient", _vps_client(script)), patch.object(asyncio, "sleep", AsyncMock()):
        with pytest.raises(vps.MetaAPIError) as exc:
            _run(vps.VideoPublishService._publish_instagram_reel("ig1", "tok", "https://v/x.mp4", "cap"))
    assert isinstance(exc.value, ValueError) and str(exc.value) == "IG container create error: User request limit reached"
    assert exc.value.rate_limit.code == "17"


def test_a_throttle_on_the_container_poll_stops_it_but_other_poll_hiccups_do_not():
    ok_create = Resp({"id": "c1"})
    # a throttle on the status poll: stop, don't keep calling Meta
    throttled = {"/media": [ok_create], "/c1": [graph_error(4, "limit", headers=usage_header(5))]}
    with patch.object(vps.httpx, "AsyncClient", _vps_client(throttled)), patch.object(asyncio, "sleep", AsyncMock()):
        with pytest.raises(vps.MetaAPIError) as exc:
            _run(vps.VideoPublishService._publish_instagram_reel("ig1", "tok", "https://v/x.mp4", "cap"))
    assert exc.value.rate_limit.retry_after_s == 6 * 60
    # an unrelated hiccup on the poll is ignored, exactly as before, and the post still goes out
    hiccup = {"/media": [Resp({"id": "c1"})],
              "/c1": [graph_error(2, "temporary"), Resp({"status_code": "FINISHED"})],
              "/media_publish": [Resp({"id": "m1"})]}
    with patch.object(vps.httpx, "AsyncClient", _vps_client(hiccup)), patch.object(asyncio, "sleep", AsyncMock()):
        post_id, url = _run(vps.VideoPublishService._publish_instagram_reel("ig1", "tok", "https://v/x.mp4", "cap"))
    assert post_id == "m1"


def test_a_throttled_facebook_video_job_is_typed():
    script = {"/me": [Resp({"id": "page1"})], "/videos": [graph_error(32, "Page request limit reached")]}
    with patch.object(vps.httpx, "AsyncClient", _vps_client(script)):
        with pytest.raises(vps.MetaAPIError) as exc:
            _run(vps.VideoPublishService._publish_facebook_video("tok", "https://v/x.mp4", "cap"))
    assert exc.value.rate_limit.platform == "facebook" and exc.value.rate_limit.code == "32"


def test_a_throttled_job_fails_with_a_clear_message_and_no_reconnect_advice():
    exc = vps.MetaAPIError("IG publish error: (#4) Application request limit reached",
                           rl.classify_meta(400, {"code": 4}, platform="instagram", now=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)))
    f = vps.VideoPublishService._failure_update(exc, "instagram")
    assert (f["status"], f["error_code"], f["retryable"]) == ("failed", "rate_limited", True)
    assert "Instagram is limiting" in f["error"] and "reconnect" not in f["error"].lower()


# ── Facebook feed / carousel ──────────────────────────────────────────────────

class FakeFbClient:
    def __init__(self, response):
        self.response = response

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return self.response


def _fb(response):
    with patch("app.services.FacebookService.httpx.AsyncClient", FakeFbClient(response)):
        return _run(FacebookService.publish_post("page1", {"message": "hi"}, "tok"))


def test_facebook_failures_now_keep_what_meta_said():
    failure = _fb(graph_error(32, "Page request limit reached", headers=usage_header(10)))
    assert failure["status"] is False and failure["responseCode"] == 404          # the old shape is untouched
    assert failure["meta_error"] == {"http_status": 400, "code": 32, "subcode": None, "message": "Page request limit reached"}
    assert failure["rate_limit"].platform == "facebook" and failure["rate_limit"].retry_after_s == 11 * 60


def test_a_facebook_failure_that_is_not_a_limit_has_no_rate_limit_key():
    failure = _fb(graph_error(190, "Error validating access token"))
    assert failure["meta_error"]["code"] == 190 and "rate_limit" not in failure


def test_a_non_json_facebook_failure_does_not_crash():
    class Bare(Resp):
        def json(self):
            raise ValueError("no json")

    failure = _fb(Bare({}, status=502))
    assert failure["meta_error"]["http_status"] == 502 and failure["status"] is False


def test_facebook_success_is_unchanged():
    ok = _fb(Resp({"id": "page1_post1"}))
    assert ok["status"] is True and ok["responseData"] == {"id": "page1_post1"}


def test_the_publish_result_for_a_failed_facebook_post_now_has_an_error_and_the_limit():
    failure = _fb(graph_error(32, "Page request limit reached"))
    result = ApprovalWorkflowService._facebook_result(False, None, failure)
    assert result["success"] is False and result["error"] == "Facebook publish failed: Page request limit reached"
    assert result["rate_limit"].code == "32"
    assert ApprovalWorkflowService._facebook_result(True, "p1", {"status": True}) == {
        "success": True, "post_id": "p1", "raw_response": {"status": True}}


# ── End to end: a Meta throttle pauses the account until the wait Meta gave ───

class GateDb:
    def __init__(self):
        self.gate = FakeGateCollection()

    def __getitem__(self, name):
        assert name == rl.GATE_COLLECTION
        return self.gate


def test_an_instagram_throttle_pauses_that_account_until_metas_estimate():
    db = GateDb()
    result = {"success": False, "error": "Reel container error: too many calls",
              **rl.meta_limit_fields(Resp({}, headers=usage_header(30)), {"error": {"code": 80002}}, "instagram")}
    out = _run(pd.dispatch(db, "instagram", {"ig_user_id": "1784"}, AsyncMock(return_value=result)))
    assert out.deferred
    paused = db.gate.docs["instagram:1784"]["paused_until"]
    assert timedelta(minutes=30) < paused - rl.utcnow() <= timedelta(minutes=31, seconds=5)
    # and the next post for that account is deferred, not sent into the same wall
    again = _run(pd.dispatch(db, "instagram", {"ig_user_id": "1784"}, AsyncMock(), wait_max_s=0.0))
    assert again.deferred and again.resume_at == paused


def test_another_instagram_account_is_not_held_up_by_it():
    db = GateDb()
    _run(rl.pause(db, "instagram:1784", until=rl.utcnow() + timedelta(minutes=30)))
    publish = AsyncMock(return_value={"success": True})
    out = _run(pd.dispatch(db, "instagram", {"ig_user_id": "9999"}, publish))
    assert not out.deferred and publish.await_count == 1
