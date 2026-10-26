"""
Admin router endpoints for platform alerts (GET/POST /admin/alerts...) —
the Admin dashboard's view into app/services/platform_alerts.py. Calls the
router functions directly (same convention as tests/test_listening_curation.py),
not through a full HTTP client. Uses an in-memory MongoDB.
"""
import asyncio

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from fastapi import HTTPException

from app.routers.admin_router import dismiss_platform_alert, list_platform_alerts
from app.services import platform_alerts

ADMIN = {"claims": {"email": "admin@urisocial.com"}}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["admin_alerts_test"]


def test_list_returns_active_alerts_only_by_default(monkeypatch):
    db = fresh_db()

    async def noop_email(*a, **kw):
        return None
    monkeypatch.setattr(platform_alerts, "_email_admins", noop_email)

    run(platform_alerts.raise_alert(db, "a", "A", "a-detail"))
    run(platform_alerts.raise_alert(db, "b", "B", "b-detail"))
    run(platform_alerts.dismiss_alert(db, "b"))

    result = run(list_platform_alerts(include_dismissed=False, admin_user=ADMIN, db=db))
    assert {a["kind"] for a in result["alerts"]} == {"a"}


def test_list_with_include_dismissed_shows_everything(monkeypatch):
    db = fresh_db()

    async def noop_email(*a, **kw):
        return None
    monkeypatch.setattr(platform_alerts, "_email_admins", noop_email)

    run(platform_alerts.raise_alert(db, "a", "A", "a-detail"))
    run(platform_alerts.dismiss_alert(db, "a"))

    result = run(list_platform_alerts(include_dismissed=True, admin_user=ADMIN, db=db))
    assert len(result["alerts"]) == 1
    assert result["alerts"][0]["dismissed_at"] is not None


def test_dismiss_marks_it_resolved(monkeypatch):
    db = fresh_db()

    async def noop_email(*a, **kw):
        return None
    monkeypatch.setattr(platform_alerts, "_email_admins", noop_email)

    run(platform_alerts.raise_alert(db, "openai_insufficient_quota", "OpenAI is out of credit", "detail"))
    result = run(dismiss_platform_alert("openai_insufficient_quota", admin_user=ADMIN, db=db))
    assert result["alert"]["dismissed_at"] is not None

    still_active = run(list_platform_alerts(include_dismissed=False, admin_user=ADMIN, db=db))
    assert still_active["alerts"] == []


def test_dismissing_a_nonexistent_alert_is_404():
    db = fresh_db()
    with pytest.raises(HTTPException) as exc:
        run(dismiss_platform_alert("no_such_kind", admin_user=ADMIN, db=db))
    assert exc.value.status_code == 404
