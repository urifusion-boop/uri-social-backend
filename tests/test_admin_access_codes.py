"""
Admin access-code management tests (create/list/revoke, and per-code
redemption listing) — app/routers/admin_router.py.

Redemption itself (the user-facing side) is covered separately in
tests/test_access_code_redeem.py; this file only covers the admin side:
generating a code, listing codes, and inspecting who redeemed one.
"""
import asyncio
from datetime import datetime, timedelta

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

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                del self.docs[i]
                return type("Result", (), {"deleted_count": 1})()
        return type("Result", (), {"deleted_count": 0})()


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


# ── effective_status: the code's own is_active and a redemption's revoked_at
# are two DIFFERENT things, and displaying either one alone (or naively
# assuming they always agree) is what produced the "shows Active and
# Revoked at the same time" confusion — effective_status is the one field
# that tells the truth about a SPECIFIC redemption regardless of what else
# may have happened to the user's wallet since.

def _redemption(**overrides):
    now = datetime.utcnow()
    base = {
        "code": "ASA26", "user_id": "u1", "plan_tier_id": "starter",
        "access_start": now, "access_end": now + timedelta(days=60),
        "previous_subscription_tier": None, "redeemed_at": now,
        "revoked_at": None, "revocation_reason": None,
    }
    base.update(overrides)
    return base


def test_effective_status_active_when_still_the_current_grant():
    from app.routers.admin_router import list_access_code_redemptions

    db = FakeDb({
        "access_code_redemptions": [_redemption()],
        "user_credits": [{"user_id": "u1", "subscription_tier": "starter", "subscription_source": "access_code"}],
    })
    result = _run(list_access_code_redemptions("ASA26", admin_user=_admin(), db=db))
    assert result["redemptions"][0]["effective_status"] == "active"


def test_effective_status_revoked_takes_priority():
    from app.routers.admin_router import list_access_code_redemptions

    db = FakeDb({
        "access_code_redemptions": [_redemption(revoked_at=datetime.utcnow(), revocation_reason="admin_revoked")],
        "user_credits": [{"user_id": "u1", "subscription_tier": "starter", "subscription_source": "access_code"}],
    })
    result = _run(list_access_code_redemptions("ASA26", admin_user=_admin(), db=db))
    assert result["redemptions"][0]["effective_status"] == "revoked"


def test_effective_status_lapsed_when_access_end_passed():
    from app.routers.admin_router import list_access_code_redemptions

    db = FakeDb({
        "access_code_redemptions": [_redemption(access_end=datetime.utcnow() - timedelta(days=1))],
        "user_credits": [{"user_id": "u1", "subscription_tier": "starter", "subscription_source": "access_code"}],
    })
    result = _run(list_access_code_redemptions("ASA26", admin_user=_admin(), db=db))
    assert result["redemptions"][0]["effective_status"] == "lapsed"


def test_effective_status_superseded_when_wallet_moved_on_without_a_tracked_revoke():
    """The exact bug report: this redemption was never marked revoked_at and
    hasn't lapsed by date, but the wallet now reflects a DIFFERENT grant
    (e.g. a later code redeemed before the no-stacking guard existed, or a
    real subscription taken out some other way) — must not read as "Active"."""
    from app.routers.admin_router import list_access_code_redemptions

    db = FakeDb({
        "access_code_redemptions": [_redemption()],  # plan_tier_id="starter", not revoked, not lapsed
        "user_credits": [{"user_id": "u1", "subscription_tier": "pro", "subscription_source": "access_code"}],
    })
    result = _run(list_access_code_redemptions("ASA26", admin_user=_admin(), db=db))
    assert result["redemptions"][0]["effective_status"] == "superseded"


def test_effective_status_superseded_when_no_wallet_at_all():
    from app.routers.admin_router import list_access_code_redemptions

    db = FakeDb({"access_code_redemptions": [_redemption()]})  # no user_credits doc for u1
    result = _run(list_access_code_redemptions("ASA26", admin_user=_admin(), db=db))
    assert result["redemptions"][0]["effective_status"] == "superseded"


# ── Assigned (personal invite roster) codes ─────────────────────────────────

