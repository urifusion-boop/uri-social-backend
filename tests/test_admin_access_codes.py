"""
Admin access-code management tests (create/list/revoke, and per-code
redemption listing) — app/routers/admin_router.py.

Redemption itself (the user-facing side) is covered separately in
tests/test_access_code_redeem.py; this file only covers the admin side:
generating a code, listing codes, and inspecting who redeemed one.
"""
import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.domain.models.billing_models import CreateAccessCodeRequest, UpdateAccessCodeRequest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    async def find_one(self, query, projection=None, sort=None):
        matches = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return dict(matches[0]) if matches else None

    def find(self, query=None, projection=None):
        query = query or {}
        matches = [dict(d) for d in self.docs if all(d.get(k) == v for k, v in query.items())]

        class _Cursor:
            def __aiter__(self):
                return self._gen()

            async def _gen(self):
                for d in matches:
                    yield d

            def sort(self, *a, **kw):
                return self

        return _Cursor()

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                for k, v in (update.get("$inc") or {}).items():
                    d[k] = d.get(k, 0) + v
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


def test_create_access_code_with_explicit_code():
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    body = CreateAccessCodeRequest(code="asa26", plan_tier_id="starter", duration_days=60, label="ASA partnership")
    result = _run(create_access_code(body, admin_user=_admin(), db=db))
    assert result["code"] == "ASA26"  # normalized uppercase
    assert result["duration_days"] == 60
    assert result["redemption_count"] == 0
    assert result["is_active"] is True
    assert result["created_by"] == "admin@urisocial.com"


def test_create_access_code_auto_generates_when_no_code_given():
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    body = CreateAccessCodeRequest(plan_tier_id="starter", duration_days=30)
    result = _run(create_access_code(body, admin_user=_admin(), db=db))
    assert result["code"]  # non-empty, auto-generated
    assert len(result["code"]) == 8


def test_create_access_code_rejects_duplicate():
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    body = CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60)
    _run(create_access_code(body, admin_user=_admin(), db=db))

    with pytest.raises(HTTPException) as exc_info:
        _run(create_access_code(body, admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 409


def test_create_access_code_rejects_unknown_plan():
    from app.routers.admin_router import create_access_code

    db = FakeDb()  # no subscription_tiers at all
    body = CreateAccessCodeRequest(code="ASA26", plan_tier_id="nonexistent", duration_days=60)
    with pytest.raises(HTTPException) as exc_info:
        _run(create_access_code(body, admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 404


def test_list_access_codes_returns_all():
    from app.routers.admin_router import create_access_code, list_access_codes

    db = _db_with_starter_tier()
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))
    _run(create_access_code(CreateAccessCodeRequest(code="OTHER1", plan_tier_id="starter", duration_days=30), admin_user=_admin(), db=db))

    result = _run(list_access_codes(admin_user=_admin(), db=db))
    assert result["count"] == 2
    assert {c["code"] for c in result["codes"]} == {"ASA26", "OTHER1"}


def test_update_access_code_can_revoke(monkeypatch):
    from app.routers.admin_router import create_access_code, update_access_code
    from app.services.CreditService import credit_service

    db = _db_with_starter_tier()
    monkeypatch.setattr(credit_service, "_db", db)  # revoke path claws back active redeemers via credit_service
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))

    updated = _run(update_access_code("ASA26", UpdateAccessCodeRequest(is_active=False), admin_user=_admin(), db=db))
    assert updated["is_active"] is False
    assert updated["revoked_active_users"] == 0  # nobody had redeemed it yet


def test_update_access_code_unknown_code_404():
    from app.routers.admin_router import update_access_code

    db = FakeDb()
    with pytest.raises(HTTPException) as exc_info:
        _run(update_access_code("NOTREAL", UpdateAccessCodeRequest(is_active=False), admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 404


def test_update_access_code_requires_at_least_one_field():
    from app.routers.admin_router import update_access_code

    db = _db_with_starter_tier()
    with pytest.raises(HTTPException) as exc_info:
        _run(update_access_code("ASA26", UpdateAccessCodeRequest(), admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 400


def test_list_redemptions_joins_email():
    from app.routers.admin_router import list_access_code_redemptions

    db = FakeDb({
        "access_code_redemptions": [
            {"code": "ASA26", "user_id": "u1", "plan_tier_id": "starter",
             "access_start": datetime.utcnow(), "access_end": datetime.utcnow(),
             "previous_subscription_tier": None, "redeemed_at": datetime.utcnow()},
        ],
        "users": [{"userId": "u1", "email": "partner@example.com"}],
    })
    result = _run(list_access_code_redemptions("asa26", admin_user=_admin(), db=db))
    assert result["count"] == 1
    assert result["redemptions"][0]["email"] == "partner@example.com"


# ── Assigned (personal invite) codes ────────────────────────────────────────

def test_create_with_assigned_email_is_visible_immediately():
    """The whole point of this mode: the admin sees who a code is for
    BEFORE that person ever redeems it, not just discoverable afterward."""
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    db["users"].docs.append({"email": "partner@example.com", "first_name": "Ada", "last_name": "Obi"})
    body = CreateAccessCodeRequest(
        code="ASA-ADA", plan_tier_id="starter", duration_days=60, assigned_to_email="Partner@Example.com",
    )
    result = _run(create_access_code(body, admin_user=_admin(), db=db))
    assert result["assigned_to_email"] == "partner@example.com"  # normalized lowercase
    assert result["assigned_to_name"] == "Ada Obi"
    assert result["status"] == "pending"


def test_create_assigned_to_email_with_no_account_yet_shows_email_only():
    """A real invite use case: the person hasn't signed up yet. Should not
    error — just no name to resolve."""
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    body = CreateAccessCodeRequest(
        code="ASA-NEW", plan_tier_id="starter", duration_days=60, assigned_to_email="future-partner@example.com",
    )
    result = _run(create_access_code(body, admin_user=_admin(), db=db))
    assert result["assigned_to_email"] == "future-partner@example.com"
    assert result["assigned_to_name"] is None
    assert result["status"] == "pending"


def test_create_without_assigned_email_is_unassigned():
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    body = CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60)
    result = _run(create_access_code(body, admin_user=_admin(), db=db))
    assert result["assigned_to_email"] is None
    assert result["status"] == "unassigned"


def test_list_shows_redeemed_status_once_someone_has_redeemed():
    from app.routers.admin_router import create_access_code, list_access_codes

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60, assigned_to_email="p@example.com"),
        admin_user=_admin(), db=db,
    ))
    db["access_codes"].docs[0]["redemption_count"] = 1  # simulate a completed redemption

    result = _run(list_access_codes(admin_user=_admin(), db=db))
    assert result["codes"][0]["status"] == "redeemed"


