"""
TikTok posting under rate limits.

Before: no throttle (two posts for one account ran at once), the HTTP status was never checked (a
bare 429 page crashed with a JSONDecodeError), a 429 on a status check failed a post TikTok had
already accepted, and a daily cap looked the same as any other failure.

Now: a temporary limit is retried, a daily or app-wide cap never is, a status check being limited
is waited out, one post per account at a time with a gap between them, and a limit comes back to the
user as a clear message with `error_code` / `retryable`, not a raw platform error.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.agents.social_media_manager.services import rate_limit as rl
from app.agents.social_media_manager.services import tiktok_direct_service as svc
from app.agents.social_media_manager.services import video_publish_service as vps
from app.agents.social_media_manager.services.tiktok_direct_service import (
    TikTokDirectAPIError,
    TikTokPostSettings,
    TikTokRateLimitError,
)
from tests.test_account_gate import FakeGateCollection, _apply, _match

VPS = vps.VideoPublishService

CREATOR_INFO = {
    "creator_nickname": "Test Creator",
    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
    "comment_disabled": False, "duet_disabled": False, "stitch_disabled": False,
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Scripted TikTok ───────────────────────────────────────────────────────────

class Resp:
    def __init__(self, payload=None, status=200, headers=None, content=b"", text="", bare=False):
        self._payload, self.status_code, self.headers = payload, status, headers or {}
        self.content, self.text, self._bare = content, text, bare

    def json(self):
        if self._bare:
            raise ValueError("not json")
        return self._payload


def ok(data=None):
    return Resp({"error": {"code": "ok"}, "data": data or {}})


def tt_error(code, status=429, message="Please review our integration guidelines"):
    return Resp({"error": {"code": code, "message": message, "log_id": "LOG1"}}, status=status)


def bare(status, headers=None):
    return Resp(status=status, headers=headers, bare=True, text="<html>slow down</html>")


def scripted(script, calls):
    """A fake httpx.AsyncClient: each URL suffix has a list of responses, consumed in order (the last
    one repeats). Every call is recorded in `calls` as (method, suffix)."""
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
                    calls.append((method, suffix))
                    return responses.pop(0) if len(responses) > 1 else responses[0]
            raise AssertionError(f"unexpected {method} {url}")

        async def post(self, url, **k):
            return await self._go("POST", url)

        async def put(self, url, **k):
            return await self._go("PUT", url)

        async def get(self, url, **k):
            return await self._go("GET", url)

    return FakeClient


SETTINGS = TikTokPostSettings(title="t", description="d", privacy_level="SELF_ONLY")


def _photo(script, calls):
    with patch.object(svc.httpx, "AsyncClient", scripted(script, calls)), patch.object(asyncio, "sleep", AsyncMock()):
        return _run(svc.publish_tiktok_photo_direct("tok", ["https://api/x/0.jpg"], SETTINGS))


def _base_script(**overrides):
    script = {
        "/creator_info/query/": [ok(CREATOR_INFO)],
        "/content/init/": [ok({"publish_id": "pp1"})],
        "/status/fetch/": [ok({"status": "PUBLISH_COMPLETE"})],
    }
    script.update(overrides)
    return script


# ── Recognising a limit ───────────────────────────────────────────────────────

def test_a_rate_limit_error_is_still_a_tiktok_error_and_keeps_its_code():
    e = TikTokRateLimitError("boom", rl.classify_tiktok("rate_limit_exceeded"))
    assert isinstance(e, TikTokDirectAPIError) and isinstance(e, rl.RateLimitedError)
    assert e.code == "rate_limit_exceeded" and e.rate_limit.retryable


def test_a_bare_429_page_is_a_rate_limit_not_a_json_crash():
    with pytest.raises(TikTokRateLimitError) as exc:
        svc._checked(bare(429, {"Retry-After": "9"}), "creator info query")
    assert exc.value.rate_limit.retry_after_s == 9.0


def test_a_bare_500_page_is_a_plain_error_not_a_rate_limit():
    with pytest.raises(TikTokDirectAPIError) as exc:
        svc._checked(bare(500), "creator info query")
    assert not isinstance(exc.value, rl.RateLimitedError) and "HTTP 500" in str(exc.value)


@pytest.mark.parametrize("code,kind", [("rate_limit_exceeded", rl.LimitKind.TEMPORARY),
                                       ("spam_risk_too_many_posts", rl.LimitKind.DAILY_CAP),
                                       ("reached_active_user_cap", rl.LimitKind.APP_CAP)])
def test_tiktok_error_codes_become_typed_limits(code, kind):
    with pytest.raises(TikTokRateLimitError) as exc:
        svc._checked(tt_error(code), "photo init")
    assert exc.value.rate_limit.kind == kind and exc.value.code == code and "LOG1" in str(exc.value)


def test_other_tiktok_errors_stay_plain():
    with pytest.raises(TikTokDirectAPIError) as exc:
        svc._checked(tt_error("invalid_param", status=400), "photo init")
    assert not isinstance(exc.value, rl.RateLimitedError)


# ── Retrying only what is worth retrying ──────────────────────────────────────

def test_a_temporary_limit_on_init_is_retried_until_it_goes_through():
    calls = []
    script = _base_script(**{"/content/init/": [tt_error("rate_limit_exceeded"), tt_error("rate_limit_exceeded"),
                                                ok({"publish_id": "pp1"})]})
    assert _photo(script, calls) == ("pp1", "PUBLISH_COMPLETE")
    assert [c for c in calls if c[1] == "/content/init/"] == [("POST", "/content/init/")] * 3


def test_a_bare_429_on_creator_info_is_retried_too():
    calls = []
    script = _base_script(**{"/creator_info/query/": [bare(429), ok(CREATOR_INFO)]})
    assert _photo(script, calls)[0] == "pp1"
    assert sum(1 for c in calls if c[1] == "/creator_info/query/") == 2


@pytest.mark.parametrize("code,kind", [("spam_risk_too_many_posts", rl.LimitKind.DAILY_CAP),
                                       ("reached_active_user_cap", rl.LimitKind.APP_CAP)])
def test_a_cap_is_tried_once_and_never_retried(code, kind):
    calls = []
    with pytest.raises(TikTokRateLimitError) as exc:
        _photo(_base_script(**{"/content/init/": [tt_error(code)]}), calls)
    assert sum(1 for c in calls if c[1] == "/content/init/") == 1
    assert exc.value.rate_limit.kind == kind and not exc.value.rate_limit.retryable


def test_a_limit_that_never_clears_gives_up_as_a_rate_limit_error():
    calls = []
    with pytest.raises(TikTokRateLimitError):
        _photo(_base_script(**{"/content/init/": [tt_error("rate_limit_exceeded")]}), calls)
    assert sum(1 for c in calls if c[1] == "/content/init/") == 4   # the default four attempts


def test_a_limited_status_check_does_not_fail_a_post_tiktok_already_accepted():
    calls = []
    script = _base_script(**{"/status/fetch/": [tt_error("rate_limit_exceeded"),
                                                ok({"status": "PROCESSING_DOWNLOAD"}),
                                                ok({"status": "PUBLISH_COMPLETE"})]})
    assert _photo(script, calls) == ("pp1", "PUBLISH_COMPLETE")
    assert sum(1 for c in calls if c[1] == "/status/fetch/") == 3
    assert sum(1 for c in calls if c[1] == "/content/init/") == 1   # the post was NOT re-submitted


def test_video_chunk_upload_retries_a_limit_without_re_initialising():
    calls = []
    script = {
        "/creator_info/query/": [ok(CREATOR_INFO)],
        "x.mp4": [Resp(status=200, content=b"video-bytes")],
        "/video/init/": [ok({"upload_url": "https://upload.example/x", "publish_id": "pv1"})],
        "upload.example/x": [Resp(status=429, headers={"Retry-After": "1"}), Resp(status=201)],
        "/status/fetch/": [ok({"status": "PUBLISH_COMPLETE"})],
    }
    with patch.object(svc.httpx, "AsyncClient", scripted(script, calls)), patch.object(asyncio, "sleep", AsyncMock()):
        result = _run(svc.publish_tiktok_direct("tok", "https://v/x.mp4", "cap", SETTINGS))
    assert result == ("pv1", "PUBLISH_COMPLETE")
    assert sum(1 for c in calls if c[1] == "upload.example/x") == 2
    assert sum(1 for c in calls if c[1] == "/video/init/") == 1


def test_a_non_limit_chunk_error_carries_a_code_and_is_not_retried():
    calls = []
    script = {
        "/creator_info/query/": [ok(CREATOR_INFO)],
        "x.mp4": [Resp(status=200, content=b"video-bytes")],
        "/video/init/": [ok({"upload_url": "https://upload.example/x", "publish_id": "pv1"})],
        "upload.example/x": [Resp(status=403, text="forbidden")],
    }
    with patch.object(svc.httpx, "AsyncClient", scripted(script, calls)), patch.object(asyncio, "sleep", AsyncMock()):
        with pytest.raises(TikTokDirectAPIError) as exc:
            _run(svc.publish_tiktok_direct("tok", "https://v/x.mp4", "cap", SETTINGS))
    assert exc.value.code == "http_403" and sum(1 for c in calls if c[1] == "upload.example/x") == 1


# ── What a failed job says ────────────────────────────────────────────────────

def test_job_failure_for_a_temporary_limit():
    f = VPS._failure_update(TikTokRateLimitError("x", rl.classify_tiktok("rate_limit_exceeded")))
    assert (f["status"], f["error_code"], f["retryable"]) == ("failed", "rate_limited", True)
    assert "limiting how fast" in f["error"]


@pytest.mark.parametrize("code,error_code", [("spam_risk_too_many_posts", "daily_cap"),
                                             ("reached_active_user_cap", "app_cap")])
def test_job_failure_for_a_cap_says_not_to_retry(code, error_code):
    f = VPS._failure_update(TikTokRateLimitError("x", rl.classify_tiktok(code)))
    assert (f["status"], f["error_code"], f["retryable"]) == ("failed", error_code, False)
    assert "reconnect" not in f["error"].lower()


def test_job_failure_for_an_ordinary_error_is_unchanged():
    assert VPS._failure_update(TikTokDirectAPIError("nope")) == {"status": "failed", "error": "nope"}


# ── The per-account gate around a job ─────────────────────────────────────────

class _Docs:
    def __init__(self, key):
        self.docs, self.key = {}, key

    async def insert_one(self, doc):
        self.docs[doc[self.key]] = dict(doc)

    async def update_one(self, filt, update, upsert=False):
        doc = self.docs.get(filt.get(self.key))
        if doc is None:
            return
        for k, v in update.get("$set", {}).items():
            doc[k] = v
        for k in update.get("$unset", {}):
            doc.pop(k, None)

    async def find_one(self, filt, projection=None):
        doc = self.docs.get(filt.get(self.key))
        return dict(doc) if doc else None


class JobDb:
    def __init__(self):
        self.jobs, self.gate, self.drafts = _Docs("job_id"), FakeGateCollection(), _Docs("id")
        self.drafts.docs["d1"] = {"id": "d1", "status": "draft"}

    def __getitem__(self, name):
        return {"video_publish_jobs": self.jobs, rl.GATE_COLLECTION: self.gate, "content_drafts": self.drafts}[name]


DIRECT = {"id": "tt_1", "connected_via": "tiktok_direct_oauth"}
KEY = "tiktok:tt_1"


@pytest.fixture
def env(monkeypatch):
    db = JobDb()
    monkeypatch.setattr(vps, "get_db", lambda: db)
    return db, monkeypatch


def _job(db, platform="tiktok"):
    return _run(VPS.create_job("d1", platform, "u1"))


def _run_job(db, job_id, conn=DIRECT):
    _run(VPS.run_job(job_id, "d1", "tiktok", "https://v/x.mp4", "caption", conn, db, None))
    return db.jobs.docs[job_id]


def test_a_post_goes_through_and_the_account_is_released_with_a_gap(env):
    db, mp = env
    with patch.object(VPS, "_publish_tiktok", AsyncMock(return_value=("pid", None))):
        job = _run_job(db, _job(db))
    assert job["status"] == "published" and job["account_key"] == KEY
    gate = db.gate.docs[KEY]
    assert "lease_owner" not in gate and gate["next_allowed_at"] > rl.utcnow()
    assert db.drafts.docs["d1"]["status"] == "published"


def test_the_next_post_for_the_same_account_waits_for_the_gap(env):
    db, mp = env
    mp.setenv("TIKTOK_MIN_GAP_S", "0.4")
    mp.setenv("PUBLISH_JOB_WAIT_MAX_S", "5")
    started = []

    async def fake_publish(*a, **k):
        started.append(datetime.now(timezone.utc))
        return "pid", None

    a, b = _job(db), _job(db)

    async def both():
        return await asyncio.gather(VPS.run_job(a, "d1", "tiktok", "u", "c", DIRECT, db, None),
                                    VPS.run_job(b, "d1", "tiktok", "u", "c", DIRECT, db, None))

    with patch.object(VPS, "_publish_tiktok", side_effect=fake_publish):
        _run(both())
    assert len(started) == 2 and (started[1] - started[0]).total_seconds() >= 0.35
    assert all(j["status"] == "published" for j in db.jobs.docs.values())


def test_a_waiting_job_shows_when_it_expects_to_go(env):
    db, mp = env
    mp.setenv("TIKTOK_MIN_GAP_S", "0.5")
    mp.setenv("PUBLISH_JOB_WAIT_MAX_S", "5")
    seen = []

    async def fake_publish(*a, **k):
        return "pid", None

    first, second = _job(db), _job(db)

    async def run():
        await VPS.run_job(first, "d1", "tiktok", "u", "c", DIRECT, db, None)
        task = asyncio.ensure_future(VPS.run_job(second, "d1", "tiktok", "u", "c", DIRECT, db, None))
        await asyncio.sleep(0.1)
        seen.append(dict(db.jobs.docs[second]))
        await task

    with patch.object(VPS, "_publish_tiktok", side_effect=fake_publish):
        _run(run())
    assert seen[0]["status"] == "queued" and seen[0]["wait_reason"] == "account_busy" and seen[0]["next_attempt_at"]


def test_a_job_that_cannot_get_the_account_in_time_says_when_to_try_again(env):
    db, mp = env
    mp.setenv("PUBLISH_JOB_WAIT_MAX_S", "0.3")
    _run(rl.acquire(db, KEY, "someone-else", lease_s=300))          # another post holds the account
    publish = AsyncMock(return_value=("pid", None))
    with patch.object(VPS, "_publish_tiktok", publish):
        job = _run_job(db, _job(db))
    assert job["status"] == "failed" and job["error_code"] == "rate_limited" and job["retryable"] is True
    assert "try again" in job["error"].lower() and job["resume_at"]
    publish.assert_not_called()


def test_a_temporary_limit_fails_the_job_clearly_and_pauses_the_account(env):
    db, mp = env
    err = TikTokRateLimitError("limited", rl.classify_tiktok("rate_limit_exceeded"))
    with patch.object(VPS, "_publish_tiktok", AsyncMock(side_effect=err)):
        job = _run_job(db, _job(db))
    assert (job["status"], job["error_code"], job["retryable"]) == ("failed", "rate_limited", True)
    gate = db.gate.docs[KEY]
    assert gate["paused_until"] > rl.utcnow() and "lease_owner" not in gate     # paused AND released
    assert gate["pause_code"] == "rate_limit_exceeded"


def test_a_paused_account_makes_the_next_job_wait_or_say_when(env):
    db, mp = env
    mp.setenv("PUBLISH_JOB_WAIT_MAX_S", "0.3")
    _run(rl.pause(db, KEY, code="rate_limit_exceeded", reason="x"))   # 5 minutes
    with patch.object(VPS, "_publish_tiktok", AsyncMock(return_value=("pid", None))) as publish:
        job = _run_job(db, _job(db))
    assert job["status"] == "failed" and job["error_code"] == "rate_limited" and job["resume_at"]
    publish.assert_not_called()


def test_a_daily_cap_is_reported_as_one_and_does_not_pause_the_account(env):
    db, mp = env
    err = TikTokRateLimitError("capped", rl.classify_tiktok("spam_risk_too_many_posts"))
    with patch.object(VPS, "_publish_tiktok", AsyncMock(side_effect=err)):
        job = _run_job(db, _job(db))
    assert (job["status"], job["error_code"], job["retryable"]) == ("failed", "daily_cap", False)
    assert "daily posting limit" in job["error"]
    assert "paused_until" not in db.gate.docs[KEY]


def test_a_good_post_resets_the_accounts_backoff(env):
    db, mp = env
    db.gate.docs[KEY] = {"_id": KEY, "backoff_level": 2}
    with patch.object(VPS, "_publish_tiktok", AsyncMock(return_value=("pid", None))):
        _run_job(db, _job(db))
    assert db.gate.docs[KEY]["backoff_level"] == 0


def test_an_account_we_cannot_identify_is_not_gated(env):
    db, mp = env
    with patch.object(VPS, "_publish_tiktok", AsyncMock(return_value=("pid", None))):
        job = _run_job(db, _job(db), conn={"connected_via": "tiktok_direct_oauth"})
    assert job["status"] == "published" and not db.gate.docs


def test_outstand_and_direct_accounts_get_their_own_keys():
    assert VPS._gate_target("tiktok", {"connected_via": "outstand", "outstand_account_id": "7ylKW"})[0] == "outstand:7ylKW"
    assert VPS._gate_target("tiktok", DIRECT)[0] == KEY
    assert VPS._gate_target("instagram_reels", {"ig_user_id": "1784"})[0] == "instagram:1784"
    assert VPS._gate_target("facebook_reels", {"page_id": "99"})[0] == "facebook:99"
    assert VPS._gate_target("tiktok", {})[0] is None


# ── Outstand ──────────────────────────────────────────────────────────────────

def _status_error(code):
    request = httpx.Request("GET", "https://api.outstand.so/v1/posts/p1")
    response = httpx.Response(code, headers={"Retry-After": "3"}, request=request)
    return httpx.HTTPStatusError(f"{code} for url", request=request, response=response)


class FakeOutstand:
    def __init__(self, post_errors=None, failures=None, publish_errors=None):
        self.post_errors, self.failures, self.publish_errors = list(post_errors or []), failures or {}, list(publish_errors or [])
        self.published, self.polls = [], 0

    async def publish_post(self, outstand_account_ids, content, media_urls=None, platform_config=None, **k):
        if self.publish_errors:
            raise self.publish_errors.pop(0)
        self.published.append(platform_config["tiktok"])
        return {"success": True, "post": {"id": f"p{len(self.published)}"}}

    async def get_post(self, post_id):
        self.polls += 1
        if self.post_errors:
            raise self.post_errors.pop(0)
        err = self.failures.get(post_id)
        if err:
            return {"post": {"socialAccounts": [{"status": "failed", "error": err}]}}
        return {"post": {"publishedAt": "2026-11-03T12:00:00Z", "isDraft": False, "url": "https://tiktok/p"}}


def _via_outstand(fake, settings_=SETTINGS):
    with patch.object(vps, "OutstandService", lambda: fake), patch.object(asyncio, "sleep", AsyncMock()):
        return _run(VPS._publish_tiktok_via_outstand("acc", "caption", ["u"], settings_))


def test_outstand_rate_limiting_a_status_check_does_not_fail_the_post():
    fake = FakeOutstand(post_errors=[_status_error(429)])
    assert _via_outstand(fake) == ("p1", "https://tiktok/p")
    assert fake.polls == 2 and len(fake.published) == 1


def test_outstand_rate_limiting_the_post_itself_is_retried():
    fake = FakeOutstand(publish_errors=[httpx.HTTPStatusError("429 Too Many Requests for url", request=None, response=None)])
    assert _via_outstand(fake)[0] == "p1"
    assert len(fake.published) == 1


@pytest.mark.parametrize("reason,error_code", [
    ("TikTok API error (rate_limit_exceeded): slow down [rate_limit_exceeded, log_id=a]", "rate_limited"),
    ("TikTok API error (spam_risk_too_many_posts): daily cap", "daily_cap"),
    ("TikTok API error (reached_active_user_cap): cap", "app_cap"),
])
def test_a_limit_reported_by_outstand_is_explained_and_never_re_submitted(reason, error_code):
    fake = FakeOutstand(failures={"p1": reason})
    with pytest.raises(rl.RateLimitedError) as exc:
        _via_outstand(fake)
    assert len(fake.published) == 1                      # no SELF_ONLY / MEDIA_UPLOAD fallback posts
    assert VPS._failure_update(exc.value)["error_code"] == error_code


def test_the_audit_fallback_still_works_for_errors_that_are_not_limits():
    fake = FakeOutstand(failures={"p1": "TikTok API error (unaudited_client_can_only_post_to_private_accounts): x"})
    with pytest.raises(ValueError):
        _via_outstand(fake)
    assert fake.published[1] == {"postMode": "MEDIA_UPLOAD"}


# ── Jobs whose worker went away ───────────────────────────────────────────────

class _ManyDocs:
    def __init__(self, docs):
        self.docs = docs

    async def update_many(self, filt, update):
        n = 0
        for d in self.docs:
            if _match(d, filt):
                _apply(d, update)
                n += 1
        return type("R", (), {"modified_count": n})()


def test_jobs_left_behind_by_a_restart_are_failed_with_a_clear_message(monkeypatch):
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    old = "2026-11-03T10:00:00+00:00"       # two hours ago
    fresh = "2026-11-03T11:50:00+00:00"     # ten minutes ago
    docs = [
        {"job_id": "stale-uploading", "status": "uploading", "updated_at": old},
        {"job_id": "stale-queued", "status": "queued", "updated_at": old},
        {"job_id": "busy-now", "status": "uploading", "updated_at": fresh},
        {"job_id": "done", "status": "published", "updated_at": old},
        {"job_id": "old-no-updated-at", "status": "uploading", "created_at": old},
        {"job_id": "new-no-updated-at", "status": "uploading", "created_at": fresh},
    ]
    monkeypatch.setattr(vps, "_jobs_col", lambda: _ManyDocs(docs))
    assert _run(vps.mark_stale_jobs(now)) == 3
    by_id = {d["job_id"]: d for d in docs}
    for job_id in ("stale-uploading", "stale-queued", "old-no-updated-at"):
        d = by_id[job_id]
        assert d["status"] == "failed" and d["error_code"] == "interrupted" and d["retryable"] is True
        assert "interrupted" in d["error"] and d["updated_at"] == now.isoformat()
    for job_id, status in (("busy-now", "uploading"), ("done", "published"), ("new-no-updated-at", "uploading")):
        assert by_id[job_id]["status"] == status


def test_the_public_view_of_a_job_hides_internal_fields():
    job = {"job_id": "j", "user_id": "u1", "account_key": "tiktok:tt_1", "status": "failed", "error": "x",
           "error_code": "daily_cap", "retryable": False, "draft_id": "d1", "platform": "tiktok", "_id": "abc"}
    view = vps.public_job_view(job)
    assert view == {"job_id": "j", "status": "failed", "error": "x", "error_code": "daily_cap",
                    "retryable": False, "draft_id": "d1", "platform": "tiktok"}
    assert "user_id" not in view and "account_key" not in view


def test_a_rate_limited_api_response_is_a_429_with_retry_after():
    err = TikTokRateLimitError("x", rl.classify_tiktok("rate_limit_exceeded", headers={"Retry-After": "12"}))
    status, message, headers = rl.http_limit_response(err, "tiktok")
    assert status == 429 and headers == {"Retry-After": "12"} and "limiting how fast" in message
    assert rl.http_limit_response(ValueError("Couldn't reach TikTok"), "tiktok") is None
    capped = rl.http_limit_response(TikTokRateLimitError("x", rl.classify_tiktok("spam_risk_too_many_posts")), "tiktok")
    assert capped[0] == 429 and capped[2] == {}