def test_create_with_assigned_emails_is_visible_immediately():
    """The whole point of this mode: the admin sees the whole invite roster
    BEFORE anyone on it redeems, not just discoverable afterward."""
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    body = CreateAccessCodeRequest(
        code="ASA-ADA", plan_tier_id="starter", duration_days=60,
        assigned_emails=["Partner@Example.com", "second@example.com", "partner@example.com"],  # dup, different case
    )
    result = _run(create_access_code(body, admin_user=_admin(), db=db))
    assert result["assigned_emails"] == ["partner@example.com", "second@example.com"]  # normalized, deduped
    assert result["assigned_count"] == 2
    assert result["status"] == "pending"


def test_create_without_assigned_emails_is_unassigned():
    from app.routers.admin_router import create_access_code

    db = _db_with_starter_tier()
    body = CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60)
    result = _run(create_access_code(body, admin_user=_admin(), db=db))
    assert result["assigned_emails"] == []
    assert result["status"] == "unassigned"


def test_list_status_reflects_partial_and_full_roster_redemption():
    from app.routers.admin_router import create_access_code, list_access_codes

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(
            code="ASA26", plan_tier_id="starter", duration_days=60,
            assigned_emails=["a@example.com", "b@example.com"],
        ),
        admin_user=_admin(), db=db,
    ))
    db["access_codes"].docs[0]["redemption_count"] = 1  # one of two redeemed

    result = _run(list_access_codes(admin_user=_admin(), db=db))
    assert result["codes"][0]["status"] == "partially_redeemed"

    db["access_codes"].docs[0]["redemption_count"] = 2  # both redeemed
    result = _run(list_access_codes(admin_user=_admin(), db=db))
    assert result["codes"][0]["status"] == "fully_redeemed"


def test_update_can_replace_the_whole_roster():
    from app.routers.admin_router import create_access_code, update_access_code

    db = _db_with_starter_tier()
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))

    updated = _run(update_access_code(
        "ASA26", UpdateAccessCodeRequest(assigned_emails=["New-Partner@Example.com"]), admin_user=_admin(), db=db,
    ))
    assert updated["assigned_emails"] == ["new-partner@example.com"]
    assert updated["status"] == "pending"


def test_update_can_clear_the_roster():
    """Passing an empty list clears it back to a shared/open code — distinct
    from omitting the field entirely, which leaves the roster untouched."""
    from app.routers.admin_router import create_access_code, update_access_code

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60, assigned_emails=["p@example.com"]),
        admin_user=_admin(), db=db,
    ))

    updated = _run(update_access_code("ASA26", UpdateAccessCodeRequest(assigned_emails=[]), admin_user=_admin(), db=db))
    assert updated["assigned_emails"] == []
    assert updated["status"] == "unassigned"


def test_list_redemptions_includes_not_yet_redeemed_roster_members():
    """The roster is visible in full from creation — assigned people who
    haven't redeemed yet show up as their own row, not just the subset who
    happened to redeem already."""
    from app.routers.admin_router import create_access_code, list_access_code_redemptions

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(
            code="ASA26", plan_tier_id="starter", duration_days=60,
            assigned_emails=["redeemed@example.com", "pending@example.com"],
        ),
        admin_user=_admin(), db=db,
    ))
    db["access_code_redemptions"].docs.append({
        "code": "ASA26", "user_id": "u1", "plan_tier_id": "starter",
        "access_start": datetime.utcnow(), "access_end": datetime.utcnow() + timedelta(days=60),
        "previous_subscription_tier": None, "redeemed_at": datetime.utcnow(),
        "revoked_at": None, "revocation_reason": None,
    })
    db["users"].docs.append({"userId": "u1", "email": "redeemed@example.com"})
    db["user_credits"].docs.append({"user_id": "u1", "subscription_tier": "starter", "subscription_source": "access_code"})

    result = _run(list_access_code_redemptions("ASA26", admin_user=_admin(), db=db))
    assert result["count"] == 2
    by_email = {r["email"]: r for r in result["redemptions"]}
    assert by_email["redeemed@example.com"]["effective_status"] == "active"
    assert by_email["pending@example.com"]["effective_status"] == "not_redeemed"
    assert by_email["pending@example.com"]["user_id"] is None


