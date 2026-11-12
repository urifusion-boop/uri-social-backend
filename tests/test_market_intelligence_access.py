"""
Uri Market Intelligence — access control tests (PRD §21).

Deliberately not built on AgencyRole/WorkspaceRole (see access.py's own
docstring) — this is a small, additive, MI-only layer, and these tests
cover exactly its two jobs: (1) resolving a user's effective access level
(absence of a record = FULL, never the other way around), and (2) deciding
who's allowed to grant/restrict others (an agency admin for an agency-owned
brand, or the brand's own owner for a personal one).
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.agents.market_intelligence.access import (
    can_manage_mi_access,
    get_mi_access_level,
    require_mi_write_access,
)
from app.agents.market_intelligence.models import AccessGrantRequest, MIAccessLevel


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    async def find_one(self, query):
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

        return _Cursor()

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def delete_one(self, query):
        self.docs = [d for d in self.docs if not all(d.get(k) == v for k, v in query.items())]

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._colls = {name: FakeCollection(docs) for name, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())

    def __getattr__(self, name):
        # router.py looks up db.users (attribute style) the same way the
        # real motor database object supports both db["x"] and db.x.
        return self[name]


# ── get_mi_access_level ──────────────────────────────────────────────────────

def test_no_grant_record_means_full_access():
    db = FakeDb()
    level = _run(get_mi_access_level(db, "b1", "u1"))
    assert level == MIAccessLevel.FULL


def test_existing_grant_record_is_honoured():
    db = FakeDb({"mi_access": [{"brand_id": "b1", "user_id": "u1", "level": "view_only"}]})
    level = _run(get_mi_access_level(db, "b1", "u1"))
    assert level == MIAccessLevel.VIEW_ONLY


# ── can_manage_mi_access ─────────────────────────────────────────────────────

def test_personal_brand_owner_can_manage(monkeypatch):
    from app.models.brand_account import BrandAccount

    personal_id = BrandAccount.personal_brand_id("u1")
    db = FakeDb({"brand_accounts": [{"brand_id": personal_id, "owner_user_id": "u1", "agency_id": None}]})
    assert _run(can_manage_mi_access(db, personal_id, "u1")) is True


def test_non_owner_cannot_manage_personal_brand():
    from app.models.brand_account import BrandAccount

    personal_id = BrandAccount.personal_brand_id("u1")
    db = FakeDb({"brand_accounts": [{"brand_id": personal_id, "owner_user_id": "u1", "agency_id": None}]})
    assert _run(can_manage_mi_access(db, personal_id, "someone_else")) is False


def test_no_brand_accounts_record_falls_back_to_personal_id_check():
    from app.models.brand_account import BrandAccount

    personal_id = BrandAccount.personal_brand_id("u1")
    db = FakeDb()  # no brand_accounts collection at all
    assert _run(can_manage_mi_access(db, personal_id, "u1")) is True
    assert _run(can_manage_mi_access(db, personal_id, "someone_else")) is False


def test_agency_owned_brand_defers_to_agency_admin_check(monkeypatch):
    from app.agents.market_intelligence import access as access_module

    db = FakeDb({"brand_accounts": [{"brand_id": "b1", "owner_user_id": "creator", "agency_id": "ag1"}]})

    async def fake_is_admin(user_id, agency_id, db):
        assert agency_id == "ag1"
        return user_id == "admin_user"

    monkeypatch.setattr(
        "app.services.AgencyService.AgencyService.is_agency_admin", staticmethod(fake_is_admin)
    )

    assert _run(can_manage_mi_access(db, "b1", "admin_user")) is True
    assert _run(can_manage_mi_access(db, "b1", "regular_agent")) is False


# ── require_mi_write_access ──────────────────────────────────────────────────

def test_require_write_access_allows_full():
    db = FakeDb()
    ctx = _run(require_mi_write_access(ctx={"brand_id": "b1", "user_id": "u1"}, db=db))
    assert ctx == {"brand_id": "b1", "user_id": "u1"}


def test_require_write_access_blocks_view_only():
    db = FakeDb({"mi_access": [{"brand_id": "b1", "user_id": "u1", "level": "view_only"}]})
    with pytest.raises(HTTPException) as exc_info:
        _run(require_mi_write_access(ctx={"brand_id": "b1", "user_id": "u1"}, db=db))
    assert exc_info.value.status_code == 403


# ── router: GET/POST /access ─────────────────────────────────────────────────

def test_list_access_grants_rejects_non_managers():
    from app.agents.market_intelligence.router import list_access_grants
    from app.models.brand_account import BrandAccount

    personal_id = BrandAccount.personal_brand_id("owner")
    db = FakeDb({"brand_accounts": [{"brand_id": personal_id, "owner_user_id": "owner", "agency_id": None}]})

    with pytest.raises(HTTPException) as exc_info:
        _run(list_access_grants(ctx={"brand_id": personal_id, "user_id": "not_owner"}, db=db))
    assert exc_info.value.status_code == 403


def test_set_access_grant_restricts_a_user():
    from app.agents.market_intelligence.router import set_access_grant
    from app.models.brand_account import BrandAccount

    personal_id = BrandAccount.personal_brand_id("owner")
    db = FakeDb({
        "brand_accounts": [{"brand_id": personal_id, "owner_user_id": "owner", "agency_id": None}],
        "users": [{"userId": "teammate", "email": "teammate@example.com", "first_name": "Tee", "last_name": "Mate"}],
    })

    _run(set_access_grant(
        AccessGrantRequest(email="teammate@example.com", level=MIAccessLevel.VIEW_ONLY),
        ctx={"brand_id": personal_id, "user_id": "owner"}, db=db,
    ))

    level = _run(get_mi_access_level(db, personal_id, "teammate"))
    assert level == MIAccessLevel.VIEW_ONLY


def test_set_access_grant_full_removes_restriction():
    from app.agents.market_intelligence.router import set_access_grant
    from app.models.brand_account import BrandAccount

    personal_id = BrandAccount.personal_brand_id("owner")
    db = FakeDb({
        "brand_accounts": [{"brand_id": personal_id, "owner_user_id": "owner", "agency_id": None}],
        "users": [{"userId": "teammate", "email": "teammate@example.com", "first_name": "Tee", "last_name": "Mate"}],
        "mi_access": [{"brand_id": personal_id, "user_id": "teammate", "level": "view_only"}],
    })

    _run(set_access_grant(
        AccessGrantRequest(email="teammate@example.com", level=MIAccessLevel.FULL),
        ctx={"brand_id": personal_id, "user_id": "owner"}, db=db,
    ))

    level = _run(get_mi_access_level(db, personal_id, "teammate"))
    assert level == MIAccessLevel.FULL
    assert db["mi_access"].docs == []


def test_set_access_grant_rejects_unknown_email():
    from app.agents.market_intelligence.router import set_access_grant
    from app.models.brand_account import BrandAccount

    personal_id = BrandAccount.personal_brand_id("owner")
    db = FakeDb({"brand_accounts": [{"brand_id": personal_id, "owner_user_id": "owner", "agency_id": None}]})

    with pytest.raises(HTTPException) as exc_info:
        _run(set_access_grant(
            AccessGrantRequest(email="nobody@example.com", level=MIAccessLevel.VIEW_ONLY),
            ctx={"brand_id": personal_id, "user_id": "owner"}, db=db,
        ))
    assert exc_info.value.status_code == 404


def test_set_access_grant_blocks_self_restriction():
    from app.agents.market_intelligence.router import set_access_grant
    from app.models.brand_account import BrandAccount

    personal_id = BrandAccount.personal_brand_id("owner")
    db = FakeDb({
        "brand_accounts": [{"brand_id": personal_id, "owner_user_id": "owner", "agency_id": None}],
        "users": [{"userId": "owner", "email": "owner@example.com", "first_name": "O", "last_name": "Wner"}],
    })

    with pytest.raises(HTTPException) as exc_info:
        _run(set_access_grant(
            AccessGrantRequest(email="owner@example.com", level=MIAccessLevel.VIEW_ONLY),
            ctx={"brand_id": personal_id, "user_id": "owner"}, db=db,
        ))
    assert exc_info.value.status_code == 400


def test_list_access_grants_enriches_with_user_display_info():
    from app.agents.market_intelligence.router import list_access_grants
    from app.models.brand_account import BrandAccount

    personal_id = BrandAccount.personal_brand_id("owner")
    db = FakeDb({
        "brand_accounts": [{"brand_id": personal_id, "owner_user_id": "owner", "agency_id": None}],
        "users": [{"userId": "teammate", "email": "teammate@example.com", "first_name": "Tee", "last_name": "Mate"}],
        "mi_access": [{"brand_id": personal_id, "user_id": "teammate", "level": "view_only"}],
    })

    res = _run(list_access_grants(ctx={"brand_id": personal_id, "user_id": "owner"}, db=db))
    grants = res["responseData"]
    assert grants[0]["email"] == "teammate@example.com"
    assert grants[0]["user_name"] == "Tee Mate"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
