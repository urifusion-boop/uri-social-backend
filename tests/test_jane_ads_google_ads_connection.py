"""
Unit tests for the Google Ads connection state machine (google_ads_connection.py).

httpx is mocked throughout — these prove the module's own state machine and OAuth
token handling are correct, not that Google's live API behaves as documented. Follows
the exact convention established by test_jane_ads_meta_adapter.py: no pytest-asyncio,
a manual _run() helper, a hand-rolled FakeDb, unittest.mock.patch("httpx.AsyncClient").

Reflects the corrected connection model, live-confirmed on staging: Google Ads REST
calls authenticate as URI's own admin identity (a single fixed doc,
platform="google_ads_admin"), never a brand's own OAuth token — a brand's own
social_connections doc (platform="google_ads") carries no token fields at all any
more, only customer_id/manager_link_status/account_name. See the module docstring for
the live NOT_ADS_USER error this was built to fix.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.agents.jane_ads.google_ads_connection import (
    AdsConnectionRequired,
    ConnectionState,
    GoogleAdsConnectionError,
    create_client_account_under_mcc,
    get_admin_valid_access_token,
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
    """A brand's own connection doc — no token fields at all (see module docstring):
    it only tracks which Ads account is theirs, never how to authenticate as them."""
    base = dict(
        id="gads_1", platform="google_ads", user_id="u1", brand_id="b1",
        connection_status="active", customer_id="1234567890",
        login_customer_id="9999999999",
        manager_link_status="active", account_name="Acme Co",
        connected_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return base


def _admin_conn_doc(**kw) -> dict:
    """URI's own single admin connection doc — the one that actually carries a
    token, since it's the identity every real REST call authenticates as."""
    base = dict(
        id="gads_admin", platform="google_ads_admin",
        refresh_token="admin_rt", access_token="admin_at",
        token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
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


# ── resolve_connection_state (never touches the network — pure doc-field reads) ──

def test_resolve_connection_state_none_when_no_doc():
    db = FakeDb()
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.NONE
    assert conn is None


def test_resolve_connection_state_manager_link_pending():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="pending"))
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.MANAGER_LINK_PENDING


def test_resolve_connection_state_needs_account_selection_right_after_connect():
    """A connection with no customer_id chosen yet — manager_link_status is "none"
    here, NOT "pending", which must NOT be confused with a real invitation already
    sent to Google (that's a separate state, tested above)."""
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="none", customer_id=""))
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.NEEDS_ACCOUNT_SELECTION


def test_resolve_connection_state_refused_wins_even_with_customer_id_set():
    """request_manager_link sets customer_id even on refusal — the refused check must
    still take priority over needs_account_selection, since customer_id being truthy
    would otherwise mask a real refusal."""
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="refused", customer_id="5551234567"))
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.MANAGER_LINK_REFUSED


def test_resolve_connection_state_manager_link_refused():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="refused"))
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.MANAGER_LINK_REFUSED


def test_resolve_connection_state_ready_needs_no_admin_connection_at_all():
    """State resolution is now pure doc-field reads — no live_check, no network call,
    so it works even with zero admin connection in the db. Whether Google calls would
    actually succeed is a separate, later concern (get_admin_valid_access_token),
    checked only when a real REST call is attempted."""
    db = FakeDb()  # deliberately no admin doc
    db["social_connections"].docs.append(_conn_doc())
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
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


def test_resolve_customer_id_for_launch_raises_clearly_when_admin_not_connected():
    """The exact live bug this whole rework fixes: a brand can be fully READY (their
    own account linked/created) while URI's admin identity was never connected (or
    its token died) — a distinct, URI-wide ops failure, not a per-brand state."""
    db = FakeDb()  # brand doc present, but no admin doc at all
    db["social_connections"].docs.append(_conn_doc())
    with WhatsAppPatched("2348031234567"):
        try:
            _run(resolve_customer_id_for_launch(db, "u1", "b1"))
            assert False, "expected GoogleAdsConnectionError"
        except GoogleAdsConnectionError as e:
            assert "admin" in str(e).lower()


def test_resolve_customer_id_for_launch_never_returns_a_settings_fallback():
    """The literal, non-negotiable difference from ads_connection.py's
    resolve_ads_page_for_launch: there is no settings-derived fallback branch here at
    all. Confirm a fully READY connection returns THIS brand's own customer_id, not
    anything sourced from settings.GOOGLE_ADS_MCC_CUSTOMER_ID (which is only ever used
    as login_customer_id, never as a substitute customer_id)."""
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(customer_id="555555", login_customer_id="9999999999"))
    db["social_connections"].docs.append(_admin_conn_doc())
    with WhatsAppPatched("2348031234567"):
        result = _run(resolve_customer_id_for_launch(db, "u1", "b1"))
    assert result["customer_id"] == "555555"
    assert result["customer_id"] != "9999999999"  # never the MCC id
    assert result["login_customer_id"] == "9999999999"
    assert result["whatsapp_number"] == "2348031234567"


