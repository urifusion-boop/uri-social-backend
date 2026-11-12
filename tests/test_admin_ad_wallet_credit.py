"""Admin "Add to ad wallet" — credits one of the user's own brand wallets through the ledger."""
import asyncio

import pytest
from fastapi import HTTPException

from app.agents.jane_ads.store import InMemoryWalletStore
from app.agents.jane_ads.wallet import WalletService
from app.routers import admin_router as ar

BRAND = "brnd_abc"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Db:
    pass


@pytest.fixture
def store(monkeypatch):
    s = InMemoryWalletStore()
    monkeypatch.setattr("app.agents.jane_ads.store.MongoWalletStore", lambda db: s)

    async def _billing(user_id, db):
        return "billing-1"
    monkeypatch.setattr(ar, "_resolve_billing_id", _billing)

    async def _ids(user_id, db):
        return [BRAND] if user_id == "billing-1" else []
    monkeypatch.setattr("app.services.AgencyService.AgencyService.accessible_brand_ids", _ids)
    return s


ADMIN = {"claims": {"email": "Admin@uri.com"}}


def test_credits_the_users_brand_wallet_with_a_ledger_row(store):
    res = _run(ar.credit_brand_ad_wallet("u1", BRAND, ar.AdWalletCreditRequest(amount_ngn=1_000), ADMIN, _Db()))

    assert res["ad_wallet_ngn"] == 1_000   # below the customer top-up minimum: admin-only
    txns = _run(WalletService(store).list_transactions(BRAND))
    assert len(txns) == 1 and txns[0].reference.startswith("manual:admin@uri.com:")


def test_refuses_a_brand_that_is_not_the_users(store):
    with pytest.raises(HTTPException) as e:
        _run(ar.credit_brand_ad_wallet("u1", "brnd_other", ar.AdWalletCreditRequest(amount_ngn=1_000), ADMIN, _Db()))
    assert e.value.status_code == 404


def test_details_list_brand_account_wallets_not_brand_profiles(store, monkeypatch):
    from types import SimpleNamespace
    _run(WalletService(store).top_up(BRAND, 7_500, reference="seed", enforce_minimum=False))

    async def _brands(user_id, db):
        return [SimpleNamespace(brand_id=BRAND, name="Uri Social")]
    monkeypatch.setattr("app.services.BrandAccountService.BrandAccountService.list_brands_for_user", _brands)

    assert _run(ar._ad_wallets(_Db(), "billing-1")) == [
        {"brand_id": BRAND, "name": "Uri Social", "ad_wallet_ngn": 7_500}]
