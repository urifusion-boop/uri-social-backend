"""
Unit tests for the new, purely additive /jane-ads/google/* endpoints in router.py.

Endpoints are called directly with explicit db/brand_ctx kwargs rather than through
HTTP — their FastAPI Depends() are plain defaults, so this exercises the real handler
without standing up the app, same convention as test_jane_ads_campaign_status.py.
Does not touch anything under /jane-ads/meta/*.

Reflects the corrected connection model (live-confirmed on staging): Google Ads API
calls authenticate as URI's own admin identity, never a brand's own OAuth token — see
google_ads_connection.py's module docstring. So /google/connect is now a synchronous,
brand-scoped POST with no OAuth roundtrip, and the admin's own one-time OAuth grant
lives under a separate, non-brand-scoped /google/admin/connect/* pair.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.agents.jane_ads.google_ads_connection import AdsConnectionRequired, ConnectionState
from app.agents.jane_ads.router import (
    GoogleAdsCreateAccountBody,
    GoogleAdsLinkExistingBody,
    jane_google_ads_admin_connect_callback,
    jane_google_ads_admin_connect_initiate,
    jane_google_ads_connect,
    jane_google_ads_connection_status,
    jane_google_ads_create_account,
    jane_google_ads_link_existing,
)

BRAND_CTX = {"user_id": "u1", "brand_id": "b1"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, query, *a, **kw):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return type("R", (), {"inserted_id": doc.get("id")})()

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return type("R", (), {"matched_count": 1})()
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)
            return type("R", (), {"matched_count": 0})()
        return type("R", (), {"matched_count": 0})()


class FakeDb:
    def __init__(self):
        self._coll = FakeCollection()

    def __getitem__(self, name):
        return self._coll


# ── admin/connect/initiate ────────────────────────────────────────────────────────

def test_admin_connect_initiate_url_has_offline_and_consent():
    with patch("app.core.config.settings.GOOGLE_ADS_CLIENT_ID", "client123"), \
         patch("app.core.config.settings.PUBLIC_API_URL", "https://api.example.com"):
        resp = _run(jane_google_ads_admin_connect_initiate())
    location = resp.headers["location"]
    assert "access_type=offline" in location
    assert "prompt=consent" in location
    assert "client_id=client123" in location
    assert "scope=https" in location  # url-encoded adwords scope
    assert "admin%2Fconnect%2Fcallback" in location  # url-encoded redirect_uri


def test_admin_connect_initiate_raises_without_client_id():
    with patch("app.core.config.settings.GOOGLE_ADS_CLIENT_ID", ""):
        with pytest.raises(HTTPException) as exc:
            _run(jane_google_ads_admin_connect_initiate())
    assert exc.value.status_code == 500


# ── admin/connect/callback ────────────────────────────────────────────────────────

def test_admin_callback_saves_single_fixed_doc_on_success():
    db = FakeDb()
    with patch("app.core.config.settings.PUBLIC_API_URL", "https://api.example.com"), \
         patch(
             "app.agents.jane_ads.google_ads_connection.exchange_code_for_tokens",
             new=AsyncMock(return_value={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}),
         ):
        resp = _run(jane_google_ads_admin_connect_callback(code="abc123", db=db))
    assert resp.status_code == 200
    assert len(db["social_connections"].docs) == 1
    doc = db["social_connections"].docs[0]
    assert doc["platform"] == "google_ads_admin"
    assert doc["id"] == "gads_admin"
    assert doc["refresh_token"] == "rt"


def test_admin_callback_fails_loudly_with_no_refresh_token():
    db = FakeDb()
    with patch("app.core.config.settings.PUBLIC_API_URL", "https://api.example.com"), \
         patch(
             "app.agents.jane_ads.google_ads_connection.exchange_code_for_tokens",
             new=AsyncMock(return_value={"access_token": "at", "expires_in": 3600}),
         ):
        resp = _run(jane_google_ads_admin_connect_callback(code="abc123", db=db))
    assert resp.status_code == 502
    assert db["social_connections"].docs == []


def test_admin_callback_fails_on_missing_code():
    db = FakeDb()
    resp = _run(jane_google_ads_admin_connect_callback(code=None, db=db))
    assert resp.status_code == 400
    assert db["social_connections"].docs == []


# ── connect (brand-facing, synchronous, no OAuth) ─────────────────────────────────

def test_connect_creates_active_doc_immediately():
    db = FakeDb()
    result = _run(jane_google_ads_connect(db=db, brand_ctx=BRAND_CTX))
    assert result == {"status": "connected"}
    assert len(db["social_connections"].docs) == 1
    doc = db["social_connections"].docs[0]
    assert doc["platform"] == "google_ads"
    assert doc["connection_status"] == "active"
    assert doc["customer_id"] == ""
    assert doc["user_id"] == "u1"
    assert doc["brand_id"] == "b1"
    # No token fields at all — this brand doc never authenticates as itself.
    assert "access_token" not in doc
    assert "refresh_token" not in doc


def test_connect_is_idempotent():
    db = FakeDb()
    _run(jane_google_ads_connect(db=db, brand_ctx=BRAND_CTX))
    result = _run(jane_google_ads_connect(db=db, brand_ctx=BRAND_CTX))
    assert result == {"status": "already_connected"}
    assert len(db["social_connections"].docs) == 1


# ── link-existing-account (the refused-link friction) ────────────────────────────

def test_link_existing_account_surfaces_specific_refusal_message():
    db = FakeDb()
    with patch(
        "app.agents.jane_ads.google_ads_connection.request_manager_link",
        new=AsyncMock(return_value={"manager_link_status": "refused", "detail": "raw google error"}),
    ):
        result = _run(jane_google_ads_link_existing(
            GoogleAdsLinkExistingBody(customer_id="555"), db=db, brand_ctx=BRAND_CTX,
        ))
    assert result["manager_link_status"] == "refused"
    assert "already linked to another manager" in result["detail"]
    assert "Admin" in result["detail"]  # the actionable how-to-fix part


def test_link_existing_account_raises_409_when_not_connected():
    db = FakeDb()
    with patch(
        "app.agents.jane_ads.google_ads_connection.request_manager_link",
        new=AsyncMock(side_effect=AdsConnectionRequired(ConnectionState.NONE)),
    ):
        with pytest.raises(HTTPException) as exc:
            _run(jane_google_ads_link_existing(
                GoogleAdsLinkExistingBody(customer_id="555"), db=db, brand_ctx=BRAND_CTX,
            ))
    assert exc.value.status_code == 409
    assert "none" in exc.value.detail


def test_link_existing_account_raises_502_on_rest_failure():
    from app.agents.jane_ads.google_ads_connection import GoogleAdsConnectionError

    db = FakeDb()
    with patch(
        "app.agents.jane_ads.google_ads_connection.request_manager_link",
        new=AsyncMock(side_effect=GoogleAdsConnectionError("manager-link request: boom")),
    ):
        with pytest.raises(HTTPException) as exc:
            _run(jane_google_ads_link_existing(
                GoogleAdsLinkExistingBody(customer_id="555"), db=db, brand_ctx=BRAND_CTX,
            ))
    assert exc.value.status_code == 502
    assert "boom" in exc.value.detail


# ── create-account ────────────────────────────────────────────────────────────────

def test_create_account_returns_new_customer_id():
    db = FakeDb()
    with patch(
        "app.agents.jane_ads.google_ads_connection.create_client_account_under_mcc",
        new=AsyncMock(return_value={"customer_id": "777"}),
    ):
        result = _run(jane_google_ads_create_account(
            GoogleAdsCreateAccountBody(account_name="New Biz"), db=db, brand_ctx=BRAND_CTX,
        ))
    assert result == {"customer_id": "777"}


def test_create_account_raises_502_on_rest_failure():
    from app.agents.jane_ads.google_ads_connection import GoogleAdsConnectionError

    db = FakeDb()
    with patch(
        "app.agents.jane_ads.google_ads_connection.create_client_account_under_mcc",
        new=AsyncMock(side_effect=GoogleAdsConnectionError("create client account: boom")),
    ):
        with pytest.raises(HTTPException) as exc:
            _run(jane_google_ads_create_account(
                GoogleAdsCreateAccountBody(account_name="New Biz"), db=db, brand_ctx=BRAND_CTX,
            ))
    assert exc.value.status_code == 502
    assert "boom" in exc.value.detail


# ── connection/status (never raises) ──────────────────────────────────────────────

def test_connection_status_never_raises_even_when_none():
    db = FakeDb()
    with patch(
        "app.agents.jane_ads.google_ads_connection.resolve_connection_state",
        new=AsyncMock(return_value=(ConnectionState.NONE, None)),
    ), patch("app.agents.jane_ads.whatsapp.get_brand_whatsapp", new=AsyncMock(return_value="")):
        result = _run(jane_google_ads_connection_status(db=db, brand_ctx=BRAND_CTX))
    assert result["state"] == "none"


def test_connection_status_ready_includes_whatsapp_number():
    db = FakeDb()
    conn = {"account_name": "Acme", "customer_id": "555"}
    with patch(
        "app.agents.jane_ads.google_ads_connection.resolve_connection_state",
        new=AsyncMock(return_value=(ConnectionState.READY, conn)),
    ), patch("app.agents.jane_ads.whatsapp.get_brand_whatsapp", new=AsyncMock(return_value="2348031234567")):
        result = _run(jane_google_ads_connection_status(db=db, brand_ctx=BRAND_CTX))
    assert result["state"] == "ready"
    assert result["customer_id"] == "555"
    assert result["whatsapp_number"] == "2348031234567"
