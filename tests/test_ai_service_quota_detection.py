"""
AIService's OpenAI error classification (app/services/AIService.py).

Root cause confirmed live (Oct 2026): the OpenAI account ran out of
credit, but every log said "rate limit," never "out of credit" — because
OpenAI raises the exact same RateLimitError/429 for both a real rate limit
and a zero-balance account, and the generic 429 check was written BEFORE
the specific insufficient_quota check, so the specific branch was
unreachable dead code. These tests pin the fix: insufficient_quota must be
detected even though the exception also looks exactly like a generic
RateLimitError, and detecting it must raise the platform alert
(app/services/platform_alerts.py) so an admin actually finds out.

Uses an in-memory MongoDB for the alert side; the raw openai client is
stubbed so no network call happens.
"""
import asyncio
from datetime import datetime

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.domain.models.chat_model import ChatModel, ChatMessage
from app.services import AIService as ai_service_module
from app.services.AIService import AIService


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRateLimitError(Exception):
    """Named RateLimitError on purpose: real OpenAI insufficient_quota
    failures raise this exact class — the whole bug was that the code
    could not tell it apart from a plain rate limit by class alone."""


_FakeRateLimitError.__name__ = "RateLimitError"
_FakeRateLimitError.__qualname__ = "RateLimitError"


def _request():
    return ChatModel(model="gpt-4o-mini", messages=[ChatMessage(role="user", content="hi")], temperature=0)


def _raise(exc):
    def _inner(**kwargs):
        raise exc
    return _inner


def test_insufficient_quota_is_detected_even_though_it_is_also_a_rate_limit_error(monkeypatch):
    exc = _FakeRateLimitError(
        "Error code: 429 - {'error': {'message': 'You exceeded your current quota', "
        "'type': 'insufficient_quota', 'code': 'insufficient_quota'}}"
    )
    monkeypatch.setattr(ai_service_module.client.chat.completions, "create", _raise(exc))

    result = run(AIService.chat_completion(_request()))
    assert result == {"error": "AI service is temporarily unavailable due to quota limits. Please contact support."}


def test_a_plain_rate_limit_with_no_quota_wording_still_gets_the_generic_message(monkeypatch):
    exc = _FakeRateLimitError("Error code: 429 - {'error': {'message': 'Rate limit reached', 'type': 'requests'}}")
    monkeypatch.setattr(ai_service_module.client.chat.completions, "create", _raise(exc))

    result = run(AIService.chat_completion(_request()))
    assert result == {"error": "AI service is temporarily unavailable. Please try again later or contact support."}


def test_insufficient_quota_raises_the_platform_alert(monkeypatch):
    captured = {}

    async def fake_raise_alert(db, kind, title, detail, now=None):
        captured.update(kind=kind, title=title, detail=detail)

    from app.services import platform_alerts
    import app.database as database_module
    monkeypatch.setattr(platform_alerts, "raise_alert", fake_raise_alert)
    monkeypatch.setattr(database_module, "get_db", lambda: object())

    exc = _FakeRateLimitError("insufficient_quota: account balance is negative")
    monkeypatch.setattr(ai_service_module.client.chat.completions, "create", _raise(exc))

    run(AIService.chat_completion(_request()))

    assert captured["kind"] == "openai_insufficient_quota"
    assert "out of credit" in captured["title"].lower()
    assert "Content Calendar" in captured["detail"] and "Social Listening" in captured["detail"]


def test_the_alert_detail_shows_openai_s_plain_sentence_not_the_raw_dict(monkeypatch):
    """Live-found: the admin dashboard/email used to show the raw Python
    dict repr of the error inline — technical, ugly, not user-friendly.
    The detail text must carry OpenAI's own human sentence instead."""
    captured = {}

    async def fake_raise_alert(db, kind, title, detail, now=None):
        captured.update(detail=detail)

    from app.services import platform_alerts
    import app.database as database_module
    monkeypatch.setattr(platform_alerts, "raise_alert", fake_raise_alert)
    monkeypatch.setattr(database_module, "get_db", lambda: object())

    real_shaped_error = (
        "Error code: 429 - {'error': {'message': 'You have no credits remaining. Add credits to "
        "continue using the API at https://platform.openai.com/settings/organization/billing/.', "
        "'type': 'insufficient_quota', 'param': None, 'code': 'credit_balance_exhausted'}}"
    )
    exc = _FakeRateLimitError(real_shaped_error)
    monkeypatch.setattr(ai_service_module.client.chat.completions, "create", _raise(exc))

    run(AIService.chat_completion(_request()))

    assert "You have no credits remaining" in captured["detail"]
    assert "{'error'" not in captured["detail"]  # the raw dict repr is gone
    assert "credit_balance_exhausted" not in captured["detail"]


def test_friendly_message_falls_back_to_the_raw_text_when_unparseable():
    assert ai_service_module._friendly_openai_message("totally unexpected error shape") == (
        "totally unexpected error shape"
    )


def test_a_plain_rate_limit_never_raises_the_platform_alert(monkeypatch):
    called = {"n": 0}

    async def fake_raise_alert(*a, **kw):
        called["n"] += 1

    from app.services import platform_alerts
    monkeypatch.setattr(platform_alerts, "raise_alert", fake_raise_alert)

    exc = _FakeRateLimitError("Error code: 429 - rate limit reached for requests")
    monkeypatch.setattr(ai_service_module.client.chat.completions, "create", _raise(exc))

    run(AIService.chat_completion(_request()))
    assert called["n"] == 0


def test_a_broken_alert_path_never_breaks_the_actual_ai_error_response(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db unreachable")

    from app.services import platform_alerts
    monkeypatch.setattr(platform_alerts, "raise_alert", boom)

    exc = _FakeRateLimitError("insufficient_quota: account balance is negative")
    monkeypatch.setattr(ai_service_module.client.chat.completions, "create", _raise(exc))

    result = run(AIService.chat_completion(_request()))  # must not raise
    assert result == {"error": "AI service is temporarily unavailable due to quota limits. Please contact support."}


def test_structured_chat_completion_has_the_same_fix(monkeypatch):
    exc = _FakeRateLimitError("insufficient_quota: account balance is negative")
    monkeypatch.setattr(ai_service_module.client.beta.chat.completions, "parse", _raise(exc))

    result = run(AIService.structured_chat_completion(_request()))
    assert result == {"error": "AI service is temporarily unavailable due to quota limits. Please contact support."}
