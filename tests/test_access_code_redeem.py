"""
Access code redemption tests (POST /billing/access-code/redeem).

Admin-generated partner/comp codes (e.g. "ASA26") grant free access to a
plan for duration_days, starting from the REDEEMING user's own moment, not
a shared expiry tied to the code. Expiry itself is deliberately not
reimplemented here — redemption just writes the same wallet fields a real
purchase would (subscription_tier/start_date/end_date), and the existing
daily subscription_service.expire_subscriptions() sweep (unchanged) lapses
it automatically once end_date passes, the same way it already does for
paid subscriptions.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.domain.models.billing_models import RedeemAccessCodeRequest
from app.services.CreditService import credit_service


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
                for k, v in (update.get("$inc") or {}).items():
                    d[k] = d.get(k, 0) + v
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$setOnInsert", {}))
            new_doc.update(update.get("$set", {}))
            for k, v in (update.get("$inc") or {}).items():
                new_doc[k] = new_doc.get(k, 0) + v
            self.docs.append(new_doc)


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._colls = {name: FakeCollection(docs) for name, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


@pytest.fixture
def fake_db(monkeypatch):
    """Backs both credit_service's own db access AND the endpoint's own
    inline get_db() call with the SAME fake instance, so both code paths
    share one consistent in-memory state. credit_service.db is a property
    with no setter (can't be assigned directly) — pre-seeding the private
    _db it lazily caches into sidesteps that without needing to patch the
    class/descriptor. Reset after the test so the real singleton doesn't
    leak a fake db into any other test file."""
    db = FakeDb({
        "access_codes": [{
            "code": "ASA26", "plan_tier_id": "starter", "duration_days": 60,
            "max_redemptions": None, "redemption_count": 0, "is_active": True,
            "expires_at": None, "label": "Africa SME Assembly partnership",
            "created_by": "admin@urisocial.com", "created_at": datetime.utcnow(),
        }],
        "subscription_tiers": [{
            "tier_id": "starter", "name": "Starter Plan", "credits_monthly": 20,
        }],
    })
    monkeypatch.setattr("app.database.get_db", lambda: db)
    credit_service._db = db
    yield db
    credit_service._db = None


def _redeem(code: str, user_id: str, user_email: str | None = None):
    from app.routers.billing_router import redeem_access_code
    return _run(redeem_access_code(
        RedeemAccessCodeRequest(code=code), user_id=user_id, user_email=user_email or f"{user_id}@example.com",
    ))


def test_successful_redemption_grants_the_plan(fake_db):
    result = _redeem("asa26", "user-1")  # lowercase input, normalized internally
    assert result["status"] is True
    assert result["responseData"]["plan_tier_id"] == "starter"
    assert result["responseData"]["duration_days"] == 60

    wallet = fake_db["user_credits"].docs[0]
    assert wallet["subscription_tier"] == "starter"
    assert wallet["subscription_credits"] == 20
    assert wallet["next_renewal"] is None  # never auto-renews/charges
    delta = wallet["end_date"] - wallet["start_date"]
    assert delta.days == 60


def test_redemption_is_recorded(fake_db):
    _redeem("ASA26", "user-1")
    redemptions = fake_db["access_code_redemptions"].docs
    assert len(redemptions) == 1
    assert redemptions[0]["user_id"] == "user-1"
    assert redemptions[0]["code"] == "ASA26"
    assert redemptions[0]["previous_subscription_tier"] is None


def test_redemption_count_increments_on_the_code(fake_db):
    _redeem("ASA26", "user-1")
    assert fake_db["access_codes"].docs[0]["redemption_count"] == 1


def test_same_user_cannot_redeem_twice(fake_db):
    _redeem("ASA26", "user-1")
    with pytest.raises(HTTPException) as exc_info:
        _redeem("ASA26", "user-1")
    assert exc_info.value.status_code == 400
    assert "already redeemed" in exc_info.value.detail.lower()


def test_different_users_redeem_independently(fake_db):
    _redeem("ASA26", "user-1")
    _redeem("ASA26", "user-2")
    assert len(fake_db["access_code_redemptions"].docs) == 2
    assert fake_db["access_codes"].docs[0]["redemption_count"] == 2


def test_unknown_code_is_rejected(fake_db):
    with pytest.raises(HTTPException) as exc_info:
        _redeem("NOTREAL", "user-1")
    assert exc_info.value.status_code == 404


def test_inactive_code_is_rejected(fake_db):
    fake_db["access_codes"].docs[0]["is_active"] = False
    with pytest.raises(HTTPException) as exc_info:
        _redeem("ASA26", "user-1")
    assert exc_info.value.status_code == 400


def test_expired_code_is_rejected(fake_db):
    fake_db["access_codes"].docs[0]["expires_at"] = datetime.utcnow() - timedelta(days=1)
    with pytest.raises(HTTPException) as exc_info:
        _redeem("ASA26", "user-1")
    assert exc_info.value.status_code == 400


def test_max_redemptions_enforced(fake_db):
    fake_db["access_codes"].docs[0]["max_redemptions"] = 1
    fake_db["access_codes"].docs[0]["redemption_count"] = 1
    with pytest.raises(HTTPException) as exc_info:
        _redeem("ASA26", "user-1")
    assert exc_info.value.status_code == 400


def test_previous_subscription_tier_is_recorded_for_audit(fake_db):
    fake_db["user_credits"].docs.append({
        "_id": "fake-object-id", "user_id": "user-1", "subscription_tier": "pro",
        "bonus_credits": 0, "subscription_credits": 40, "frozen_credits": 0,
        "credits_used": 0, "total_credits": 40, "credits_remaining": 40,
    })
    _redeem("ASA26", "user-1")
    redemption = fake_db["access_code_redemptions"].docs[0]
    assert redemption["previous_subscription_tier"] == "pro"
    # The comp grant overrides the prior tier outright.
    assert fake_db["user_credits"].docs[0]["subscription_tier"] == "starter"


# ── Assigned (personal invite) codes ────────────────────────────────────────

def test_assigned_code_redeemable_by_the_right_person(fake_db):
    fake_db["access_codes"].docs[0]["assigned_to_email"] = "partner@example.com"
    result = _redeem("ASA26", "user-1", user_email="Partner@Example.com")  # case-insensitive match
    assert result["status"] is True


def test_assigned_code_rejected_for_wrong_person(fake_db):
    fake_db["access_codes"].docs[0]["assigned_to_email"] = "partner@example.com"
    with pytest.raises(HTTPException) as exc_info:
        _redeem("ASA26", "user-2", user_email="someone-else@example.com")
    assert exc_info.value.status_code == 403
    assert "reserved" in exc_info.value.detail.lower()
    # Nothing granted — the rejected attempt must not touch the wallet.
    assert fake_db["user_credits"].docs == []


def test_unassigned_code_still_redeemable_by_anyone(fake_db):
    # assigned_to_email stays unset (default) — the existing shared-code path.
    result = _redeem("ASA26", "user-1", user_email="whoever@example.com")
    assert result["status"] is True


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
