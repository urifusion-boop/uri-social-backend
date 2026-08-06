"""
Unit tests for the Google Ads connection state machine (google_ads_connection.py).

httpx is mocked throughout — these prove the module's own state machine and OAuth
token handling are correct, not that Google's live API behaves as documented (no
credentials exist yet to verify that — see the Phase-1 plan). Follows the exact
convention established by test_jane_ads_meta_adapter.py: no pytest-asyncio, a manual
_run() helper, a hand-rolled FakeDb, unittest.mock.patch("httpx.AsyncClient").
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.agents.jane_ads.google_ads_connection import (
    AdsConnectionRequired,
    ConnectionState,
    create_client_account_under_mcc,
    exchange_code_for_tokens,
    get_valid_access_token,
    refresh_access_token,
    request_manager_link,
    resolve_connection_state,
    resolve_customer_id_for_launch,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    """Minimal find_one/update_one supporting the simple equality + brand-scope
    queries this module actually issues — not a general Mongo simulator."""

    def __init__(self):
        self.docs: list[dict] = []

    def _matches(self, doc: dict, query: dict) -> bool:
        for key, value in query.items():
            if key == "$or":
                if not any(self._matches(doc, alt) for alt in value):
                    return False
            elif doc.get(key) != value:
                return False
        return True

    async def find_one(self, query: dict, *args, **kwargs):
        matches = [d for d in self.docs if self._matches(d, query)]
        if not matches:
            return None
        sort = kwargs.get("sort")
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key) or "", reverse=(direction == -1))
        return dict(matches[0])

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = {k: v for k, v in query.items() if k != "$or"}
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)


class FakeDb:
    def __init__(self):
        self._coll = FakeCollection()

    def __getitem__(self, name):
        return self._coll


class WhatsAppPatched:
    """Context manager patching get_brand_whatsapp — the module imports it locally
    inside functions (from .whatsapp import get_brand_whatsapp), so we patch the
    source module's attribute directly."""

    def __init__(self, number: str):
        self._patcher = patch("app.agents.jane_ads.whatsapp.get_brand_whatsapp", new=AsyncMock(return_value=number))

    def __enter__(self):
        return self._patcher.__enter__()

    def __exit__(self, *a):
        return self._patcher.__exit__(*a)


def _conn_doc(**kw) -> dict:
    base = dict(
        id="gads_1", platform="google_ads", user_id="u1", brand_id="b1",
        connection_status="active", customer_id="1234567890",
        login_customer_id="9999999999", refresh_token="rt_1", access_token="at_1",
        token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        manager_link_status="active", account_name="Acme Co",
        connected_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return base


def _mock_client(responses):
    client = AsyncMock()
    resp_iter = iter(responses)

    async def _next(*a, **kw):
        r = AsyncMock()
        r.json = lambda: next(resp_iter)
        return r

    client.post = AsyncMock(side_effect=_next)
    return client


# ── resolve_connection_state ────────────────────────────────────────────────────

def test_resolve_connection_state_none_when_no_doc():
    db = FakeDb()
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.NONE
    assert conn is None


def test_resolve_connection_state_oauth_pending():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(connection_status="pending_user_match"))
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.OAUTH_PENDING


def test_resolve_connection_state_manager_link_pending():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="pending"))
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.MANAGER_LINK_PENDING


def test_resolve_connection_state_manager_link_refused():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="refused"))
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.MANAGER_LINK_REFUSED


def test_resolve_connection_state_expired_on_bad_refresh():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc())
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client([{"error": "invalid_grant"}])
        state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.EXPIRED


def test_resolve_connection_state_ready_on_good_refresh():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc())
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"access_token": "fresh", "expires_in": 3600}]
        )
        state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.READY


def test_resolve_connection_state_skips_live_check_when_false():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc())
    # No httpx patch at all — if live_check=False actually skips the network call,
    # this would raise/hang if it tried to hit the real endpoint.
    state, conn = _run(resolve_connection_state(db, "u1", "b1", live_check=False))
    assert state == ConnectionState.READY


# ── resolve_customer_id_for_launch (the pre-flight gate) ────────────────────────