# ── refresh_access_token / get_valid_access_token (generic over any conn_doc —
# exercised here via a plain doc; in production these only ever run against the
# single admin doc, see get_admin_valid_access_token below) ─────────────────────

def test_refresh_access_token_persists_new_token():
    db = FakeDb()
    db["social_connections"].docs.append(_admin_conn_doc())
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
    conn = _admin_conn_doc(access_token="still_good", token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    # No httpx patch — would raise/hang if a refresh call were actually attempted.
    token = _run(get_valid_access_token(db, conn))
    assert token == "still_good"


def test_get_valid_access_token_refreshes_when_close_to_expiry():
    db = FakeDb()
    conn = _admin_conn_doc(access_token="stale", token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=10))
    db["social_connections"].docs.append(conn)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"access_token": "refreshed", "expires_in": 3600}]
        )
        token = _run(get_valid_access_token(db, conn))
    assert token == "refreshed"


def test_get_valid_access_token_handles_naive_datetime_from_mongo():
    # Real pymongo/motor reads back naive UTC datetimes even though we write
    # tz-aware ones (datetime.now(timezone.utc)) — confirmed live on staging,
    # where this raised TypeError: can't subtract offset-naive and
    # offset-aware datetimes. FakeDb just stores whatever object it's given,
    # so this has to be constructed explicitly to catch the regression.
    db = FakeDb()
    naive_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
    conn = _admin_conn_doc(access_token="still_good", token_expires_at=naive_expiry)
    token = _run(get_valid_access_token(db, conn))
    assert token == "still_good"


# ── get_admin_valid_access_token ─────────────────────────────────────────────────

def test_get_admin_valid_access_token_raises_clearly_when_never_connected():
    db = FakeDb()  # no admin doc at all
    try:
        _run(get_admin_valid_access_token(db))
        assert False, "expected GoogleAdsConnectionError"
    except GoogleAdsConnectionError as e:
        assert "admin/connect/initiate" in str(e)


def test_get_admin_valid_access_token_returns_stored_token_when_fresh():
    db = FakeDb()
    db["social_connections"].docs.append(_admin_conn_doc(access_token="admin_token_1"))
    token = _run(get_admin_valid_access_token(db))
    assert token == "admin_token_1"


# ── request_manager_link (the "already linked to another manager" friction) ─────

def test_request_manager_link_success_sets_pending():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="none", customer_id=""))
    db["social_connections"].docs.append(_admin_conn_doc())
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"results": [{"resourceName": "customers/1/customerClientLinks/2"}]}]
        )
        result = _run(request_manager_link(db, "u1", "b1", "5551234567"))
    assert result["manager_link_status"] == "pending"
    brand_doc = next(d for d in db["social_connections"].docs if d["platform"] == "google_ads")
    assert brand_doc["manager_link_status"] == "pending"
    assert brand_doc["customer_id"] == "5551234567"


def test_request_manager_link_refusal_sets_refused_and_next_resolve_reports_it():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="none", customer_id=""))
    db["social_connections"].docs.append(_admin_conn_doc())
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"error": {"message": "This client is already linked to a manager."}}]
        )
        result = _run(request_manager_link(db, "u1", "b1", "5551234567"))
    assert result["manager_link_status"] == "refused"

    # A second resolve_connection_state call correctly reports the refused state —
    # no further httpx call involved, since state resolution never touches the
    # network at all any more.
    state, conn = _run(resolve_connection_state(db, "u1", "b1"))
    assert state == ConnectionState.MANAGER_LINK_REFUSED


def test_request_manager_link_raises_clearly_when_admin_not_connected():
    db = FakeDb()  # brand doc present, no admin doc
    db["social_connections"].docs.append(_conn_doc(manager_link_status="none", customer_id=""))
    try:
        _run(request_manager_link(db, "u1", "b1", "5551234567"))
        assert False, "expected GoogleAdsConnectionError"
    except GoogleAdsConnectionError as e:
        assert "admin" in str(e).lower()


# ── create_client_account_under_mcc ──────────────────────────────────────────────

def test_create_client_account_under_mcc_auto_links():
    db = FakeDb()
    db["social_connections"].docs.append(_conn_doc(manager_link_status="none", customer_id=""))
    db["social_connections"].docs.append(_admin_conn_doc())
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = _mock_client(
            [{"resourceName": "customers/777888999"}]
        )
        result = _run(create_client_account_under_mcc(db, "u1", "b1", "New Brand Ads"))
    assert result["customer_id"] == "777888999"
    brand_doc = next(d for d in db["social_connections"].docs if d["platform"] == "google_ads")
    assert brand_doc["manager_link_status"] == "active"
    assert brand_doc["created_account_by_uri"] is True
