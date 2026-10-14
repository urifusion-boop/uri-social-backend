"""
"Continue with Google" — connecting Google Ads without a Customer ID.

The owner signs in to Google, we list the Ads accounts they can use by name,
they pick one (or it is picked for them when there is only one), URI's manager
account sends the link request and it is accepted on their behalf with their
own sign-in. Covers the one-time state, where the browser may be sent back to,
which accounts are offered, the link + accept, and what happens when the
signed-in person can't accept. httpx is mocked.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads import google_ads_connection as g
from tests.test_jane_ads_google_ads_connection import FakeDb
from tests.test_jane_ads_google_link_sync import Google, brand_doc, db_with, link


def run(coro):
    return asyncio.run(coro)


MINE = {"customer_id": "4052658060", "name": "Uri Social", "currency_code": "USD", "is_manager": False,
        "is_test": False, "status": "ENABLED"}
MANAGER = {**MINE, "customer_id": "1112223334", "name": "My Agency", "is_manager": True}
PENDING_AT_OWNER = {"results": [{"customerManagerLink": {
    "resourceName": "customers/4052658060/customerManagerLinks/9999999999~55",
    "managerCustomer": "customers/9999999999", "status": "PENDING"}}]}
ACTIVE_AT_URI = {"results": [link("ACTIVE", "55", "4052658060")]}
DETAILS = {"results": [{"customerClient": {"descriptiveName": "Uri Social", "currencyCode": "USD"}}]}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(g.settings, "GOOGLE_ADS_MCC_CUSTOMER_ID", "9999999999", raising=False)
    monkeypatch.setattr(g.settings, "GOOGLE_ADS_CLIENT_ID", "client-id", raising=False)
    monkeypatch.setattr(g.settings, "PUBLIC_API_URL", "https://api.example.com", raising=False)
    monkeypatch.setattr(g.settings, "WEB_APP_URL", "https://www.urisocial.com", raising=False)


def signin_db(accounts, minutes_ago=1):
    db = db_with(manager_link_status="none", customer_id="", account_name="")
    brand_doc(db)["google_signin"] = {"access_token": "owner_token", "accounts": accounts,
                                      "at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)}
    return db


def with_insert(db):
    coll = db["x"]

    async def insert_one(doc):
        coll.docs.append(doc)

    coll.insert_one = insert_one
    return db


class TestStartingTheSignIn:
    def test_issues_a_one_time_state_and_keeps_no_long_lived_access(self):
        db = with_insert(db_with(manager_link_status="none", customer_id=""))
        url = run(g.start_signin(db, "u1", "b1", "https://uri-staging.urisocial.com"))
        state_doc = next(d for d in db["x"].docs if d.get("state"))
        assert state_doc["state"].startswith("b_") and f"state={state_doc['state']}" in url
        assert "access_type=online" in url and "adwords" in url
        assert "redirect_uri=https%3A%2F%2Fapi.example.com%2Fjane-ads%2Fgoogle%2Fadmin%2Fconnect%2Fcallback" in url
        assert state_doc["return_to"] == "https://uri-staging.urisocial.com"

    def test_first_time_creates_the_brands_connection(self):
        db = with_insert(FakeDb())
        run(g.start_signin(db, "u9", "b9", ""))
        assert any(d.get("platform") == "google_ads" and d.get("brand_id") == "b9" for d in db["x"].docs)

    @pytest.mark.parametrize("url,kept", [
        ("https://uri-staging.urisocial.com/workspace", "https://uri-staging.urisocial.com"),
        ("https://www.urisocial.com", "https://www.urisocial.com"),
        ("http://localhost:3000", "http://localhost:3000"),
    ])
    def test_our_own_app_is_where_the_browser_returns(self, url, kept):
        assert g.safe_return_to(url) == kept

    @pytest.mark.parametrize("url", ["https://evil.example.com", "https://urisocial.com.evil.io",
                                     "http://uri-staging.urisocial.com", "javascript:alert(1)", "", "not a url"])
    def test_anywhere_else_falls_back_to_the_app(self, url):
        assert g.safe_return_to(url) == "https://www.urisocial.com"

    def test_state_is_single_use_and_expires(self):
        db = FakeDb()
        now = datetime.now(timezone.utc)
        db["x"].docs.append({"state": "b_1", "user_id": "u1", "brand_id": "b1", "created_at": now, "used": False})
        db["x"].docs.append({"state": "b_old", "user_id": "u1", "brand_id": "b1", "used": False,
                             "created_at": now - timedelta(minutes=40)})
        assert run(g.take_signin_state(db, "b_1"))["brand_id"] == "b1"
        assert run(g.take_signin_state(db, "b_1")) is None            # replayed
        assert run(g.take_signin_state(db, "b_old")) is None          # too old
        assert run(g.take_signin_state(db, "b_never_issued")) is None


class TestWhichAccountsAreOffered:
    def test_only_accounts_ads_can_run_in(self):
        test_account = {**MINE, "customer_id": "5", "is_test": True}
        closed = {**MINE, "customer_id": "6", "status": "CANCELED"}
        uris_own = {**MINE, "customer_id": "9999999999"}
        assert g.usable_accounts([MINE, MANAGER, test_account, closed, uris_own]) == [MINE]


class TestLinkingThePickedAccount:
    def test_linked_without_the_owner_opening_google_ads(self):
        db = signin_db([MINE, MANAGER])
        with Google({"result": {"resourceName": "customers/9999999999/customerClientLinks/4052658060~55"}},
                    PENDING_AT_OWNER, {"result": {}}, ACTIVE_AT_URI, DETAILS) as google:
            out = run(g.link_selected_account(db, "u1", "b1", "405-265-8060"))
        assert out["state"] == "ready" and out["auto_accepted"] is True and out["account_name"] == "Uri Social"
        request, _, accept = google.calls[0], google.calls[1], google.calls[2]
        assert request["headers"]["Authorization"] == "Bearer admin_at"        # the request: as URI
        assert accept["url"].endswith("/customers/4052658060/customerManagerLinks:mutate")
        assert accept["json"]["operations"][0]["update"]["status"] == "ACTIVE"
        assert accept["headers"]["Authorization"] == "Bearer owner_token"      # the accept: as the owner
        assert "login-customer-id" not in accept["headers"]
        assert not brand_doc(db).get("google_signin")                           # token dropped once linked

    def test_owner_who_cannot_accept_is_left_waiting_not_failed(self):
        db = signin_db([MINE])
        with Google({"result": {}}, {"error": {"message": "permission denied"}},
                    {"results": [link("PENDING", "55", "4052658060")]}):
            out = run(g.link_selected_account(db, "u1", "b1", "4052658060"))
        assert out["state"] == "manager_link_pending" and out["auto_accepted"] is False
        assert brand_doc(db)["account_name"] == "Uri Social"      # shown by name while waiting

    def test_google_still_saying_pending_right_after_we_approve_is_waited_out(self, monkeypatch):
        """Seen on the first real sign-in: we approved the link, Google answered
        "pending" for a moment, and the owner was told an admin had to approve."""
        monkeypatch.setattr(g, "_ACCEPT_SETTLE_SECONDS", 0)
        db = signin_db([MINE])
        still_pending = {"results": [link("PENDING", "55", "4052658060")]}
        with Google({"result": {}}, PENDING_AT_OWNER, {"result": {}},
                    still_pending, still_pending, ACTIVE_AT_URI, DETAILS):
            out = run(g.link_selected_account(db, "u1", "b1", "4052658060"))
        assert out["state"] == "ready" and out["auto_accepted"] is True

    def test_if_google_stays_slow_it_is_reported_as_finishing_not_as_needing_an_admin(self, monkeypatch):
        monkeypatch.setattr(g, "_ACCEPT_SETTLE_SECONDS", 0)
        db = db_with(manager_link_status="none", customer_id="", account_name="")
        still_pending = {"results": [link("PENDING", "55", "4052658060")]}
        with patch.object(g, "exchange_code_for_tokens", new=AsyncMock(return_value={"access_token": "owner_token"})), \
             patch.object(g, "list_user_accounts", new=AsyncMock(return_value=[MINE])), \
             Google({"result": {}}, PENDING_AT_OWNER, {"result": {}}, *[still_pending] * 5):
            out = run(g.finish_signin(db, {"user_id": "u1", "brand_id": "b1", "return_to": "https://app"}, "code"))
        assert out["outcome"] == "finishing"

    def test_someone_who_could_not_approve_is_still_told_an_admin_must(self, monkeypatch):
        monkeypatch.setattr(g, "_ACCEPT_SETTLE_SECONDS", 0)
        db = db_with(manager_link_status="none", customer_id="", account_name="")
        with patch.object(g, "exchange_code_for_tokens", new=AsyncMock(return_value={"access_token": "owner_token"})), \
             patch.object(g, "list_user_accounts", new=AsyncMock(return_value=[MINE])), \
             Google({"result": {}}, {"error": {"message": "permission denied"}},
                    {"results": [link("PENDING", "55", "4052658060")]}) as google:
            out = run(g.finish_signin(db, {"user_id": "u1", "brand_id": "b1", "return_to": "https://app"}, "code"))
        assert out["outcome"] == "pending" and len(google.calls) == 3      # read once, no waiting around

    def test_the_app_is_told_it_is_finishing_only_for_a_few_minutes(self):
        now = datetime.now(timezone.utc)
        assert g.owner_approval_settling({"manager_link_status": "pending", "owner_approved_at": now}) is True
        assert g.owner_approval_settling({"manager_link_status": "pending",
                                          "owner_approved_at": now - timedelta(minutes=10)}) is False
        assert g.owner_approval_settling({"manager_link_status": "pending"}) is False       # nobody approved it
        assert g.owner_approval_settling({"manager_link_status": "active", "owner_approved_at": now}) is False

    def test_an_account_the_sign_in_did_not_show_is_refused(self):
        db = signin_db([MINE])
        with Google() as google, pytest.raises(g.AccountNotLinkable):
            run(g.link_selected_account(db, "u1", "b1", "1234567890"))
        assert google.calls == []

    def test_an_old_sign_in_asks_to_sign_in_again(self):
        db = signin_db([MINE], minutes_ago=90)
        with Google() as google, pytest.raises(g.AccountNotLinkable, match="expired"):
            run(g.link_selected_account(db, "u1", "b1", "4052658060"))
        assert google.calls == []


class TestComingBackFromGoogle:
    STATE = {"user_id": "u1", "brand_id": "b1", "return_to": "https://app"}

    def _finish(self, db, accounts, *google_responses):
        with patch.object(g, "exchange_code_for_tokens", new=AsyncMock(return_value={"access_token": "owner_token"})), \
             patch.object(g, "list_user_accounts", new=AsyncMock(return_value=accounts)), \
             Google(*google_responses) as google:
            return run(g.finish_signin(db, self.STATE, "code")), google

    def test_one_account_is_linked_straight_away(self):
        db = db_with(manager_link_status="none", customer_id="", account_name="")
        out, _ = self._finish(db, [MINE, MANAGER], {"result": {}}, PENDING_AT_OWNER, {"result": {}},
                              ACTIVE_AT_URI, DETAILS)
        assert out == {"return_to": "https://app", "outcome": "linked", "detail": ""}

    def test_several_accounts_ask_the_owner_to_choose(self):
        db = db_with(manager_link_status="none", customer_id="")
        other = {**MINE, "customer_id": "7778889990", "name": "Second Shop"}
        out, google = self._finish(db, [MINE, other])
        assert out["outcome"] == "choose" and google.calls == []
        assert len(brand_doc(db)["google_signin"]["accounts"]) == 2

    def test_no_usable_account_says_so(self):
        db = db_with(manager_link_status="none", customer_id="")
        out, _ = self._finish(db, [MANAGER])
        assert out["outcome"] == "no_accounts"

    def test_google_failure_is_a_clean_message(self):
        db = db_with(manager_link_status="none", customer_id="")
        with patch.object(g, "exchange_code_for_tokens",
                          new=AsyncMock(side_effect=g.GoogleAdsConnectionError("invalid_grant"))):
            out = run(g.finish_signin(db, self.STATE, "code"))
        assert out["outcome"] == "error" and "try again" in out["detail"]


class TestWhoMayBeTheAdminConnection:
    def _check(self, names):
        with patch("httpx.AsyncClient") as mock:
            r = AsyncMock()
            r.json = lambda: {"resourceNames": names}
            client = AsyncMock()
            client.get = AsyncMock(return_value=r)
            mock.return_value.__aenter__.return_value = client
            return run(g.identity_manages_mcc("token"))

    def test_someone_with_the_manager_account(self):
        assert self._check(["customers/9999999999", "customers/1"]) is True

    def test_a_stranger_with_their_own_account(self):
        assert self._check(["customers/1234567890"]) is False


class TestReminderADayLater:
    """One nudge when the request is still waiting — never if it was accepted meanwhile."""

    def _db(self, hours_ago, **conn):
        db = db_with(manager_link_status="pending", invited_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
                     **conn)
        coll = db["x"]

        class Cursor:
            def __init__(self, docs):
                self.docs = docs

            async def to_list(self, length=None):
                return [dict(d) for d in self.docs]

        coll.find = lambda q: Cursor([d for d in coll.docs if coll._matches(d, q)])
        coll.docs.append({"userId": "u1", "email": "owner@docerity.com"})
        return db

    def _remind(self, db, *google_responses):
        sent, logged = [], []

        async def fake_send(db_, channel, user, profile, user_id, title, message, link):
            sent.append({"channel": channel, "to": user.get("email"), "title": title, "message": message, "link": link})
            return True, None

        async def fake_log(db_, user_id, event, title, message, metadata, deliveries):
            logged.append({"event": event, "deliveries": deliveries})

        with patch("app.services.brand_notifier._send", new=fake_send), \
             patch("app.services.brand_notifier._log", new=fake_log), Google(*google_responses):
            out = run(g.remind_pending_links(db))
        return out, sent, logged

    def test_still_waiting_after_a_day_gets_one_reminder(self):
        db = self._db(26, customer_id="4052658060")
        out, sent, logged = self._remind(db, {"results": [link("PENDING", "55", "4052658060")]})
        assert out["reminded"] == 1 and sent[0]["to"] == "owner@docerity.com"
        assert "405-265-8060" in sent[0]["message"] and "Continue with Google" in sent[0]["message"]
        assert sent[0]["link"].endswith("/workspace/?tab=connections")
        assert logged[0]["event"] == "google_ads_link_reminder"
        # and never a second one
        out2, sent2, _ = self._remind(db, {"results": [link("PENDING", "55", "4052658060")]})
        assert out2["reminded"] == 0 and sent2 == []

    def test_accepted_in_the_meantime_gets_no_reminder(self):
        db = self._db(26, customer_id="4052658060")
        out, sent, _ = self._remind(db, {"results": [link("ACTIVE", "55", "4052658060")]}, DETAILS)
        assert out == {"checked": 1, "reminded": 0, "already_accepted": 1} and sent == []
        assert brand_doc(db)["manager_link_status"] == "active"

    def test_too_soon_is_left_alone(self):
        db = self._db(3)
        out, sent, _ = self._remind(db)
        assert out["checked"] == 0 and sent == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