def test_list_redemptions_on_deleted_code_still_returns_audit_history():
    """delete_access_code keeps redemption records for audit even after the
    code itself is gone — this endpoint must not 404 just because the code
    doc is missing."""
    from app.routers.admin_router import list_access_code_redemptions

    db = FakeDb({
        "access_code_redemptions": [{
            "code": "GONE", "user_id": "u1", "plan_tier_id": "starter",
            "access_start": datetime.utcnow(), "access_end": datetime.utcnow() + timedelta(days=60),
            "previous_subscription_tier": None, "redeemed_at": datetime.utcnow(),
            "revoked_at": datetime.utcnow(), "revocation_reason": "admin_revoked",
        }],
    })
    result = _run(list_access_code_redemptions("GONE", admin_user=_admin(), db=db))
    assert result["count"] == 1
    assert result["redemptions"][0]["effective_status"] == "revoked"


# ── Revoking ONE redeemer without touching the code or anyone else ──────────

def test_revoke_one_redemption_clears_only_that_users_wallet(monkeypatch):
    from app.routers.admin_router import create_access_code, revoke_access_code_redemption
    from app.services.CreditService import credit_service

    db = _db_with_starter_tier()
    monkeypatch.setattr(credit_service, "_db", db)
    _run(create_access_code(
        CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db,
    ))
    now = datetime.utcnow()
    db["access_code_redemptions"].docs.append({
        "code": "ASA26", "user_id": "u1", "plan_tier_id": "starter",
        "access_start": now, "access_end": now + timedelta(days=60),
        "previous_subscription_tier": None, "redeemed_at": now,
        "revoked_at": None, "revocation_reason": None,
    })
    db["user_credits"].docs.append({
        "user_id": "u1", "subscription_tier": "starter", "subscription_source": "access_code",
        "subscription_credits": 20, "start_date": now, "end_date": now + timedelta(days=60),
    })

    result = _run(revoke_access_code_redemption("ASA26", "u1", admin_user=_admin(), db=db))
    assert result["revoked"] is True

    wallet = db["user_credits"].docs[0]
    assert wallet["subscription_source"] is None
    assert wallet["subscription_tier"] is None

    redemption = db["access_code_redemptions"].docs[0]
    assert redemption["revoked_at"] is not None
    assert redemption["revocation_reason"] == "admin_revoked"

    code_doc = db["access_codes"].docs[0]
    assert code_doc["is_active"] is True  # the code itself is untouched


