"""
Uri Market Intelligence — action brief tests (PRD §13, §9 "Concern to
action" journey, §25 P0-12).

No dedicated brief route ever had test coverage before this file even
though create_brief/update_brief were fully built — these are the first
tests exercising the actual endpoint functions rather than just the model.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.agents.market_intelligence.models import BriefCreateRequest


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

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

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
        return self[name]


def _insight_doc(insight_id="ins1", brand_id="b1"):
    return {
        "id": insight_id,
        "brand_id": brand_id,
        "revision": 1,
        "business_implication": "Customers can't find a size that fits.",
        "evidence_ids": ["ev1", "ev2"],
        "suggested_next_step": "Offer a size-guide message on the product page.",
    }


def _ctx(brand_id="b1", user_id="u1"):
    return {"brand_id": brand_id, "user_id": user_id}


# ── GET /insights/{id}/briefs ────────────────────────────────────────────────

def test_get_brief_returns_not_found_shape_when_none_exists():
    from app.agents.market_intelligence.router import get_brief

    db = FakeDb({"mi_insights": [_insight_doc()]})
    res = _run(get_brief("ins1", ctx=_ctx(), db=db))
    assert res["status"] is False


def test_get_brief_never_creates_one():
    from app.agents.market_intelligence.router import get_brief

    db = FakeDb({"mi_insights": [_insight_doc()]})
    _run(get_brief("ins1", ctx=_ctx(), db=db))
    assert db["mi_briefs"].docs == []


def test_get_brief_rejects_cross_tenant_insight():
    from app.agents.market_intelligence.router import get_brief

    db = FakeDb({"mi_insights": [_insight_doc(brand_id="other_brand")]})
    with pytest.raises(HTTPException) as exc_info:
        _run(get_brief("ins1", ctx=_ctx(brand_id="b1"), db=db))
    assert exc_info.value.status_code == 404


# ── POST /insights/{id}/briefs ───────────────────────────────────────────────

def test_create_brief_drafts_from_insight_when_no_message_given():
    from app.agents.market_intelligence.router import create_brief

    db = FakeDb({"mi_insights": [_insight_doc()]})
    res = _run(create_brief("ins1", BriefCreateRequest(proposed_message=None), ctx=_ctx(), db=db))
    brief = res["responseData"]
    assert brief["proposed_message"] == "Offer a size-guide message on the product page."
    assert brief["customer_need"] == "Customers can't find a size that fits."
    assert brief["evidence_ids"] == ["ev1", "ev2"]
    assert brief["destination"] == "market_intelligence"


def test_create_brief_uses_given_message_over_the_insight_default():
    from app.agents.market_intelligence.router import create_brief

    db = FakeDb({"mi_insights": [_insight_doc()]})
    res = _run(create_brief("ins1", BriefCreateRequest(proposed_message="Custom offer text"), ctx=_ctx(), db=db))
    assert res["responseData"]["proposed_message"] == "Custom offer text"


def test_create_brief_is_idempotent_no_duplicate_row():
    from app.agents.market_intelligence.router import create_brief

    db = FakeDb({"mi_insights": [_insight_doc()]})
    first = _run(create_brief("ins1", BriefCreateRequest(proposed_message="v1"), ctx=_ctx(), db=db))
    second = _run(create_brief("ins1", BriefCreateRequest(proposed_message="v2 ignored"), ctx=_ctx(), db=db))
    assert first["responseData"]["id"] == second["responseData"]["id"]
    assert second["responseData"]["proposed_message"] == "v1"
    assert len(db["mi_briefs"].docs) == 1


def test_create_brief_blocked_for_view_only_user():
    from app.agents.market_intelligence.access import require_mi_write_access
    from app.agents.market_intelligence.router import create_brief

    db = FakeDb({
        "mi_insights": [_insight_doc()],
        "mi_access": [{"brand_id": "b1", "user_id": "u1", "level": "view_only"}],
    })
    with pytest.raises(HTTPException) as exc_info:
        ctx = _run(require_mi_write_access(ctx=_ctx(), db=db))
        _run(create_brief("ins1", BriefCreateRequest(proposed_message="x"), ctx=ctx, db=db))
    assert exc_info.value.status_code == 403


# ── PATCH /insights/{id}/briefs ──────────────────────────────────────────────

def test_update_brief_requires_an_existing_brief():
    from app.agents.market_intelligence.router import update_brief

    db = FakeDb({"mi_insights": [_insight_doc()]})
    with pytest.raises(HTTPException) as exc_info:
        _run(update_brief("ins1", BriefCreateRequest(proposed_message="x"), ctx=_ctx(), db=db))
    assert exc_info.value.status_code == 404


def test_update_brief_edits_the_message_in_place():
    from app.agents.market_intelligence.router import create_brief, update_brief

    db = FakeDb({"mi_insights": [_insight_doc()]})
    created = _run(create_brief("ins1", BriefCreateRequest(proposed_message="original"), ctx=_ctx(), db=db))
    brief_id = created["responseData"]["id"]

    updated = _run(update_brief("ins1", BriefCreateRequest(proposed_message="revised"), ctx=_ctx(), db=db))
    assert updated["responseData"]["proposed_message"] == "revised"
    assert updated["responseData"]["id"] == brief_id
    assert len(db["mi_briefs"].docs) == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
