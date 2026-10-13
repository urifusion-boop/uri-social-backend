"""
Google Ads connection: our record follows what Google says.

A brand that links an existing Google Ads account gets a manager-link
invitation; the owner accepts it in Google Ads, not in URI. Nothing used to ask
Google afterwards, so the brand stayed "waiting for you to accept" forever.
These cover asking Google (accepted / still pending / declined / withdrawn),
the account name, Google's own error codes, the launch gate, and change /
disconnect. httpx is mocked, as in test_jane_ads_google_ads_connection.py.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads import google_ads_connection as g
from app.agents.jane_ads.google_ads_connection import ConnectionState
from tests.test_jane_ads_google_ads_connection import (
    FakeDb,
    WhatsAppPatched,
    _admin_conn_doc,
    _conn_doc,
)


def run(coro):
    return asyncio.run(coro)


def db_with(**conn):
    db = FakeDb()
    coll = db["social_connections"]

    async def delete_one(query):
        coll.docs[:] = [d for d in coll.docs if not coll._matches(d, query)]

    coll.delete_one = delete_one
    coll.docs.append(_conn_doc(**conn))
    coll.docs.append(_admin_conn_doc())
    return db


def brand_doc(db):
    return next(d for d in db["social_connections"].docs if d["platform"] == "google_ads")


class Google:
    """Stands in for Google's REST API: answers each call in order and records what was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        self._p = patch("httpx.AsyncClient")
        mock = self._p.start()

        async def post(url, headers=None, json=None, **kw):
            self.calls.append({"url": url, "json": json, "headers": headers})
            r = AsyncMock()
            payload = self.responses.pop(0) if self.responses else {}
            r.json = lambda: payload
            return r

        client = AsyncMock()
        client.post = AsyncMock(side_effect=post)
        mock.return_value.__aenter__.return_value = client
        return self

    def __exit__(self, *a):
        self._p.stop()


def link(status, link_id="7", cid="1234567890"):
    return {"customerClientLink": {"resourceName": f"customers/9999999999/customerClientLinks/{cid}~{link_id}",
                                   "status": status, "managerLinkId": link_id}}


ACCOUNT = {"results": [{"customerClient": {"descriptiveName": "Docerity Ads", "currencyCode": "NGN",
                                           "timeZone": "Africa/Lagos"}}]}


