"""
Admin "Set Plan" action.

Live-reported bug: topping a user up to 20 credits via the admin panel's
existing "Adjust Credits" control never made their subscription badge stop
showing "free" — because that control only ever touches bonus_credits (see
CreditService.admin_adjust_credits), which is a genuinely different thing
from a subscription tier. There was no admin action anywhere that could set
subscription_tier at all.

admin_set_subscription/admin_clear_subscription are the actual "change this
user's plan" actions, writing the wallet exactly like a real purchase or an
access-code redemption would (subscription_tier, subscription_credits,
start_date/end_date, subscription_source), so:
- the "free" badge updates immediately (get_user_wallet always recomputes
  credits_remaining as bonus_credits + subscription_credits on read — the
  same mechanism the access-code grant path already relies on).
- SubscriptionService.expire_subscriptions() (unchanged, already runs daily)
  auto-lapses the grant back to free once end_date passes, with no new cron
  job needed.
- subscription_source="admin_grant" (not "access_code") deliberately keeps
  these grants OUT of deduct_credit's access-code-specific "auto-revoke the
  instant credits hit 0" rule.
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


def test_set_subscription_updates_tier_and_credits(fake_db):
    fake_db["user_credits"].docs.append({
        "_id": "wallet-1", "user_id": "user-1", "subscription_tier": None, "subscription_source": None,
        "subscription_credits": 0, "bonus_credits": 0, "credits_used": 0,
        "total_credits": 0, "credits_remaining": 0,
        "start_date": None, "end_date": None, "next_renewal": None,
    })

    wallet = _run(credit_service.admin_set_subscription(
        "user-1", "starter", credits_monthly=20, duration_days=30, notes="manual comp"
    ))

    assert wallet.subscription_tier == "starter"
    assert wallet.subscription_credits == 20
    assert wallet.subscription_source == "admin_grant"
    # This is the exact bug: credits_remaining must now reflect the plan,
    # not stay stuck reporting only bonus credits.
    assert wallet.credits_remaining == 20
    assert wallet.end_date is not None
    assert wallet.end_date > datetime.utcnow() + timedelta(days=29)


def test_set_subscription_preserves_existing_bonus_credits(fake_db):
    fake_db["user_credits"].docs.append({
        "_id": "wallet-1", "user_id": "user-1", "subscription_tier": None, "subscription_source": None,
        "subscription_credits": 0, "bonus_credits": 5, "credits_used": 0,
        "total_credits": 5, "credits_remaining": 5,
        "start_date": None, "end_date": None, "next_renewal": None,
    })

    wallet = _run(credit_service.admin_set_subscription(
        "user-1", "growth", credits_monthly=35, duration_days=30
    ))

    assert wallet.bonus_credits == 5
    assert wallet.credits_remaining == 40  # 5 bonus + 35 subscription


def test_set_subscription_on_a_user_with_no_wallet_creates_one_via_upsert(fake_db):
    wallet = _run(credit_service.admin_set_subscription(
        "brand-new-user", "starter", credits_monthly=20, duration_days=30
    ))

    assert wallet.subscription_tier == "starter"
    assert wallet.credits_remaining == 20
    assert fake_db["user_credits"].docs[0]["user_id"] == "brand-new-user"


def test_set_subscription_logs_an_auditable_transaction(fake_db):
    fake_db["user_credits"].docs.append({
        "_id": "wallet-1", "user_id": "user-1", "subscription_tier": None, "subscription_source": None,
        "subscription_credits": 0, "bonus_credits": 0, "credits_used": 0,
        "total_credits": 0, "credits_remaining": 0,
        "start_date": None, "end_date": None, "next_renewal": None,
    })

    _run(credit_service.admin_set_subscription("user-1", "pro", credits_monthly=50, duration_days=30, notes="VIP partner"))

    txns = fake_db["credit_transactions"].docs
    assert len(txns) == 1
    assert txns[0]["type"] == "admin_adjustment"
    assert "pro" in txns[0]["notes"]
    assert "VIP partner" in txns[0]["notes"]


def test_clear_subscription_reverts_to_free_and_keeps_bonus_credits(fake_db):
    now = datetime.utcnow()
    fake_db["user_credits"].docs.append({
        "_id": "wallet-1", "user_id": "user-1", "subscription_tier": "starter", "subscription_source": "admin_grant",
        "subscription_credits": 20, "bonus_credits": 3, "credits_used": 0,
        "total_credits": 23, "credits_remaining": 23,
        "start_date": now, "end_date": now + timedelta(days=30), "next_renewal": None,
    })

    wallet = _run(credit_service.admin_clear_subscription("user-1", notes="picked wrong tier"))

    assert wallet.subscription_tier is None
    assert wallet.subscription_source is None
    assert wallet.subscription_credits == 0
    assert wallet.bonus_credits == 3
    assert wallet.credits_remaining == 3
    assert wallet.end_date is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
