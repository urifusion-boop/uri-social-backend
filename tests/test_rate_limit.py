"""
Rate-limit classification and retry (services/rate_limit.py).

What must hold: a TikTok/Meta limit is recognised from its code, its HTTP status or just its
text (Outstand reports failures as strings); temporary limits are retried within a budget while
caps are never retried; and the user-facing wording never promises a reset time the platform
didn't give.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.social_media_manager.services import rate_limit as rl
from app.agents.social_media_manager.services.rate_limit import LimitKind, RateLimitedError


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


# ── TikTok ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,kind", [
    ("rate_limit_exceeded", LimitKind.TEMPORARY),
    ("spam_risk_too_many_posts", LimitKind.DAILY_CAP),
    ("reached_active_user_cap", LimitKind.APP_CAP),
])
def test_tiktok_codes(code, kind):
    info = rl.classify_tiktok(code)
    assert info.kind == kind and info.platform == "tiktok" and info.code == code


def test_tiktok_429_without_a_code_is_temporary():
    assert rl.classify_tiktok(None, status=429).kind == LimitKind.TEMPORARY


@pytest.mark.parametrize("code", ["invalid_param", "url_ownership_unverified", "scope_not_authorized",
                                  "unaudited_client_can_only_post_to_private_accounts", "", None])
def test_other_tiktok_errors_are_not_rate_limits(code):
    assert rl.classify_tiktok(code, status=400) is None


def test_caps_are_not_retryable_and_temporary_is():
    assert rl.classify_tiktok("spam_risk_too_many_posts").retryable is False
    assert rl.classify_tiktok("reached_active_user_cap").retryable is False
    assert rl.classify_tiktok("rate_limit_exceeded").retryable is True


def test_retry_after_seconds_and_date():
    assert rl.parse_retry_after({"Retry-After": "17"}) == 17.0
    soon = (datetime.now(timezone.utc) + timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 60 < rl.parse_retry_after({"retry-after": soon}) <= 91
    assert rl.parse_retry_after({}) is None and rl.parse_retry_after(None) is None
    assert rl.parse_retry_after({"Retry-After": "soon"}) is None


def test_tiktok_429_carries_retry_after():
    info = rl.classify_tiktok("rate_limit_exceeded", headers={"Retry-After": "8"})
    assert info.retry_after_s == 8.0


# ── Meta ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", [4, 17, 32, 613, 80001, 80002])
def test_meta_throttle_codes_are_windows(code):
    info = rl.classify_meta(400, {"code": code}, platform="instagram", now=NOW)
    assert info.kind == LimitKind.WINDOW and info.platform == "instagram" and info.code == str(code)


@pytest.mark.parametrize("body", [{"code": 100}, {"code": 190}, {"code": 2207027}, {}, None])
def test_other_meta_errors_are_not_rate_limits(body):
    assert rl.classify_meta(400, body, now=NOW) is None


def test_meta_uses_estimated_time_to_regain_access_in_minutes():
    headers = {"X-Business-Use-Case-Usage":
               '{"123": [{"type": "instagram", "call_count": 100, "estimated_time_to_regain_access": 30}],'
               ' "456": [{"type": "pages", "estimated_time_to_regain_access": 5}]}'}
    info = rl.classify_meta(400, {"code": 80002}, headers, platform="instagram", now=NOW)
    # the longest estimate wins (30 min) plus a one-minute margin
    assert info.retry_after_s == 31 * 60
    assert info.resume_at == NOW + timedelta(minutes=31)
    assert "12:31 UTC" in info.user_message


def test_meta_without_any_time_leaves_the_wait_to_the_gate():
    info = rl.classify_meta(400, {"code": 4}, platform="facebook", now=NOW)
    assert info.retry_after_s is None and info.resume_at is None


def test_usage_header_garbage_is_ignored():
    assert rl.parse_estimated_regain_minutes({"X-Business-Use-Case-Usage": "not json"}) is None
    assert rl.parse_estimated_regain_minutes({"X-Business-Use-Case-Usage": '{"1": [{"estimated_time_to_regain_access": 0}]}'}) is None


# ── Text (Outstand reports failures as strings) ───────────────────────────────

@pytest.mark.parametrize("raw,kind", [
    ("TikTok API error (rate_limit_exceeded): Too many requests [rate_limit_exceeded, log_id=abc]", LimitKind.TEMPORARY),
    ("photo init: Please review [spam_risk_too_many_posts, log_id=x]", LimitKind.DAILY_CAP),
    ("TikTok publish failed via Outstand: reached_active_user_cap", LimitKind.APP_CAP),
    ("429 Too Many Requests for url https://api.outstand.so/v1/posts/", LimitKind.TEMPORARY),
    ("Rate limit exceeded, retry later", LimitKind.TEMPORARY),
])
def test_tiktok_text(raw, kind):
    assert rl.classify_text("tiktok", raw).kind == kind


@pytest.mark.parametrize("raw", [
    "(#4) Application request limit reached",
    "(#17) User request limit reached",
    "Calls to this API have exceeded the rate limit.",
    "(#80002) There have been too many calls to this Instagram account",
])
def test_meta_text(raw):
    assert rl.classify_text("instagram", raw).kind == LimitKind.WINDOW


@pytest.mark.parametrize("raw", ["", None, "Media container error: Invalid image", "Error publishing post (#100)",
                                 "TikTok couldn't fetch the media [url_ownership_unverified]"])
def test_unrelated_text_is_not_a_limit(raw):
    assert rl.classify_text("instagram", raw) is None
    assert rl.classify_text("tiktok", raw) is None


class _CodeError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def test_classify_exception_prefers_its_own_rate_limit_then_code_then_text():
    carried = rl.classify_tiktok("spam_risk_too_many_posts")
    assert rl.classify_exception(RateLimitedError("x", carried), "tiktok") is carried
    assert rl.classify_exception(_CodeError("boom", "rate_limit_exceeded"), "tiktok").kind == LimitKind.TEMPORARY
    assert rl.classify_exception(ValueError("429 Too Many Requests"), "tiktok").kind == LimitKind.TEMPORARY
    assert rl.classify_exception(ValueError("Invalid image"), "tiktok") is None


# ── Wording ───────────────────────────────────────────────────────────────────

def test_cap_messages_never_promise_a_reset_time():
    daily = rl.classify_tiktok("spam_risk_too_many_posts").user_message
    app_cap = rl.classify_tiktok("reached_active_user_cap").user_message
    assert "daily posting limit" in daily and "doesn't say exactly when" in daily
    assert "UTC" not in daily and "UTC" not in app_cap


def test_messages_never_tell_the_user_to_reconnect():
    for code in ("rate_limit_exceeded", "spam_risk_too_many_posts", "reached_active_user_cap"):
        assert "reconnect" not in rl.classify_tiktok(code).user_message.lower()
    assert "reconnect" not in rl.classify_meta(400, {"code": 4}, platform="instagram", now=NOW).user_message.lower()


def test_error_code_the_frontend_switches_on():
    assert rl.classify_tiktok("rate_limit_exceeded").error_code == "rate_limited"
    assert rl.classify_meta(400, {"code": 17}, now=NOW).error_code == "rate_limited"
    assert rl.classify_tiktok("spam_risk_too_many_posts").error_code == "daily_cap"
    assert rl.classify_tiktok("reached_active_user_cap").error_code == "app_cap"


# ── with_backoff ──────────────────────────────────────────────────────────────

def _classify(exc):
    return rl.classify_exception(exc, "tiktok")


class _Flaky:
    """Fails with the given errors in order, then returns 'ok'."""
    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


class _Sleeps:
    def __init__(self):
        self.waits = []

    async def __call__(self, seconds):
        self.waits.append(seconds)


def _retry(fn, sleeps, **kw):
    kw.setdefault("jitter", 0.0)
    return _run(rl.with_backoff(fn, classify=_classify, sleep=sleeps, **kw))


def test_a_temporary_limit_is_retried_with_growing_waits_and_then_succeeds():
    fn, sleeps = _Flaky(_CodeError("x", "rate_limit_exceeded"), _CodeError("x", "rate_limit_exceeded")), _Sleeps()
    assert _retry(fn, sleeps, budget_s=60) == "ok"
    assert fn.calls == 3 and sleeps.waits == [2.0, 4.0]


def test_retry_after_is_honoured():
    err = rl.RateLimitedError("slow down", rl.classify_tiktok("rate_limit_exceeded", headers={"Retry-After": "7"}))
    fn, sleeps = _Flaky(err), _Sleeps()
    assert _retry(fn, sleeps, budget_s=60) == "ok" and sleeps.waits == [7.0]


def test_caps_are_never_retried():
    for code in ("spam_risk_too_many_posts", "reached_active_user_cap"):
        fn, sleeps = _Flaky(_CodeError("capped", code)), _Sleeps()
        with pytest.raises(RateLimitedError) as exc:
            _retry(fn, sleeps)
        assert fn.calls == 1 and sleeps.waits == [] and not exc.value.rate_limit.retryable


def test_a_meta_window_is_deferred_not_slept_on():
    fn, sleeps = _Flaky(rl.RateLimitedError("x", rl.classify_meta(400, {"code": 17}, platform="instagram", now=NOW))), _Sleeps()
    with pytest.raises(RateLimitedError) as exc:
        _run(rl.with_backoff(fn, classify=lambda e: rl.classify_exception(e, "instagram"), sleep=sleeps))
    assert fn.calls == 1 and sleeps.waits == [] and exc.value.rate_limit.kind == LimitKind.WINDOW


def test_a_wait_longer_than_the_budget_is_deferred_instead_of_slept():
    err = rl.RateLimitedError("x", rl.classify_tiktok("rate_limit_exceeded", headers={"Retry-After": "120"}))
    fn, sleeps = _Flaky(err), _Sleeps()
    with pytest.raises(RateLimitedError):
        _retry(fn, sleeps, budget_s=30)
    assert fn.calls == 1 and sleeps.waits == []


def test_attempts_run_out():
    fn, sleeps = _Flaky(*[_CodeError("x", "rate_limit_exceeded")] * 10), _Sleeps()
    with pytest.raises(RateLimitedError):
        _retry(fn, sleeps, max_attempts=3, budget_s=600)
    assert fn.calls == 3


def test_errors_that_are_not_limits_pass_straight_through_untouched():
    boom = ValueError("Invalid image")
    fn, sleeps = _Flaky(boom), _Sleeps()
    with pytest.raises(ValueError) as exc:
        _retry(fn, sleeps)
    assert exc.value is boom and fn.calls == 1


def test_on_retry_is_told_about_each_wait():
    seen = []

    async def on_retry(attempt, wait, info):
        seen.append((attempt, wait, info.code))

    fn, sleeps = _Flaky(_CodeError("x", "rate_limit_exceeded")), _Sleeps()
    _retry(fn, sleeps, budget_s=60, on_retry=on_retry)
    assert seen == [(1, 2.0, "rate_limit_exceeded")]


def test_settings_read_environment_overrides(monkeypatch):
    monkeypatch.setenv("TIKTOK_MIN_GAP_S", "30")
    monkeypatch.setenv("PUBLISH_IMMEDIATE_WAIT_MAX_S", "not a number")
    s = rl.get_settings()
    assert s.tiktok_min_gap_s == 30.0 and s.immediate_wait_max_s == 20.0
