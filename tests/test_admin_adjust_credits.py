"""
Admin "Adjust Credits" action (CreditService.admin_adjust_credits).

Live-reported bug: an admin tops a user up with credits after they paid
outside the normal flow, and the user keeps seeing "you're out of
credits"/"your trial has ended" for weeks afterward. Traced to a real
arithmetic bug here: bonus_credits and subscription_credits are already
net of usage (get_user_wallet's own recompute is just bonus_credits +
subscription_credits — see its docstring), but this function subtracted
credits_used from the new total a second time, writing a wrong (often
negative) credits_remaining to the database whenever a user's lifetime
credits_used exceeded the fresh grant amount — exactly the case for
someone who fully used up a trial/plan before being topped up.
"""
import asyncio

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

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$setOnInsert", {}))
            new_doc.update(update.get("$set", {}))
            new_doc.setdefault("_id", f"fake-id-{len(self.docs)}")
            self.docs.append(new_doc)

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._colls = {name: FakeCollection(docs) for name, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


@pytest.fixture
def fake_db():
    db = FakeDb()
    credit_service._db = db
    yield db
    credit_service._db = None


def test_topping_up_a_fully_used_trial_gives_the_real_grant_amount_not_a_negative_number(fake_db):
    # A user whose trial/plan ran all the way out: used 50 credits over its
    # lifetime, nothing left now (bonus/subscription both 0).
    fake_db["user_credits"].docs.append({
        "_id": "wallet-1", "user_id": "user-1", "subscription_tier": None, "subscription_source": None,
        "subscription_credits": 0, "bonus_credits": 0, "credits_used": 50,
        "total_credits": 0, "credits_remaining": 0,
    })

    wallet = _run(credit_service.admin_adjust_credits("user-1", 20, notes="client paid manually"))

    assert wallet.credits_remaining == 20  # not 20 - 50 = -30
    assert wallet.bonus_credits == 20


def test_topping_up_preserves_existing_subscription_credits(fake_db):
    fake_db["user_credits"].docs.append({
        "_id": "wallet-1", "user_id": "user-1", "subscription_tier": "starter", "subscription_source": "stripe",
        "subscription_credits": 10, "bonus_credits": 0, "credits_used": 5,
        "total_credits": 10, "credits_remaining": 10,
    })

    wallet = _run(credit_service.admin_adjust_credits("user-1", 15))

    assert wallet.bonus_credits == 15
    assert wallet.credits_remaining == 25  # 15 bonus + 10 subscription


def test_a_negative_adjustment_floors_at_zero_not_debt(fake_db):
    fake_db["user_credits"].docs.append({
        "_id": "wallet-1", "user_id": "user-1", "subscription_tier": None, "subscription_source": None,
        "subscription_credits": 0, "bonus_credits": 5, "credits_used": 100,
        "total_credits": 5, "credits_remaining": 5,
    })

    wallet = _run(credit_service.admin_adjust_credits("user-1", -20, notes="correcting an over-grant"))

    assert wallet.bonus_credits == 0
    assert wallet.credits_remaining == 0  # not -15


def test_granting_credits_to_a_brand_new_wallet_is_also_correct(fake_db):
    wallet = _run(credit_service.admin_adjust_credits("new-user", 20))

    assert wallet.bonus_credits == 20
    assert wallet.credits_remaining == 20


def test_what_gets_stored_matches_what_get_user_wallet_recomputes(fake_db):
    """The real end-to-end guarantee: whatever this writes must agree with
    get_user_wallet()'s own fresh bonus_credits + subscription_credits
    recompute on the very next read — the mechanism every real balance
    check (get_credit_balance, check_sufficient_credits) actually goes
    through."""
    fake_db["user_credits"].docs.append({
        "_id": "wallet-1", "user_id": "user-1", "subscription_tier": None, "subscription_source": None,
        "subscription_credits": 0, "bonus_credits": 0, "credits_used": 50,
        "total_credits": 0, "credits_remaining": 0,
    })
    _run(credit_service.admin_adjust_credits("user-1", 20))

    reread = _run(credit_service.get_user_wallet("user-1"))
    assert reread.credits_remaining == 20
