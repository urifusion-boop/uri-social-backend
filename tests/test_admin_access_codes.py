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

    async def find_one(self, query, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

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


def test_update_access_code_can_revoke():
    from app.routers.admin_router import create_access_code, update_access_code

    db = _db_with_starter_tier()
    _run(create_access_code(CreateAccessCodeRequest(code="ASA26", plan_tier_id="starter", duration_days=60), admin_user=_admin(), db=db))

    updated = _run(update_access_code("ASA26", UpdateAccessCodeRequest(is_active=False), admin_user=_admin(), db=db))
    assert updated["is_active"] is False


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


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
