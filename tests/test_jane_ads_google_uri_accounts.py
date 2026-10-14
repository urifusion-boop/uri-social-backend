"""
URI pays Google: brands run Google ads in URI's own Google Ads accounts. An
account is added only if it is under URI's manager account and can pay; each
brand gets its own account the first time; no free account means a clear
message, not a broken launch.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.agents.jane_ads import google_flow as gf
from app.agents.jane_ads import google_uri_accounts as ua


def run(coro):
    return asyncio.run(coro)


class Coll:
    def __init__(self):
        self.docs = []

    def _match(self, d, q):
        return all(d.get(k) == v for k, v in q.items())

    async def find_one(self, q, proj=None):
        return next((dict(d) for d in self.docs if self._match(d, q)), None)

    async def find_one_and_update(self, q, update):
        for d in self.docs:
            if self._match(d, q):
                before = dict(d)
                d.update(update["$set"])
                return before
        return None

    async def update_one(self, q, update, upsert=False):
        for d in self.docs:
            if self._match(d, q):
                d.update(update.get("$set", {}))
                return
        if upsert:
            self.docs.append({**q, **update.get("$setOnInsert", {}), **update.get("$set", {})})

    def find(self, q, proj=None):
        docs = [dict(d) for d in self.docs if self._match(d, q)]

        class C:
            async def to_list(self, length=None):
                return docs
        return C()


class Db:
    def __init__(self):
        self.c = Coll()

    def __getitem__(self, name):
        return self.c


def account(cid, brand=None, billing=True, currency="NGN"):
    return {"customer_id": cid, "name": f"URI {cid}", "currency_code": currency,
            "billing_ready": billing, "brand_id": brand}


class TestAddingAnAccount:
    def _google(self, client=None, billing="APPROVED"):
        rows = [{"customerClient": client}] if client is not None else []

        async def search(db, customer_id, query):
            if "customer_client" in query:
                return rows
            return [{"billingSetup": {"status": billing}}] if billing else []
        return patch.object(ua, "_search", search)

    CLIENT = {"descriptiveName": "URI Ads 1", "currencyCode": "NGN", "manager": False, "status": "ENABLED"}

    def test_an_account_under_uri_that_can_pay_is_added_unassigned(self):
        db = Db()
        with self._google(self.CLIENT):
            out = run(ua.add_account(db, "405-265-8060", "admin@uri"))
        assert out["customer_id"] == "4052658060" and out["brand_id"] is None and out["billing_ready"]

    @pytest.mark.parametrize("client, billing, says", [
        (None, "APPROVED", "isn't under URI"),
        ({**CLIENT, "manager": True}, "APPROVED", "manager account"),
        ({**CLIENT, "status": "CANCELED"}, "APPROVED", "isn't active"),
        (CLIENT, "CANCELLED", "no working payment method"),
        ({**CLIENT, "currencyCode": "EUR"}, "APPROVED", "is in EUR"),
    ])
    def test_unusable_accounts_are_refused_with_the_reason(self, client, billing, says):
        with self._google(client, billing), pytest.raises(ua.UriAccountError) as e:
            run(ua.add_account(Db(), "4052658060", "admin@uri"))
        assert says in str(e.value)

    def test_a_customer_id_must_have_ten_digits(self):
        with pytest.raises(ua.UriAccountError):
            run(ua.inspect_account(Db(), "12345"))


class TestGivingBrandsAnAccount:
    def test_first_launch_takes_a_free_account_and_keeps_it(self):
        db = Db()
        db.c.docs += [account("1111111111"), account("2222222222")]
        first = run(ua.account_for_brand(db, "b1"))
        again = run(ua.account_for_brand(db, "b1"))
        other = run(ua.account_for_brand(db, "b2"))
        assert first["customer_id"] == again["customer_id"] != other["customer_id"]

    def test_an_account_that_cant_pay_is_never_given_out(self):
        db = Db()
        db.c.docs.append(account("1111111111", billing=False))
        with pytest.raises(ua.NoUriAccountAvailable):
            run(ua.account_for_brand(db, "b1"))

    def test_ready_when_the_brand_has_one_or_one_is_free(self):
        db = Db()
        assert run(ua.available(db, "b1")) is False
        db.c.docs.append(account("1111111111", brand="b1"))
        assert run(ua.available(db, "b1")) is True and run(ua.available(db, "b2")) is False
        db.c.docs.append(account("2222222222"))
        assert run(ua.available(db, "b2")) is True


class TestLaunchingThere:
    def test_launch_uses_the_brands_uri_account_as_uris_manager(self, monkeypatch):
        monkeypatch.setattr(ua.settings, "GOOGLE_ADS_MCC_CUSTOMER_ID", "221-539-1819", raising=False)
        db = Db()
        db.c.docs.append(account("1111111111"))
        with patch("app.agents.jane_ads.google_ads_connection.get_admin_valid_access_token",
                   AsyncMock(return_value="admin_at")), \
                patch("app.agents.jane_ads.whatsapp.get_brand_whatsapp", AsyncMock(return_value="234801")):
            conn = run(ua.launch_connection(db, "b1"))
        assert conn["customer_id"] == "1111111111" and conn["login_customer_id"] == "2215391819"
        assert conn["access_token"] == "admin_at" and conn["uri_managed"]

    def test_no_free_account_is_a_plain_message(self, monkeypatch):
        monkeypatch.setattr(gf.settings, "GOOGLE_ADS_DEVELOPER_TOKEN", "dev", raising=False)
        with pytest.raises(HTTPException) as e:
            run(gf.adapter_for_launch(Db(), {"brand_id": "b1"}))
        assert e.value.status_code == 409 and "aren't available just yet" in e.value.detail


class TestTheTwoWays:
    """'uri': URI's account, nothing to connect, wallet pays all.
    'own': the brand's account, Google bills them the spend, wallet pays the fee."""

    def test_uri_is_the_default_and_the_choice_is_kept(self):
        db = Db()
        assert run(gf.get_mode(db, "b1")) == "uri"
        run(gf.set_mode(db, "b1", "own"))
        assert run(gf.get_mode(db, "b1")) == "own" and run(gf.get_mode(db, "b2")) == "uri"
        with pytest.raises(ValueError):
            run(gf.set_mode(db, "b1", "tv"))

    def _configured(self, monkeypatch):
        monkeypatch.setattr(gf.settings, "GOOGLE_ADS_DEVELOPER_TOKEN", "dev", raising=False)
        monkeypatch.setattr(gf.settings, "GOOGLE_ADS_MCC_CUSTOMER_ID", "2215391819", raising=False)

    def test_uri_way_is_ready_with_a_free_account_and_says_so_without(self, monkeypatch):
        self._configured(monkeypatch)
        db = Db()
        ready, reason = run(gf.readiness(db, "u1", "b1"))
        assert not ready and "connect your own Google Ads account" in reason
        db.c.docs.append(account("1111111111"))
        assert run(gf.readiness(db, "u1", "b1")) == (True, "")

    def test_own_way_needs_a_linked_account_and_says_where(self, monkeypatch):
        from app.agents.jane_ads.google_ads_connection import ConnectionState
        self._configured(monkeypatch)
        db = Db()
        run(gf.set_mode(db, "b1", "own"))
        with patch.object(gf, "resolve_connection_state",
                          AsyncMock(return_value=(ConnectionState.MANAGER_LINK_PENDING, {}))):
            ready, reason = run(gf.readiness(db, "u1", "b1"))
        assert not ready and "hasn't approved URI" in reason and "Run by URI" in reason
        with patch.object(gf, "resolve_connection_state", AsyncMock(return_value=(ConnectionState.READY, {}))):
            assert run(gf.readiness(db, "u1", "b1")) == (True, "")

    def test_own_way_launches_in_the_brands_account(self, monkeypatch):
        self._configured(monkeypatch)
        db = Db()
        run(gf.set_mode(db, "b1", "own"))
        own = {"customer_id": "4052658060", "login_customer_id": "2215391819", "access_token": "t",
               "currency_code": "USD", "whatsapp_number": "234"}
        with patch.object(gf, "resolve_customer_id_for_launch", AsyncMock(return_value=own)):
            adapter, conn = run(gf.adapter_for_launch(db, {"brand_id": "b1", "user_id": "u1"}))
        assert conn["customer_id"] == "4052658060" and conn["uri_managed"] is False

    def test_own_way_not_linked_is_a_plain_message(self, monkeypatch):
        from app.agents.jane_ads.google_ads_connection import AdsConnectionRequired, ConnectionState
        self._configured(monkeypatch)
        db = Db()
        run(gf.set_mode(db, "b1", "own"))
        with patch.object(gf, "resolve_customer_id_for_launch",
                          AsyncMock(side_effect=AdsConnectionRequired(ConnectionState.NONE))), \
                pytest.raises(HTTPException) as e:
            run(gf.adapter_for_launch(db, {"brand_id": "b1", "user_id": "u1"}))
        assert e.value.status_code == 409 and "isn't connected" in e.value.detail


class TestWhatTheWalletPays:
    def test_uri_way_pays_the_whole_budget_own_way_only_the_fee(self):
        from app.agents.jane_ads import constants as C
        from app.agents.jane_ads.router import _total_due_ngn
        whole = _total_due_ngn(5400)
        fee = _total_due_ngn(5400, fee_only=True)
        assert whole == round(5400 * C.AD_SPEND_MARKUP, 2)
        assert fee == round(whole - 5400, 2) and 0 < fee < whole

    def test_the_own_way_shortfall_message_says_google_bills_the_spend(self):
        from app.agents.jane_ads.router import _wallet_shortfall_message
        msg = _wallet_shortfall_message(100, 5400, fee_only=True)
        assert "URI's fee" in msg and "Google bills your Google Ads account" in msg
