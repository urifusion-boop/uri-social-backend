"""
Jane + Ads — per-brand Google Ads connection (Google adapter, Phase 1).

Mirrors ads_connection.py's SHAPE (an explicit ConnectionState enum — never a bare
boolean — one resolve function, one typed exception, a pre-flight gate checked BEFORE a
campaign is built, never at launch) but deliberately NOT its content, and deliberately
does not import from it: no cross-platform-module imports, so this module can be
reasoned about, tested, and changed without touching the live Meta path at all.

Non-negotiable product rule this module exists to enforce: every real Google Ads API
call operates against a specific brand's own customer_id, resolved per-brand via
resolve_customer_id_for_launch(). There is NO fallback to a shared/default account
anywhere in this file — that is the exact mistake ads_connection.py's
resolve_ads_page_for_launch makes with settings.META_ADS_PAGE_ID (a shared URI Page used
when a brand has no connection of its own), which this module is explicitly required not
to repeat. See tests/test_jane_ads_google_no_fallback_account.py for the CI guard.

Extends the EXISTING social_connections collection (platform="google_ads") rather than
inventing a parallel one — that collection already carries a generic, platform-
parameterized envelope reused across LinkedIn/X/TikTok/Instagram/Meta-ads connections.
The OAuth grant itself (initiate/callback/finalize) lives in router.py, as new, purely
additive endpoints under /jane-ads/google/*; this module is the state machine + token
handling underneath it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import httpx

from app.core.config import settings

CONNECTIONS = "social_connections"

_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Google Ads API's REST surface — https://googleads.googleapis.com/{version}/...
# (REST, not the official gRPC client library — see the Phase-1 plan for why).
_API_VERSION = lambda: settings.GOOGLE_ADS_API_VERSION  # noqa: E731 — read live, not at import time

# Refresh a bit before actual expiry so a request never races a token that expires
# mid-flight.
_TOKEN_REFRESH_MARGIN_SECONDS = 120


class ConnectionState(str, Enum):
    NONE = "none"                                  # no google_ads connection of any kind
    OAUTH_PENDING = "oauth_pending"                 # OAuth doc stored, not yet finalized to a brand
    NO_WHATSAPP = "no_whatsapp"                     # connection healthy but no brand WhatsApp number saved
    NEEDS_ACCOUNT_SELECTION = "needs_account_selection"  # OAuth finalized, but no customer_id chosen yet —
                                                     # distinct from MANAGER_LINK_PENDING: nothing has been
                                                     # sent to Google to wait on yet, the brand hasn't picked
                                                     # link-existing vs. create-new
    MANAGER_LINK_PENDING = "manager_link_pending"   # link request sent, awaiting client accept
    MANAGER_LINK_REFUSED = "manager_link_refused"   # client account already linked to another manager
    EXPIRED = "expired"                             # refresh token invalid/revoked
    READY = "ready"                                 # customer_id + active manager link + refreshable token


class AdsConnectionRequired(Exception):
    """Raised when a Google campaign can't be built because the brand's Google Ads
    connection isn't READY. Carries the specific state so the caller can map it to the
    matching prompt — never a generic error."""

    def __init__(self, state: ConnectionState, account_name: str = "") -> None:
        super().__init__(state.value)
        self.state = state
        self.account_name = account_name


class GoogleAdsConnectionError(Exception):
    """An OAuth token exchange/refresh call, or a manager-link/create-account call,
    returned an error Google didn't give us a more specific typed state for."""


def _brand_scope(user_id: Optional[str], brand_id: Optional[str]) -> dict:
    """Same scoping rule ads_connection.py uses — copied locally rather than imported,
    since this module deliberately never depends on the Meta-specific one."""
    if brand_id and not str(brand_id).startswith("brnd_personal_"):
        return {"brand_id": brand_id}
    return {"$or": [{"user_id": user_id}, {"brand_id": brand_id}]} if brand_id else {"user_id": user_id}


def _raise_for_error(data: dict, context: str) -> None:
    if "error" in data:
        err = data["error"]
        detail = err.get("message") or "unknown error"
        details = err.get("details") or []
        if details:
            inner = (details[0].get("errors") or [{}])[0]
            detail = inner.get("message") or detail
        raise GoogleAdsConnectionError(f"{context}: {detail}")


async def get_google_ads_connection(db, user_id: Optional[str], brand_id: Optional[str]) -> Optional[dict]:
    """The brand's ACTIVE google_ads connection — the one that carries a usable
    refresh_token. Mirrors ads_connection.get_ads_connection."""
    if db is None or not (user_id or brand_id):
        return None
    return await db[CONNECTIONS].find_one(
        {"platform": "google_ads", "connection_status": "active", **_brand_scope(user_id, brand_id)},
        sort=[("connected_at", -1)],
    )


async def _get_any_connection(db, user_id: Optional[str], brand_id: Optional[str]) -> Optional[dict]:
    """Same brand scope, WITHOUT the connection_status=active filter — needed to
    distinguish OAUTH_PENDING (doc exists, not finalized yet) from NONE (nothing at
    all)."""
    if db is None or not (user_id or brand_id):
        return None
    return await db[CONNECTIONS].find_one(
        {"platform": "google_ads", **_brand_scope(user_id, brand_id)},
        sort=[("connected_at", -1)],
    )


