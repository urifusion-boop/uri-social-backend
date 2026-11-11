"""Admin "Add to ad wallet" — credits one of the user's own brand wallets through the ledger."""
import asyncio

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.agents.jane_ads.store import InMemoryWalletStore
from app.agents.jane_ads.wallet import WalletService
from app.routers import admin_router as ar

BRAND = ObjectId()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Brands:
    async def find_one(self, q):
        return {"_id": BRAND} if q == {"_id": BRAND, "user_id": "billing-1"} else None


class _Db:
    def __getitem__(self, name):
        assert name == "brand_profiles"
        return _Brands()


@pytest.fixture
def store(monkeypatch):
    s = InMemoryWalletStore()
    monkeypatch.setattr("app.agents.jane_ads.store.MongoWalletStore", lambda db: s)

    async def _billing(user_id, db):
        return "billing-1"
    monkeypatch.setattr(ar, "_resolve_billing_id", _billing)
    return s


ADMIN = {"claims": {"email": "Admin@uri.com"}}


def test_credits_the_users_brand_wallet_with_a_ledger_row(store):
    res = _run(ar.credit_brand_ad_wallet("u1", str(BRAND), ar.AdWalletCreditRequest(amount_ngn=1_000), ADMIN, _Db()))

    assert res["ad_wallet_ngn"] == 1_000   # below the customer top-up minimum: admin-only
    txns = _run(WalletService(store).list_transactions(str(BRAND)))
    assert len(txns) == 1 and txns[0].reference.startswith("manual:admin@uri.com:")


def test_refuses_a_brand_that_is_not_the_users(store):
    with pytest.raises(HTTPException) as e:
        _run(ar.credit_brand_ad_wallet("u1", str(ObjectId()), ar.AdWalletCreditRequest(amount_ngn=1_000), ADMIN, _Db()))
    assert e.value.status_code == 404
