"""Security audit 2026-11-23, high findings 4 (brute-forceable codes) and 7 (job IDOR)."""
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.core.one_time_codes import MAX_ATTEMPTS, check_one_time_code, new_code
from tests.test_jane_ads_growth import mongomock_motor  # noqa: F401


def run(coro):
    return asyncio.run(coro)


def test_codes_are_six_digits_from_a_csprng():
    codes = {new_code() for _ in range(200)}
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(codes) > 150


def _user_db(code="123456", minutes=15):
    db = mongomock_motor.AsyncMongoMockClient()["t"]
    run(db.users.insert_one({"_id": "u1", "email": "a@b.co", "password_reset_code": code,
                             "password_reset_code_expires": datetime.utcnow() + timedelta(minutes=minutes)}))
    return db


def _check(db, provided):
    user = run(db.users.find_one({"_id": "u1"}))
    return run(check_one_time_code(db, user, provided, "password_reset_code",
                                   "password_reset_code_expires", "password_reset_attempts", "reset"))


def test_the_right_code_passes():
    db = _user_db()
    _check(db, "123456")


def test_the_code_is_destroyed_after_too_many_wrong_guesses():
    db = _user_db()
    for _ in range(MAX_ATTEMPTS - 1):
        with pytest.raises(HTTPException):
            _check(db, "000000")
    with pytest.raises(HTTPException) as e:
        _check(db, "000000")
    assert "Too many" in e.value.detail
    # Even the right code no longer works: the attacker's budget is spent, and so is the code.
    with pytest.raises(HTTPException):
        _check(db, "123456")


def test_an_expired_code_is_refused_even_when_correct():
    db = _user_db(minutes=-1)
    with pytest.raises(HTTPException) as e:
        _check(db, "123456")
    assert "expired" in e.value.detail


def test_no_stored_code_means_no_match():
    db = _user_db(code="")
    with pytest.raises(HTTPException):
        _check(db, "")


# ── job ownership (finding 7) ────────────────────────────────────────────────

def test_a_job_is_only_returned_to_its_owner():
    from app.agents.social_media_manager.routers.complete_social_manager import _own_job

    job = {"job_id": "j1", "user_id": "alice"}
    assert _own_job(job, "alice") is job
    assert _own_job(job, "mallory") is None
    assert _own_job(job, "") is None
    assert _own_job(None, "alice") is None


def test_jobs_from_before_owners_were_recorded_stay_readable_only_where_allowed():
    from app.agents.social_media_manager.routers.complete_social_manager import _own_job

    legacy = {"job_id": "j1"}
    assert _own_job(legacy, "alice", legacy_ok=True) is legacy
    assert _own_job(legacy, "alice") is None


# ── API hardening (findings 9 and 12) ────────────────────────────────────────

def test_api_responses_carry_baseline_security_headers():
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).get("/__no_such_route__")
    assert r.headers.get("strict-transport-security", "").startswith("max-age=")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"


def test_api_docs_are_switched_off_in_production(monkeypatch):
    import importlib

    from app.core.config import settings

    monkeypatch.setattr(settings, "ENV", "Production")
    import app.main as main
    prod_app = importlib.reload(main).app
    try:
        assert prod_app.docs_url is None and prod_app.openapi_url is None
    finally:
        monkeypatch.setattr(settings, "ENV", "Development")
        importlib.reload(main)
