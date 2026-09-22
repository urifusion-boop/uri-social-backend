"""
Emailing an assigned access code to its recipients — app/routers/admin_router.py.

An ASSIGNED code (assigned_emails set) is reserved for a specific roster of
people; this covers actually getting the code INTO their inboxes, either
automatically on creation (send_email=True, the default) or on-demand via
the resend endpoint — either to the whole roster at once or to one person —
rather than requiring the admin to copy/paste it into some other channel by
hand.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.domain.models.billing_models import CreateAccessCodeRequest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _flush():
    """Let any asyncio.ensure_future(...)-scheduled task run to completion."""
    _run(asyncio.sleep(0))
    _run(asyncio.sleep(0))


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    async def find_one(self, query, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return type("Result", (), {"matched_count": 1})()
        if upsert:
            self.docs.append({**query, **update.get("$set", {})})
        return type("Result", (), {"matched_count": 0})()


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._colls = {name: FakeCollection(docs) for name, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def _admin():
    return {"claims": {"email": "admin@urisocial.com"}}


def _db_with_starter_tier():
    return FakeDb({"subscription_tiers": [{"tier_id": "starter", "name": "Starter Plan", "credits_monthly": 20}]})


@pytest.fixture
def fake_send(monkeypatch):
    calls = []

    async def _fake(to_email, subject, template_name, template_vars):
        calls.append(
            {"to_email": to_email, "subject": subject, "template_name": template_name, "template_vars": template_vars}
        )
        return True

    import app.routers.admin_router as admin_router

    monkeypatch.setattr(admin_router.email_service, "send_email", _fake)
    return calls


def test_creating_an_assigned_code_emails_every_roster_member_by_default(fake_send):
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    body = CreateAccessCodeRequest(
        code="ASA26", plan_tier_id="starter", duration_days=60,
        assigned_emails=["Partner@Example.com", "second@example.com"], label="Africa SME Assembly",
    )
    result = _run(create_access_code(body, admin_user=_admin(), db=db))
    _flush()

    assert result["emails_sent"] == 2
    assert len(fake_send) == 2
    sent_to = {c["to_email"] for c in fake_send}
    assert sent_to == {"partner@example.com", "second@example.com"}  # normalized lowercase
    assert fake_send[0]["template_name"] == "coupon_code"
    assert fake_send[0]["template_vars"]["code"] == "ASA26"
    assert fake_send[0]["template_vars"]["plan_name"] == "Starter Plan"
    assert fake_send[0]["template_vars"]["duration_days"] == 60


def test_send_email_false_skips_the_email(fake_send):
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    body = CreateAccessCodeRequest(
        code="ASA26", plan_tier_id="starter", duration_days=60,
        assigned_emails=["partner@example.com"], send_email=False,
    )
    result = _run(create_access_code(body, admin_user=_admin(), db=db))
    _flush()

    assert result["emails_sent"] == 0
    assert len(fake_send) == 0


def test_unassigned_code_is_never_emailed(fake_send):
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    body = CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60)
    result = _run(create_access_code(body, admin_user=_admin(), db=db))
    _flush()

    assert result["emails_sent"] == 0
    assert len(fake_send) == 0


def test_resend_endpoint_emails_the_whole_roster_by_default(fake_send):
    from app.routers.admin_router import create_access_code, send_access_code_email

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(
            code="ASA26", plan_tier_id="starter", duration_days=60,
            assigned_emails=["partner@example.com", "second@example.com"], send_email=False,
        ),
        admin_user=_admin(), db=db,
    ))
    _flush()
    fake_send.clear()  # creation itself didn't send (send_email=False); resend should

    result = _run(send_access_code_email("asa26", email=None, admin_user=_admin(), db=db))
    _flush()

    assert result["sent"] is True
    assert set(result["to"]) == {"partner@example.com", "second@example.com"}
    assert len(fake_send) == 2


def test_resend_can_target_just_one_roster_member(fake_send):
    from app.routers.admin_router import create_access_code, send_access_code_email

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(
            code="ASA26", plan_tier_id="starter", duration_days=60,
            assigned_emails=["partner@example.com", "second@example.com"], send_email=False,
        ),
        admin_user=_admin(), db=db,
    ))
    _flush()
    fake_send.clear()

    result = _run(send_access_code_email("asa26", email="Second@Example.com", admin_user=_admin(), db=db))
    _flush()

    assert result == {"sent": True, "to": ["second@example.com"]}
    assert len(fake_send) == 1
    assert fake_send[0]["to_email"] == "second@example.com"


def test_resend_rejects_an_email_not_on_the_roster(fake_send):
    from app.routers.admin_router import create_access_code, send_access_code_email

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(
            code="ASA26", plan_tier_id="starter", duration_days=60, assigned_emails=["partner@example.com"],
        ),
        admin_user=_admin(), db=db,
    ))
    _flush()

    with pytest.raises(HTTPException) as exc_info:
        _run(send_access_code_email("ASA26", email="stranger@example.com", admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 400


def test_resend_rejects_an_unassigned_code(fake_send):
    from app.routers.admin_router import create_access_code, send_access_code_email

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60),
        admin_user=_admin(), db=db,
    ))

    with pytest.raises(HTTPException) as exc_info:
        _run(send_access_code_email("ASA26", admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 400


def test_resend_rejects_an_unknown_code(fake_send):
    from app.routers.admin_router import send_access_code_email

    db = _db_with_starter_tier()
    with pytest.raises(HTTPException) as exc_info:
        _run(send_access_code_email("NOSUCHCODE", admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 404


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