def test_update_can_reassign_to_a_different_email():
    from app.routers.admin_router import create_access_code, update_access_code

    db = _db_with_starter_tier()
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))

    updated = _run(update_access_code(
        "ASA26", UpdateAccessCodeRequest(assigned_to_email="new-partner@example.com"), admin_user=_admin(), db=db,
    ))
    assert updated["assigned_to_email"] == "new-partner@example.com"
    assert updated["status"] == "pending"


def test_update_can_clear_an_assignment():
    """Passing an empty string clears it — distinct from omitting the field
    entirely, which leaves the existing assignment untouched."""
    from app.routers.admin_router import create_access_code, update_access_code

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60, assigned_to_email="p@example.com"),
        admin_user=_admin(), db=db,
    ))

    updated = _run(update_access_code("ASA26", UpdateAccessCodeRequest(assigned_to_email=""), admin_user=_admin(), db=db))
    assert updated["assigned_to_email"] is None
    assert updated["status"] == "unassigned"


# ── Revoking a code claws back anyone currently redeeming it ────────────────
# A deliberate revoke means "stop this now" — unlike a code just lapsing on
# its own (end_date passing, or a redeemer's credits running out), which only
# ever affects future redemptions, not people already granted access.

def test_revoke_immediately_cuts_off_an_active_redeemer(monkeypatch):
    from app.routers.admin_router import create_access_code, update_access_code
    from app.services.CreditService import credit_service

    db = _db_with_starter_tier()
    monkeypatch.setattr(credit_service, "_db", db)
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))

    redeemed_at = datetime.utcnow()
    db["user_credits"].docs.append({
        "user_id": "user-1", "subscription_tier": "starter", "subscription_source": "access_code",
        "subscription_credits": 15, "bonus_credits": 0, "credits_used": 5,
        "total_credits": 15, "credits_remaining": 15, "end_date": redeemed_at,
    })
    db["access_code_redemptions"].docs.append({
        "code": "ASA26", "user_id": "user-1", "plan_tier_id": "starter",
        "redeemed_at": redeemed_at, "revoked_at": None, "revocation_reason": None,
    })

    updated = _run(update_access_code("ASA26", UpdateAccessCodeRequest(is_active=False), admin_user=_admin(), db=db))
    assert updated["is_active"] is False
    assert updated["revoked_active_users"] == 1

    wallet = db["user_credits"].docs[0]
    assert wallet["subscription_tier"] is None
    assert wallet["subscription_source"] is None

    redemption = db["access_code_redemptions"].docs[0]
    assert redemption["revoked_at"] is not None
    assert redemption["revocation_reason"] == "admin_revoked"


def test_revoke_does_not_touch_a_different_code_or_user(monkeypatch):
    from app.routers.admin_router import create_access_code, update_access_code
    from app.services.CreditService import credit_service

    db = _db_with_starter_tier()
    monkeypatch.setattr(credit_service, "_db", db)
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))
    _run(create_access_code(CreateAccessCodeRequest(code="OTHER1", plan_tier_id="starter", duration_days=30), admin_user=_admin(), db=db))

    redeemed_at = datetime.utcnow()
    db["user_credits"].docs.append({
        "user_id": "user-2", "subscription_tier": "starter", "subscription_source": "access_code",
        "subscription_credits": 20, "bonus_credits": 0, "credits_used": 0,
        "total_credits": 20, "credits_remaining": 20, "end_date": redeemed_at,
    })
    db["access_code_redemptions"].docs.append({
        "code": "OTHER1", "user_id": "user-2", "plan_tier_id": "starter",
        "redeemed_at": redeemed_at, "revoked_at": None, "revocation_reason": None,
    })

    updated = _run(update_access_code("ASA26", UpdateAccessCodeRequest(is_active=False), admin_user=_admin(), db=db))
    assert updated["revoked_active_users"] == 0

    # user-2's OTHER1 grant is untouched — ASA26 being revoked is unrelated.
    wallet = db["user_credits"].docs[0]
    assert wallet["subscription_tier"] == "starter"
    assert db["access_code_redemptions"].docs[0]["revoked_at"] is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