async def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """One-shot code->tokens exchange, right after the OAuth consent redirect. The
    authorize URL (built in router.py) MUST include access_type=offline&prompt=consent
    or Google won't reliably return a refresh_token — a common Google OAuth gotcha,
    not optional here since this connection has to survive far longer than one
    request."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(_TOKEN_ENDPOINT, data={
            "code": code,
            "client_id": settings.GOOGLE_ADS_CLIENT_ID,
            "client_secret": settings.GOOGLE_ADS_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
    data = resp.json()
    _raise_for_error(data, "Google Ads OAuth code exchange")
    return data


async def refresh_access_token(db, conn_doc: dict) -> dict:
    """Exchanges the stored refresh_token for a fresh access_token and persists it.
    This doubles as the live health check in resolve_connection_state — Google has no
    separate 'verify scopes' endpoint the way Meta's /me/permissions does, so a
    successful refresh IS the proof the connection still works. Raises
    GoogleAdsConnectionError on failure (invalid_grant = revoked/expired refresh token
    is the most common real case); the caller maps that to ConnectionState.EXPIRED."""
    refresh_token = conn_doc.get("refresh_token", "")
    if not refresh_token:
        raise GoogleAdsConnectionError("no refresh_token stored on this connection")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(_TOKEN_ENDPOINT, data={
            "refresh_token": refresh_token,
            "client_id": settings.GOOGLE_ADS_CLIENT_ID,
            "client_secret": settings.GOOGLE_ADS_CLIENT_SECRET,
            "grant_type": "refresh_token",
        })
    data = resp.json()
    if "error" in data:
        raise GoogleAdsConnectionError(f"token refresh failed: {data['error']}")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600)))
    await db[CONNECTIONS].update_one(
        {"id": conn_doc["id"]},
        {"$set": {
            "access_token": data["access_token"],
            "token_expires_at": expires_at,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return data


async def get_valid_access_token(db, conn_doc: dict) -> str:
    """Returns a still-valid access_token, refreshing first if it's expired or close
    to it. This is what resolve_customer_id_for_launch and adapters/google.py actually
    call — callers never read conn_doc["access_token"] directly."""
    expires_at = conn_doc.get("token_expires_at")
    if isinstance(expires_at, datetime):
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
        if remaining > _TOKEN_REFRESH_MARGIN_SECONDS:
            return conn_doc["access_token"]
    data = await refresh_access_token(db, conn_doc)
    return data["access_token"]


async def resolve_connection_state(
    db, user_id: Optional[str], brand_id: Optional[str], *, live_check: bool = True,
) -> tuple[ConnectionState, Optional[dict]]:
    """The OAuth/manager-link state machine (§2 of the Phase-1 plan). Deliberately does
    NOT consider the brand's WhatsApp number — that's a separate, orthogonal check
    (resolve_customer_id_for_launch checks it independently, same ordering as
    ads_connection.resolve_ads_page_for_launch: WhatsApp first, then this). Returns
    (state, connection_doc_or_None)."""
    conn = await _get_any_connection(db, user_id, brand_id)
    if not conn:
        return ConnectionState.NONE, None

    if conn.get("connection_status") != "active":
        return ConnectionState.OAUTH_PENDING, conn

    if conn.get("manager_link_status") == "refused":
        return ConnectionState.MANAGER_LINK_REFUSED, conn
    if not conn.get("customer_id"):
        # OAuth is done, but nothing has been sent to Google to wait on yet — the
        # brand hasn't chosen link-existing vs. create-new. Checked BEFORE the
        # "pending" branch below: a doc with manager_link_status="none" and no
        # customer_id is NOT the same as one where an invitation was actually sent.
        return ConnectionState.NEEDS_ACCOUNT_SELECTION, conn
    if conn.get("manager_link_status") in (None, "", "none", "pending"):
        return ConnectionState.MANAGER_LINK_PENDING, conn

    if live_check:
        try:
            await refresh_access_token(db, conn)
        except GoogleAdsConnectionError:
            return ConnectionState.EXPIRED, conn

    return ConnectionState.READY, conn


async def resolve_customer_id_for_launch(db, user_id: Optional[str], brand_id: Optional[str]) -> dict:
    """The pre-flight gate — called BEFORE any Google campaign plan is built, never at
    launch, so a client is never walked through a whole planning conversation only to
    hit a connection wall at the end (same discipline as
    ads_connection.resolve_ads_page_for_launch).

    Returns {customer_id, login_customer_id, access_token, whatsapp_number} or raises
    AdsConnectionRequired. There is NO trailing fallback return — unlike Meta's
    equivalent gate (see the module docstring above for that comparison), every path
    here either returns a real per-brand customer_id or raises. A Google Ads account
    is 1:1 per brand (created under, or linked to, URI's MCC) — there is no shared
    "default" account a client could silently fall back to."""
    from .whatsapp import get_brand_whatsapp

    wa_number = await get_brand_whatsapp(db, brand_id)
    if not wa_number:
        raise AdsConnectionRequired(ConnectionState.NO_WHATSAPP)

    state, conn = await resolve_connection_state(db, user_id, brand_id)
    if state != ConnectionState.READY:
        raise AdsConnectionRequired(state, (conn or {}).get("account_name", ""))

    access_token = await get_valid_access_token(db, conn)
    return {
        "customer_id": conn["customer_id"],
        "login_customer_id": conn.get("login_customer_id") or settings.GOOGLE_ADS_MCC_CUSTOMER_ID,
        "access_token": access_token,
        "whatsapp_number": wa_number,
    }


async def request_manager_link(
    db, user_id: Optional[str], brand_id: Optional[str], client_customer_id: str,
) -> dict:
    """Path (a) of the two connection paths: the client already has their own Google
    Ads account — send a manager-link INVITATION from URI's MCC. The client accepts it
    in their own Google Ads UI; nothing here can force that step (a separate
    pending-link-acceptance poll is out of scope for this module — see the Phase-1
    plan).

    REST shape follows Google Ads API's documented CustomerClientLinkService (POST
    customers/{mccCustomerId}/customerClientLinks:mutate, creating a
    CustomerClientLink with client_customer=resource name of the client account and
    status=PENDING) — NOT yet live-verified end-to-end (no MCC/developer token/OAuth
    client exist yet). Re-confirm exact field names against Google's current REST docs
    at first real use, same "shape not live-verified" caveat adapters/meta.py's header
    would carry for anything untested against a real Ad Account.

    On Google's specific "already linked to another manager" error (the known §3.6
    friction — a previous agency's link was never removed), stores WHY so
    resolve_connection_state reports MANAGER_LINK_REFUSED with a precise, actionable
    reason next time, instead of retrying blindly."""
    conn = await get_google_ads_connection(db, user_id, brand_id)
    if not conn:
        raise AdsConnectionRequired(ConnectionState.NONE)

    mcc_id = settings.GOOGLE_ADS_MCC_CUSTOMER_ID
    access_token = await get_valid_access_token(db, conn)
    api_base = f"https://googleads.googleapis.com/{_API_VERSION()}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": settings.GOOGLE_ADS_DEVELOPER_TOKEN,
        "login-customer-id": mcc_id,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{api_base}/customers/{mcc_id}/customerClientLinks:mutate",
            headers=headers,
            json={"operations": [{"create": {
                "clientCustomer": f"customers/{client_customer_id}",
                "status": "PENDING",
            }}]},
        )
    data = resp.json()
    if "error" in data:
        message = (data["error"].get("message") or "").lower()
        if "already" in message and ("link" in message or "manager" in message):
            await db[CONNECTIONS].update_one(
                {"id": conn["id"]},
                {"$set": {
                    "manager_link_status": "refused",
                    "manager_link_error": data["error"].get("message", ""),
                    "customer_id": client_customer_id,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
            return {"manager_link_status": "refused", "detail": data["error"].get("message", "")}
        _raise_for_error(data, "manager-link request")

    await db[CONNECTIONS].update_one(
        {"id": conn["id"]},
        {"$set": {
            "manager_link_status": "pending",
            "customer_id": client_customer_id,
            "login_customer_id": mcc_id,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return {"manager_link_status": "pending"}


async def create_client_account_under_mcc(
    db, user_id: Optional[str], brand_id: Optional[str], account_name: str,
) -> dict:
    """Path (b): the client has no Google Ads account — create one fresh under URI's
    MCC. A freshly-created child account is auto-linked (no separate accept step the
    way an invitation needs), so this sets manager_link_status="active" immediately.

    REST shape follows CustomerService.CreateCustomerClient (POST
    customers/{mccCustomerId}:createCustomerClient) — same "not yet live-verified"
    caveat as request_manager_link above."""
    conn = await get_google_ads_connection(db, user_id, brand_id)
    if not conn:
        raise AdsConnectionRequired(ConnectionState.NONE)

    mcc_id = settings.GOOGLE_ADS_MCC_CUSTOMER_ID
    access_token = await get_valid_access_token(db, conn)
    api_base = f"https://googleads.googleapis.com/{_API_VERSION()}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": settings.GOOGLE_ADS_DEVELOPER_TOKEN,
        "login-customer-id": mcc_id,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{api_base}/customers/{mcc_id}:createCustomerClient",
            headers=headers,
            json={"customerClient": {
                "descriptiveName": account_name,
                "currencyCode": "NGN",
                "timeZone": "Africa/Lagos",
            }},
        )
    data = resp.json()
    _raise_for_error(data, "create client account")
    resource_name = data.get("resourceName", "")
    new_customer_id = resource_name.split("/")[-1] if resource_name else ""

    await db[CONNECTIONS].update_one(
        {"id": conn["id"]},
        {"$set": {
            "customer_id": new_customer_id,
            "login_customer_id": mcc_id,
            "manager_link_status": "active",
            "created_account_by_uri": True,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return {"customer_id": new_customer_id}