def test_revoke_one_redemption_unknown_user_404():
    from app.routers.admin_router import create_access_code, revoke_access_code_redemption

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db,
    ))
    with pytest.raises(HTTPException) as exc_info:
        _run(revoke_access_code_redemption("ASA26", "nobody", admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 404


def test_revoke_one_redemption_already_revoked_400():
    from app.routers.admin_router import create_access_code, revoke_access_code_redemption

    db = _db_with_starter_tier()
    _run(create_access_code(
        CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db,
    ))
    now = datetime.utcnow()
    db["access_code_redemptions"].docs.append({
        "code": "ASA26", "user_id": "u1", "plan_tier_id": "starter",
        "access_start": now, "access_end": now + timedelta(days=60),
        "previous_subscription_tier": None, "redeemed_at": now,
        "revoked_at": now, "revocation_reason": "admin_revoked",
    })
    with pytest.raises(HTTPException) as exc_info:
        _run(revoke_access_code_redemption("ASA26", "u1", admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 400


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


# ── Deleting a code entirely ─────────────────────────────────────────────
# Distinct from revoking: revoke keeps the code around (is_active: False) for
# its audit trail; delete removes the code document outright, for cleaning
# up a mistake or a test code. Redemption history is kept either way.

def test_delete_removes_the_code(monkeypatch):
    from app.routers.admin_router import create_access_code, delete_access_code
    from app.services.CreditService import credit_service

    db = _db_with_starter_tier()
    monkeypatch.setattr(credit_service, "_db", db)
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))

    result = _run(delete_access_code("asa26", admin_user=_admin(), db=db))
    assert result == {"deleted": True, "code": "ASA26", "revoked_active_users": 0}
    assert db["access_codes"].docs == []


def test_delete_unknown_code_404():
    from app.routers.admin_router import delete_access_code

    db = FakeDb()
    with pytest.raises(HTTPException) as exc_info:
        _run(delete_access_code("NOTREAL", admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 404


def test_delete_claws_back_active_redeemers_first(monkeypatch):
    from app.routers.admin_router import create_access_code, delete_access_code
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

    result = _run(delete_access_code("ASA26", admin_user=_admin(), db=db))
    assert result["revoked_active_users"] == 1
    assert db["access_codes"].docs == []

    # The code doc is gone, but the redemption record survives for audit,
    # now correctly marked as no longer in effect.
    redemption = db["access_code_redemptions"].docs[0]
    assert redemption["revoked_at"] is not None
    assert redemption["revocation_reason"] == "admin_revoked"
    assert db["user_credits"].docs[0]["subscription_tier"] is None


# ── Restoring one person's revoked/lapsed access ────────────────────────────
# The counterpart to revoke, but scoped to a single redeemer — for when an
# admin decides a revoke (or exhaustion) was a mistake, without having to
# mint a whole new code just to give that person access again.

def test_restore_regrants_full_access_and_clears_revocation(monkeypatch):
    from app.routers.admin_router import create_access_code, restore_access_code_redemption

    db = _db_with_starter_tier()
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))
    db["user_credits"].docs.append({
        "user_id": "u1", "subscription_tier": None, "subscription_source": None,
        "subscription_credits": 0, "bonus_credits": 0, "credits_used": 20,
    })
    db["access_code_redemptions"].docs.append({
        "code": "ASA26", "user_id": "u1", "plan_tier_id": "starter",
        "redeemed_at": datetime.utcnow() - timedelta(days=10), "revoked_at": datetime.utcnow() - timedelta(days=1),
        "revocation_reason": "admin_revoked",
    })

    result = _run(restore_access_code_redemption("asa26", "u1", admin_user=_admin(), db=db))
    assert result["restored"] is True

    wallet = db["user_credits"].docs[0]
    assert wallet["subscription_tier"] == "starter"
    assert wallet["subscription_source"] == "access_code"
    assert wallet["subscription_credits"] == 20  # a fresh allocation, not the drained amount

    redemption = db["access_code_redemptions"].docs[0]
    assert redemption["revoked_at"] is None
    assert redemption["revocation_reason"] is None


def test_restore_rejects_a_different_active_comp_grant(monkeypatch):
    from app.routers.admin_router import create_access_code, restore_access_code_redemption

    db = _db_with_starter_tier()
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))
    db["user_credits"].docs.append({
        "user_id": "u1", "subscription_tier": "pro", "subscription_source": "access_code",
        "subscription_credits": 40, "bonus_credits": 0, "credits_used": 0,
        "end_date": datetime.utcnow() + timedelta(days=30),
    })
    db["access_code_redemptions"].docs.append({
        "code": "ASA26", "user_id": "u1", "plan_tier_id": "starter",
        "redeemed_at": datetime.utcnow() - timedelta(days=10), "revoked_at": datetime.utcnow() - timedelta(days=1),
        "revocation_reason": "admin_revoked",
    })

    with pytest.raises(HTTPException) as exc_info:
        _run(restore_access_code_redemption("ASA26", "u1", admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 400
    # Untouched — the rejected attempt must not clobber their other grant.
    assert db["user_credits"].docs[0]["subscription_tier"] == "pro"


def test_restore_unknown_code_404():
    from app.routers.admin_router import restore_access_code_redemption

    db = FakeDb()
    with pytest.raises(HTTPException) as exc_info:
        _run(restore_access_code_redemption("NOTREAL", "u1", admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 404


def test_restore_unknown_redemption_404():
    from app.routers.admin_router import create_access_code, restore_access_code_redemption

    db = _db_with_starter_tier()
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))

    with pytest.raises(HTTPException) as exc_info:
        _run(restore_access_code_redemption("ASA26", "nobody-redeemed-as-this-user", admin_user=_admin(), db=db))
    assert exc_info.value.status_code == 404


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