class TestAskingGoogle:
    def test_accepted_invitation_becomes_ready_with_the_account_name(self):
        db = db_with(manager_link_status="pending", account_name="")
        with Google({"results": [link("ACTIVE")]}, ACCOUNT) as google:
            state, conn, err = run(g.refresh_connection(db, "u1", "b1"))
        assert state == ConnectionState.READY and err == ""
        assert conn["account_name"] == "Docerity Ads" and conn["currency_code"] == "NGN"
        assert "customer_client_link.client_customer = 'customers/1234567890'" in google.calls[0]["json"]["query"]
        assert "login-customer-id" in google.calls[0]["headers"] and "developer-token" in google.calls[0]["headers"]

    def test_still_pending_stays_pending(self):
        db = db_with(manager_link_status="pending")
        with Google({"results": [link("PENDING")]}):
            state, _, _ = run(g.refresh_connection(db, "u1", "b1"))
        assert state == ConnectionState.MANAGER_LINK_PENDING

    @pytest.mark.parametrize("google_status,ours", [("REFUSED", "declined"), ("CANCELED", "cancelled"),
                                                    ("INACTIVE", "inactive")])
    def test_declined_or_removed_is_reported_as_ended(self, google_status, ours):
        db = db_with(manager_link_status="pending")
        with Google({"results": [link(google_status)]}):
            state, conn, _ = run(g.refresh_connection(db, "u1", "b1"))
        assert state == ConnectionState.MANAGER_LINK_DECLINED and conn["manager_link_status"] == ours

    def test_invitation_google_no_longer_has_was_withdrawn(self):
        db = db_with(manager_link_status="pending")
        with Google({"results": []}):
            state, _, _ = run(g.refresh_connection(db, "u1", "b1"))
        assert state == ConnectionState.MANAGER_LINK_DECLINED

    def test_a_live_link_wins_over_an_old_cancelled_one(self):
        db = db_with(manager_link_status="pending")
        with Google({"results": [link("CANCELED", "3"), link("ACTIVE", "9")]}, ACCOUNT):
            state, conn, _ = run(g.refresh_connection(db, "u1", "b1"))
        assert state == ConnectionState.READY and conn["manager_link_resource"].endswith("~9")

    def test_ready_is_not_rechecked_on_every_status_read(self):
        db = db_with(manager_link_status="active")
        with Google() as google:
            state, _, _ = run(g.refresh_connection(db, "u1", "b1"))
        assert state == ConnectionState.READY and google.calls == []

    def test_check_again_rechecks_even_when_ready(self):
        db = db_with(manager_link_status="active")
        with Google({"results": [link("INACTIVE")]}):
            state, _, _ = run(g.refresh_connection(db, "u1", "b1", force=True))
        assert state == ConnectionState.MANAGER_LINK_DECLINED

    def test_pending_is_not_polled_more_than_every_twenty_seconds(self):
        db = db_with(manager_link_status="pending",
                     manager_link_synced_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        with Google() as google:
            run(g.refresh_connection(db, "u1", "b1"))
        assert google.calls == []

    def test_google_unreachable_keeps_the_stored_state_and_says_so(self):
        db = db_with(manager_link_status="pending")
        with Google({"error": {"message": "backend unavailable"}}):
            state, conn, err = run(g.refresh_connection(db, "u1", "b1"))
        assert state == ConnectionState.MANAGER_LINK_PENDING and "couldn't reach Google Ads" in err
        assert brand_doc(db)["manager_link_status"] == "pending"

    def test_no_account_chosen_never_calls_google(self):
        db = db_with(manager_link_status="none", customer_id="")
        with Google() as google:
            state, _, _ = run(g.refresh_connection(db, "u1", "b1", force=True))
        assert state == ConnectionState.NEEDS_ACCOUNT_SELECTION and google.calls == []


def google_error(code, message="Request contains an invalid argument."):
    return {"error": {"message": message, "details": [{"errors": [{"errorCode": {"managerLinkError": code},
                                                                   "message": message}]}]}}


class TestGooglesOwnErrorCodes:
    def _link(self, *responses):
        db = db_with(manager_link_status="none", customer_id="")
        with Google(*responses):
            return db, run(g.request_manager_link(db, "u1", "b1", "405-265-8060"))

    def test_already_invited_by_us_is_simply_pending(self):
        db, result = self._link(google_error("ALREADY_INVITED_BY_THIS_MANAGER"))
        assert result == {"manager_link_status": "pending"}
        assert brand_doc(db)["customer_id"] == "4052658060"

    def test_already_managed_by_us_reads_the_real_state(self):
        db, result = self._link(google_error("ALREADY_MANAGED_BY_THIS_MANAGER"),
                                {"results": [link("ACTIVE", cid="4052658060")]}, ACCOUNT)
        assert result == {"manager_link_status": "active"}
        assert run(g.resolve_connection_state(db, "u1", "b1"))[0] == ConnectionState.READY

    def test_too_many_managers_explains_what_to_do(self):
        db, result = self._link(google_error("TOO_MANY_MANAGERS"))
        assert result["manager_link_status"] == "refused" and "maximum number of managers" in result["reason"]

    @pytest.mark.parametrize("code,phrase", [("CLIENT_HAS_NO_ADMIN_USER", "no admin user"),
                                             ("ACCOUNTS_NOT_COMPATIBLE_FOR_LINKING", "manager or test account"),
                                             ("CUSTOMER_CANNOT_MANAGE_SELF", "URI's own manager account")])
    def test_unlinkable_accounts_get_a_reason_not_a_502(self, code, phrase):
        db = db_with(manager_link_status="none", customer_id="")
        with Google(google_error(code)), pytest.raises(g.AccountNotLinkable) as e:
            run(g.request_manager_link(db, "u1", "b1", "4052658060"))
        assert phrase in str(e.value)
        assert brand_doc(db)["customer_id"] == ""       # nothing stored for an account we can't use

    def test_a_declined_invitation_can_be_sent_again(self):
        db = db_with(manager_link_status="declined")
        with Google({"result": {"resourceName": "customers/9999999999/customerClientLinks/1234567890~12"}}):
            result = run(g.request_manager_link(db, "u1", "b1", "1234567890"))
        assert result == {"manager_link_status": "pending"}
        assert run(g.resolve_connection_state(db, "u1", "b1"))[0] == ConnectionState.MANAGER_LINK_PENDING
        assert brand_doc(db)["manager_link_resource"].endswith("~12")


class TestLaunchGate:
    def test_a_link_removed_in_google_ads_stops_the_launch(self):
        db = db_with(manager_link_status="active")
        with WhatsAppPatched("2348012345678"), Google({"results": [link("INACTIVE")]}):
            with pytest.raises(g.AdsConnectionRequired) as e:
                run(g.resolve_customer_id_for_launch(db, "u1", "b1"))
        assert e.value.state == ConnectionState.MANAGER_LINK_DECLINED

    def test_a_live_link_passes(self):
        db = db_with(manager_link_status="active")
        with WhatsAppPatched("2348012345678"), Google({"results": [link("ACTIVE")]}, ACCOUNT):
            out = run(g.resolve_customer_id_for_launch(db, "u1", "b1"))
        assert out["customer_id"] == "1234567890" and out["access_token"] == "admin_at"


class TestChangeAndDisconnect:
    def test_change_account_withdraws_a_pending_invitation(self):
        db = db_with(manager_link_status="pending")
        with Google({"results": [link("PENDING")]}, {"result": {}}) as google:
            out = run(g.release_account(db, "u1", "b1"))
        update = google.calls[1]["json"]["operation"]
        assert update["update"]["status"] == "CANCELED" and update["updateMask"] == "status"
        assert out == {"released": True, "removed": False, "google_note": ""}
        assert run(g.resolve_connection_state(db, "u1", "b1"))[0] == ConnectionState.NEEDS_ACCOUNT_SELECTION

    def test_disconnect_removes_an_accepted_link_and_the_connection(self):
        db = db_with(manager_link_status="active")
        with Google({"results": [link("ACTIVE")]}, ACCOUNT, {"result": {}}) as google:
            out = run(g.release_account(db, "u1", "b1", remove=True))
        assert google.calls[-1]["json"]["operation"]["update"]["status"] == "INACTIVE"
        assert out["removed"] and run(g.resolve_connection_state(db, "u1", "b1"))[0] == ConnectionState.NONE

    def test_released_on_our_side_even_if_google_cant_be_reached(self):
        db = db_with(manager_link_status="pending")
        with Google({"error": {"message": "unavailable"}}):
            out = run(g.release_account(db, "u1", "b1"))
        assert "Admin → Access and security → Managers" in out["google_note"]
        assert brand_doc(db)["customer_id"] == ""

    def test_an_account_uri_created_is_not_released(self):
        db = db_with(manager_link_status="active", created_account_by_uri=True)
        with Google() as google, pytest.raises(g.AccountNotLinkable):
            run(g.release_account(db, "u1", "b1"))
        assert google.calls == [] and brand_doc(db)["customer_id"] == "1234567890"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
