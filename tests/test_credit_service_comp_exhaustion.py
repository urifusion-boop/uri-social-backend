"""
Comp-grant (access-code redemption) credit-exhaustion tests.

A redeemed access code grants its plan's credits ONCE, deliberately never
refilled mid-window (subscription_source='access_code', next_renewal=None
— see billing_router.py's redeem_access_code). So unlike a real paid
subscription, a comp grant is meant to end whichever comes first: end_date,
or running out of credits. This file covers the "runs out of credits"
half — CreditService.deduct_credit must auto-revoke the tier the instant
that happens, not leave a hollow "Starter" badge with nothing usable
behind it until end_date's daily expiry sweep eventually catches up.

A real PAID subscriber hitting 0 credits must NOT lose their tier this
way — they're just blocked until their next monthly renewal, exactly like
today. Only subscription_source='access_code' triggers this.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.services.CreditService import credit_service


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

    async def find_one_and_update(self, query, update, return_document=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return dict(d)
        return None

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._colls = {name: FakeCollection(docs) for name, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDb()
    credit_service._db = db
    yield db
    credit_service._db = None


def _comp_wallet(credits_remaining: int = 1):
    now = datetime.utcnow()
    return {
        "user_id": "user-1", "subscription_tier": "starter", "subscription_source": "access_code",
        "subscription_credits": credits_remaining, "bonus_credits": 0, "credits_used": 0,
        "total_credits": credits_remaining, "credits_remaining": credits_remaining,
        "start_date": now, "end_date": now + timedelta(days=60),
        "next_renewal": None,
    }


def _paid_wallet(credits_remaining: int = 1):
    now = datetime.utcnow()
    return {
        "user_id": "user-1", "subscription_tier": "starter", "subscription_source": None,
        "subscription_credits": credits_remaining, "bonus_credits": 0, "credits_used": 0,
        "total_credits": credits_remaining, "credits_remaining": credits_remaining,
        "start_date": now, "end_date": now + timedelta(days=30),
        "next_renewal": now + timedelta(days=30),
    }


def test_comp_grant_revoked_when_last_credit_spent(fake_db):
    fake_db["user_credits"].docs.append(_comp_wallet(credits_remaining=1))
    fake_db["access_code_redemptions"].docs.append({
        "code": "ASA26", "user_id": "user-1", "plan_tier_id": "starter",
        "redeemed_at": datetime.utcnow(), "revoked_at": None, "revocation_reason": None,
    })

    ok = _run(credit_service.deduct_credit("user-1", campaign_id="c1", amount=1))
    assert ok is True

    wallet = fake_db["user_credits"].docs[0]
    assert wallet["subscription_tier"] is None
    assert wallet["subscription_source"] is None
    assert wallet["subscription_credits"] == 0

    redemption = fake_db["access_code_redemptions"].docs[0]
    assert redemption["revoked_at"] is not None
    assert redemption["revocation_reason"] == "credits_exhausted"


def test_comp_grant_not_revoked_while_credits_remain(fake_db):
    fake_db["user_credits"].docs.append(_comp_wallet(credits_remaining=5))
    fake_db["access_code_redemptions"].docs.append({
        "code": "ASA26", "user_id": "user-1", "plan_tier_id": "starter",
        "redeemed_at": datetime.utcnow(), "revoked_at": None, "revocation_reason": None,
    })

    _run(credit_service.deduct_credit("user-1", campaign_id="c1", amount=1))

    wallet = fake_db["user_credits"].docs[0]
    assert wallet["subscription_tier"] == "starter"  # untouched — 4 credits still left
    assert fake_db["access_code_redemptions"].docs[0]["revoked_at"] is None


def test_paid_subscription_not_revoked_when_credits_hit_zero(fake_db):
    """The critical distinction: a real paying subscriber running dry mid-
    cycle keeps their tier (blocked until next renewal, same as today) —
    only a comp grant (subscription_source='access_code') auto-revokes."""
    fake_db["user_credits"].docs.append(_paid_wallet(credits_remaining=1))

    ok = _run(credit_service.deduct_credit("user-1", campaign_id="c1", amount=1))
    assert ok is True

    wallet = fake_db["user_credits"].docs[0]
    assert wallet["subscription_tier"] == "starter"  # NOT cleared


def test_targets_the_most_recent_redemption_not_a_stale_one(fake_db):
    """A user who redeemed an earlier code that already lapsed naturally
    (revoked_at still None, since only exhaustion sets it) must not have
    THAT old record mistaken for the current grant."""
    fake_db["user_credits"].docs.append(_comp_wallet(credits_remaining=1))
    older = datetime.utcnow() - timedelta(days=90)
    newer = datetime.utcnow() - timedelta(days=1)
    fake_db["access_code_redemptions"].docs.append({
        "code": "OLDCODE", "user_id": "user-1", "plan_tier_id": "starter",
        "redeemed_at": older, "revoked_at": None, "revocation_reason": None,
    })
    fake_db["access_code_redemptions"].docs.append({
        "code": "ASA26", "user_id": "user-1", "plan_tier_id": "starter",
        "redeemed_at": newer, "revoked_at": None, "revocation_reason": None,
    })

    _run(credit_service.deduct_credit("user-1", campaign_id="c1", amount=1))

    old_record = next(r for r in fake_db["access_code_redemptions"].docs if r["code"] == "OLDCODE")
    new_record = next(r for r in fake_db["access_code_redemptions"].docs if r["code"] == "ASA26")
    assert old_record["revoked_at"] is None
    assert new_record["revoked_at"] is not None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
