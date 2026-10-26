"""
Platform-wide operational alerts (app/services/platform_alerts.py) — for
infrastructure/account failures like the OpenAI account running out of
credit, surfaced on the Admin dashboard and emailed to
settings.PLATFORM_ALERT_EMAILS, deliberately not routed through Sentry
(nobody is actively watching it). Uses an in-memory MongoDB; EmailService
is stubbed so no real SMTP connection happens.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.services import platform_alerts


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["platform_alerts_test"]


def _email_spy(monkeypatch):
    calls = []

    async def fake_send(to_email, subject, template_name, template_vars):
        calls.append({"to": to_email, "subject": subject, "template": template_name, "vars": template_vars})
        return True

    from app.services.EmailService import email_service
    monkeypatch.setattr(email_service, "send_email", fake_send)
    return calls


def test_first_occurrence_creates_the_alert_and_emails_every_recipient(monkeypatch):
    db = fresh_db()
    calls = _email_spy(monkeypatch)

    run(platform_alerts.raise_alert(db, "openai_insufficient_quota", "OpenAI is out of credit", "Everything's down."))

    doc = run(db["platform_alerts"].find_one({"kind": "openai_insufficient_quota"}))
    assert doc["occurrences"] == 1
    assert doc["dismissed_at"] is None
    assert doc["first_seen"] == doc["last_seen"]

    from app.core.config import settings
    expected = [e.strip() for e in settings.PLATFORM_ALERT_EMAILS.split(",") if e.strip()]
    assert {c["to"] for c in calls} == set(expected)
    assert "hello@urisocial.com" in expected and "urifusion@gmail.com" in expected
    assert all(c["template"] == "platform_alert" for c in calls)
    assert calls[0]["vars"]["occurrences"] == 1
    assert calls[0]["vars"]["title"] == "OpenAI is out of credit"


def test_repeated_occurrences_within_the_cooldown_do_not_re_email(monkeypatch):
    db = fresh_db()
    calls = _email_spy(monkeypatch)

    run(platform_alerts.raise_alert(db, "openai_insufficient_quota", "t", "d"))
    run(platform_alerts.raise_alert(db, "openai_insufficient_quota", "t", "d"))
    run(platform_alerts.raise_alert(db, "openai_insufficient_quota", "t", "d"))

    doc = run(db["platform_alerts"].find_one({"kind": "openai_insufficient_quota"}))
    assert doc["occurrences"] == 3  # every occurrence is still counted
    n_recipients = len([e for e in (run(_settings_emails())) if e])
    assert len(calls) == n_recipients  # but only the first occurrence emailed


async def _settings_emails():
    from app.core.config import settings
    return [e.strip() for e in settings.PLATFORM_ALERT_EMAILS.split(",")]


def test_re_emails_after_the_cooldown_window_passes(monkeypatch):
    db = fresh_db()
    calls = _email_spy(monkeypatch)
    now = datetime(2026, 10, 26, 9, 0)

    run(db["platform_alerts"].insert_one({
        "kind": "openai_insufficient_quota", "title": "t", "detail": "d",
        "first_seen": now - timedelta(hours=3), "last_seen": now - timedelta(hours=2),
        "occurrences": 10, "last_emailed_at": now - timedelta(hours=2), "dismissed_at": None,
    }))

    run(platform_alerts.raise_alert(db, "openai_insufficient_quota", "t", "d", now=now))  # 2h later, past the 60min cooldown
    assert len(calls) > 0


def test_dismissing_then_recurring_emails_again(monkeypatch):
    db = fresh_db()
    calls = _email_spy(monkeypatch)
    run(platform_alerts.raise_alert(db, "openai_insufficient_quota", "t", "d"))
    assert len(calls) > 0
    calls.clear()

    run(platform_alerts.dismiss_alert(db, "openai_insufficient_quota"))
    doc = run(db["platform_alerts"].find_one({"kind": "openai_insufficient_quota"}))
    assert doc["dismissed_at"] is not None

    # the same failure happens again moments later, well inside the cooldown —
    # but since it was marked fixed, the admin needs to hear about it again
    run(platform_alerts.raise_alert(db, "openai_insufficient_quota", "t", "d"))
    assert len(calls) > 0
    doc = run(db["platform_alerts"].find_one({"kind": "openai_insufficient_quota"}))
    assert doc["dismissed_at"] is None  # un-dismissed


def test_dismissing_an_alert_that_does_not_exist_returns_none():
    db = fresh_db()
    assert run(platform_alerts.dismiss_alert(db, "no_such_kind")) is None


def test_list_alerts_excludes_dismissed_by_default(monkeypatch):
    db = fresh_db()
    _email_spy(monkeypatch)
    run(platform_alerts.raise_alert(db, "kind_a", "A", "a"))
    run(platform_alerts.raise_alert(db, "kind_b", "B", "b"))
    run(platform_alerts.dismiss_alert(db, "kind_b"))

    active = run(platform_alerts.list_alerts(db))
    assert {a["kind"] for a in active} == {"kind_a"}

    everything = run(platform_alerts.list_alerts(db, include_dismissed=True))
    assert {a["kind"] for a in everything} == {"kind_a", "kind_b"}


def test_a_broken_email_send_does_not_block_recording_the_alert(monkeypatch):
    db = fresh_db()

    async def boom(to_email, subject, template_name, template_vars):
        raise RuntimeError("smtp down")
    from app.services.EmailService import email_service
    monkeypatch.setattr(email_service, "send_email", boom)

    run(platform_alerts.raise_alert(db, "openai_insufficient_quota", "t", "d"))  # must not raise
    doc = run(db["platform_alerts"].find_one({"kind": "openai_insufficient_quota"}))
    assert doc["occurrences"] == 1