def test_resolve_customer_id_for_launch_raises_no_whatsapp_first():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc())
    with WhatsAppPatched(""):
        try:
            _run(resolve_customer_id_for_launch(db, "u1", "b1"))
            assert False, "expected AdsConnectionRequired"
        except AdsConnectionRequired as e:
            assert e.state == ConnectionState.NO_WHATSAPP


def test_resolve_customer_id_for_launch_raises_for_each_non_ready_state():
    db = FakeDb()  # no connection at all
    with WhatsAppPatched("2348031234567"):
        try:
            _run(resolve_customer_id_for_launch(db, "u1", "b1"))
            assert False, "expected AdsConnectionRequired"
        except AdsConnectionRequired as e:
            assert e.state == ConnectionState.NONE


def test_resolve_customer_id_for_launch_never_returns_a_settings_fallback():
    """The literal, non-negotiable difference from ads_connection.py's
    resolve_ads_page_for_launch: there is no settings-derived fallback branch here at
    all. Confirm a fully READY connection returns THIS brand's own customer_id, not
    anything sourced from settings.GOOGLE_ADS_MCC_CUSTOMER_ID (which is only ever used
    as login_customer_id, never as a substitute customer_id)."""
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(customer_id="555555", login_customer_id="9999999999"))
    with WhatsAppPatched("2348031234567"), patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"access_token": "fresh", "expires_in": 3600}]
        )
        result = _run(resolve_customer_id_for_launch(db, "u1", "b1"))
    assert result["customer_id"] == "555555"
    assert result["customer_id"] != "9999999999"  # never the MCC id
    assert result["login_customer_id"] == "9999999999"
    assert result["whatsapp_number"] == "2348031234567"


# ── refresh_access_token / get_valid_access_token ────────────────────────────────

def test_refresh_access_token_persists_new_token():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc())
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"access_token": "brand_new_token", "expires_in": 3600}]
        )
        _run(refresh_access_token(db, db["social_connections"].docs[0]))
    stored = db["social_connections"].docs[0]
    assert stored["access_token"] == "brand_new_token"
    assert stored["token_expires_at"] > datetime.now(timezone.utc)


def test_get_valid_access_token_skips_refresh_when_not_expiring_soon():
    db = FakeDb()
    conn = _conn_doc(access_token="still_good", token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    # No httpx patch — would raise/hang if a refresh call were actually attempted.
    token = _run(get_valid_access_token(db, conn))
    assert token == "still_good"


def test_get_valid_access_token_refreshes_when_close_to_expiry():
    db = FakeDb()
    conn = _conn_doc(id="gads_2", access_token="stale", token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=10))
    db["social_connections"].docs.append(conn)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"access_token": "refreshed", "expires_in": 3600}]
        )
        token = _run(get_valid_access_token(db, conn))
    assert token == "refreshed"


# ── request_manager_link (the "already linked to another manager" friction) ─────

def test_request_manager_link_success_sets_pending():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="none", customer_id=""))
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"results": [{"resourceName": "customers/1/customerClientLinks/2"}]}]
        )
        result = _run(request_manager_link(db, "u1", "b1", "5551234567"))
    assert result["manager_link_status"] == "pending"
    assert db["social_connections"].docs[0]["manager_link_status"] == "pending"
    assert db["social_connections"].docs[0]["customer_id"] == "5551234567"


def test_request_manager_link_refusal_sets_refused_and_next_resolve_reports_it():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="none", customer_id=""))
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"error": {"message": "This client is already linked to a manager."}}]
        )
        result = _run(request_manager_link(db, "u1", "b1", "5551234567"))
    assert result["manager_link_status"] == "refused"

    # A second resolve_connection_state call (no further httpx call needed — refusal
    # is checked before the live-check branch) correctly reports the refused state.
    state, conn = _run(resolve_connection_state(db, "u1", "b1", live_check=False))
    assert state == ConnectionState.MANAGER_LINK_REFUSED


# ── create_client_account_under_mcc ──────────────────────────────────────────────

def test_create_client_account_under_mcc_auto_links():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="none", customer_id=""))
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"resourceName": "customers/777888999"}]
        )
        result = _run(create_client_account_under_mcc(db, "u1", "b1", "New Brand Ads"))
    assert result["customer_id"] == "777888999"
    stored = db["social_connections"].docs[0]
    assert stored["manager_link_status"] == "active"
    assert stored["created_account_by_uri"] is True
