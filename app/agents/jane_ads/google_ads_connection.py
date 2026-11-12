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
The OAuth grant itself (initiate/callback) lives in router.py, as new, purely additive
endpoints under /jane-ads/google/*; this module is the state machine + token handling
underneath it.

IMPORTANT, live-confirmed on staging: Google Ads API calls do NOT authenticate as the
brand. CreateCustomerClient/CustomerClientLinkService require the OAuth identity to
already be a user on the MANAGER account (the MCC) — a brand's own personal Google
login has no relationship to URI's MCC and gets a hard "not associated with any Ads
accounts" error. This is Google's agency model, unlike Meta's per-user/per-page tokens.
So there is exactly ONE Google Ads OAuth grant in this whole system: URI's own admin
identity (whoever administers GOOGLE_ADS_MCC_CUSTOMER_ID), connected once via
/jane-ads/google/admin/connect/initiate and stored as a single fixed doc
(get_admin_connection/_ADMIN_CONN_ID below). Every real REST call — link-existing,
create-account, and eventually launch/spend/pause — authenticates as that identity.
A brand's own social_connections doc carries no token fields at all; it only tracks
customer_id/manager_link_status/account_name, i.e. which Ads account is theirs, not
how to authenticate as them (there is no "as them").
"""
from __future__ import annotations

import asyncio
import json
import secrets
import urllib.parse
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
    NONE = "none"                                  # brand hasn't clicked Connect yet
    NO_WHATSAPP = "no_whatsapp"                     # connection healthy but no brand WhatsApp number saved
    NEEDS_ACCOUNT_SELECTION = "needs_account_selection"  # connected, but no customer_id chosen yet —
                                                     # distinct from MANAGER_LINK_PENDING: nothing has been
                                                     # sent to Google to wait on yet, the brand hasn't picked
                                                     # link-existing vs. create-new
    MANAGER_LINK_PENDING = "manager_link_pending"   # link request sent, awaiting client accept
    MANAGER_LINK_REFUSED = "manager_link_refused"   # Google wouldn't let us invite this account at all
    MANAGER_LINK_DECLINED = "manager_link_declined"  # the invitation was declined/cancelled, or an
                                                     # accepted link was later removed in Google Ads
    READY = "ready"                                 # customer_id + active manager link

    # No OAUTH_PENDING/EXPIRED here — those described a per-brand OAuth token's own
    # lifecycle, which no longer exists (see module docstring: brands don't OAuth into
    # Google at all anymore). A dead admin token is a URI-wide ops problem, not a
    # per-brand state — see GoogleAdsConnectionError below instead.


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


class InvalidCustomerId(GoogleAdsConnectionError):
    """The customer ID the brand entered isn't a valid Google Ads Customer ID
    (wrong length once hyphens are stripped) — caught before any network call,
    so this is a client input problem, not a Google API failure. Kept as its
    own type so the router can return 400 with a clean message instead of the
    502 it uses for an actual rejected REST call."""


class AccountNotLinkable(GoogleAdsConnectionError):
    """Google refused the invitation for a reason the owner can act on (no such
    account, it's a manager account, it has no admin user...). A 400 with the
    reason, not a 502."""


class MccNotEligibleToCreateAccounts(GoogleAdsConnectionError):
    """CreateCustomerClient specifically (not linking) rejected with Google's
    account-creation eligibility gate — live-confirmed error text: "This manager
    account can't create new accounts. You'll need to link a Google Ads account
    that has spent more than $1,000 and has a history of policy compliance."

    This is a real Google anti-fraud restriction on brand-new Manager Accounts
    (MCCs), not a bug: Google won't let an MCC with no proven history mint fresh
    child accounts on demand, since that's exactly the pattern a spam/fraud
    operation would use to mass-produce disposable ad accounts. It has nothing to
    do with OAuth verification or developer-token access level (both confirmed
    separately approved) — it's gated purely on whether the MCC already has at
    least one linked account with real spend and clean policy history.

    Distinct from every other GoogleAdsConnectionError because the correct
    response isn't "retry" or "fix your credentials" — it's "link an existing,
    qualifying account instead (to any brand-new client, not necessarily this
    one), which both gets this brand onto Google Ads today AND is the one thing
    that unlocks CreateCustomerClient for every brand after that."
    """

    def __init__(self, message: str, newly_discovered: bool = False) -> None:
        super().__init__(message)
        # True only the first time this MCC is confirmed ineligible (the
        # stored flag actually flipped) — lets the caller alert operators
        # once per real discovery instead of on every subsequent attempt
        # that hits the same already-known restriction.
        self.newly_discovered = newly_discovered


# Google Ads REST error envelopes bury the machine-readable reason inside
# errors[].errorCode, whose actual KEY varies by error category (customerError,
# authorizationError, quotaError, ...) — there's no single fixed field name to
# read. Matching on the raw JSON text for either the enum Google is known to use
# for this (CREATION_DENIED_INELIGIBLE_MCC / CREATION_DENIED) or the stable
# human-readable phrase from the live-confirmed message is deliberately
# redundant: if Google ever changes the wording, the enum match still catches
# it, and vice versa.
_INELIGIBLE_MCC_SIGNATURES = (
    "ineligible_mcc",
    "creation_denied",
    "can't create new accounts",
    "cannot create new accounts",
    "can not create new accounts",
)


def _is_ineligible_mcc_error(raw_error_json: str) -> bool:
    lowered = raw_error_json.lower()
    return any(sig in lowered for sig in _INELIGIBLE_MCC_SIGNATURES)


def _brand_scope(user_id: Optional[str], brand_id: Optional[str]) -> dict:
    """Same scoping rule ads_connection.py uses — copied locally rather than imported,
    since this module deliberately never depends on the Meta-specific one."""
    if brand_id and not str(brand_id).startswith("brnd_personal_"):
        return {"brand_id": brand_id}
    return {"$or": [{"user_id": user_id}, {"brand_id": brand_id}]} if brand_id else {"user_id": user_id}


def _parse_json_response(resp: httpx.Response, context: str) -> dict:
    """Google's REST API should always return JSON, but a non-2xx from an upstream
    auth/gateway failure (bad developer token, MCC not actually linked, wrong
    customer id shape, etc.) can come back with an empty or non-JSON body — calling
    .json() directly on that raises an opaque JSONDecodeError that hides the actual
    HTTP status and body. Surface both here instead, so failures are diagnosable from
    the server log instead of a bare 'Expecting value: line 1 column 1' traceback."""
    try:
        return resp.json()
    except ValueError:
        raise GoogleAdsConnectionError(
            f"{context}: non-JSON response (HTTP {resp.status_code}): {resp.text[:500]!r}"
        )


def _raise_for_error(data: dict, context: str) -> None:
    """Two DIFFERENT Google error envelope shapes flow through here: the Ads REST
    API's ({"error": {"message": ..., "details": [...]}}, data["error"] a dict) and
    the OAuth2 TOKEN endpoint's standard shape ({"error": "invalid_grant",
    "error_description": "..."}, data["error"] a bare string) — exchange_code_for_tokens
    hits the latter. Assuming the dict shape unconditionally crashed with
    AttributeError: 'str' object has no attribute 'get' on any real OAuth failure
    (bad client secret, expired/reused code, etc.), which masked the actual reason
    instead of surfacing it — confirmed live on staging."""
    if "error" in data:
        err = data["error"]
        if isinstance(err, str):
            detail = data.get("error_description") or err
            raise GoogleAdsConnectionError(f"{context}: {detail}")
        detail = err.get("message") or "unknown error"
        details = err.get("details") or []
        if details:
            inner = (details[0].get("errors") or [{}])[0]
            detail = inner.get("message") or detail
        raise GoogleAdsConnectionError(f"{context}: {detail}")


async def get_google_ads_connection(db, user_id: Optional[str], brand_id: Optional[str]) -> Optional[dict]:
    """The brand's ACTIVE google_ads connection — note this carries NO token fields
    (see module docstring); it only tracks which Ads account is theirs. Mirrors
    ads_connection.get_ads_connection."""
    if db is None or not (user_id or brand_id):
        return None
    return await db[CONNECTIONS].find_one(
        {"platform": "google_ads", "connection_status": "active", **_brand_scope(user_id, brand_id)},
        sort=[("connected_at", -1)],
    )


_ADMIN_CONN_ID = "gads_admin"


async def get_admin_connection(db) -> Optional[dict]:
    """URI's own single Google Ads OAuth grant — the identity that actually has admin
    access to the MCC, and therefore the one every real REST call authenticates as
    (see module docstring). One fixed doc, not brand-scoped."""
    if db is None:
        return None
    return await db[CONNECTIONS].find_one({"id": _ADMIN_CONN_ID, "platform": "google_ads_admin"})


async def save_admin_connection(db, tokens: dict) -> None:
    """Persists the admin's tokens after the one-time OAuth consent
    (/jane-ads/google/admin/connect/callback). Reuses the exact doc shape
    get_valid_access_token/refresh_access_token already expect (id/access_token/
    refresh_token/token_expires_at), so those two functions work unchanged against
    this doc — only WHICH doc callers fetch first differs from the old per-brand
    flow."""
    now = datetime.now(timezone.utc)
    update: dict = {
        "id": _ADMIN_CONN_ID,
        "platform": "google_ads_admin",
        "access_token": tokens["access_token"],
        "token_expires_at": now + timedelta(seconds=int(tokens.get("expires_in", 3600))),
        "updated_at": now,
    }
    if tokens.get("refresh_token"):
        # Google only returns this on first consent (or prompt=consent, always set on
        # the admin authorize URL) — never overwrite a previously-stored one with a
        # blank on a routine refresh.
        update["refresh_token"] = tokens["refresh_token"]
    await db[CONNECTIONS].update_one({"id": _ADMIN_CONN_ID}, {"$set": update}, upsert=True)


async def set_mcc_creation_eligibility(db, eligible: bool, reason: str = "") -> bool:
    """Persists whether URI's MCC can currently mint fresh client accounts, onto
    the same single admin doc (URI has exactly one MCC — this is a URI-wide fact,
    never per-brand). Read back by the connection-status endpoint so the frontend
    can hide/show "Create one for me" based on a REAL, previously-confirmed
    result instead of optimistically trying it and eating the round trip on
    every brand-new client (see MccNotEligibleToCreateAccounts).

    Returns True when this is a NEW discovery (the stored value actually
    changed) — the caller uses this to decide whether an ops alert is
    warranted, so a string of identical failed attempts doesn't spam the same
    alert repeatedly.
    """
    existing = await get_admin_connection(db)
    previous = (existing or {}).get("mcc_can_create_accounts")
    changed = previous != eligible
    await db[CONNECTIONS].update_one(
        {"id": _ADMIN_CONN_ID},
        {"$set": {
            "mcc_can_create_accounts": eligible,
            "mcc_eligibility_checked_at": datetime.now(timezone.utc),
            "mcc_eligibility_failure_reason": reason if not eligible else "",
        }},
        upsert=True,
    )
    return changed


async def get_admin_valid_access_token(db) -> str:
    """The token every real Google Ads REST call actually authenticates as. Raises
    GoogleAdsConnectionError with a clear, actionable message if the admin identity
    has never been connected — a one-time ops action entirely separate from any
    brand's own connection flow, so this is never a per-brand ConnectionState."""
    conn = await get_admin_connection(db)
    if not conn:
        raise GoogleAdsConnectionError(
            "Google Ads isn't set up yet — an admin needs to connect the manager "
            "account via GET /jane-ads/google/admin/connect/initiate first."
        )
    return await get_valid_access_token(db, conn)


async def get_any_google_ads_connection(db, user_id: Optional[str], brand_id: Optional[str]) -> Optional[dict]:
    """Same brand scope, WITHOUT the connection_status=active filter. Brand docs are
    always active from creation now (no OAuth roundtrip to wait on), so this mostly
    matters for future connection_status values, not a pending state today."""
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
    data = _parse_json_response(resp, "Google Ads OAuth code exchange")
    _raise_for_error(data, "Google Ads OAuth code exchange")
    return data


async def refresh_access_token(db, conn_doc: dict) -> dict:
    """Exchanges the stored refresh_token for a fresh access_token and persists it —
    generic over WHICH doc (the single admin doc today; nothing else carries tokens
    any more, see module docstring). Raises GoogleAdsConnectionError on failure
    (invalid_grant = revoked/expired refresh token is the most common real case)."""
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
    data = _parse_json_response(resp, "Google Ads token refresh")
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
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
        if remaining > _TOKEN_REFRESH_MARGIN_SECONDS:
            return conn_doc["access_token"]
    data = await refresh_access_token(db, conn_doc)
    return data["access_token"]


async def resolve_connection_state(
    db, user_id: Optional[str], brand_id: Optional[str],
) -> tuple[ConnectionState, Optional[dict]]:
    """The manager-link state machine (§2 of the Phase-1 plan). Deliberately does NOT
    consider the brand's WhatsApp number — that's a separate, orthogonal check
    (resolve_customer_id_for_launch checks it independently, same ordering as
    ads_connection.resolve_ads_page_for_launch: WhatsApp first, then this). No
    live_check param — a brand doc carries no token to health-check any more (see
    module docstring); the one thing that CAN go dead is the shared admin token,
    which every real call already surfaces as a clear GoogleAdsConnectionError on its
    own, not a per-brand state. Returns (state, connection_doc_or_None)."""
    conn = await get_any_google_ads_connection(db, user_id, brand_id)
    if not conn:
        return ConnectionState.NONE, None

    if conn.get("manager_link_status") == "refused":
        return ConnectionState.MANAGER_LINK_REFUSED, conn
    if conn.get("manager_link_status") in _ENDED_LINK_STATUSES and conn.get("customer_id"):
        return ConnectionState.MANAGER_LINK_DECLINED, conn
    if not conn.get("customer_id"):
        # Connected, but nothing has been sent to Google to wait on yet — the brand
        # hasn't chosen link-existing vs. create-new. Checked BEFORE the "pending"
        # branch below: a doc with manager_link_status="none" and no customer_id is
        # NOT the same as one where an invitation was actually sent.
        return ConnectionState.NEEDS_ACCOUNT_SELECTION, conn
    if conn.get("manager_link_status") in (None, "", "none", "pending"):
        return ConnectionState.MANAGER_LINK_PENDING, conn

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

    # Checked against Google, not just our record: a link the owner removed in
    # Google Ads since we last looked must stop a launch here, not fail mid-way.
    state, conn, _ = await refresh_connection(db, user_id, brand_id, force=True)
    if state != ConnectionState.READY:
        raise AdsConnectionRequired(state, (conn or {}).get("account_name", ""))

    # Authenticates as URI's own admin identity, not the brand's — see module
    # docstring. GoogleAdsConnectionError here means the admin was never connected or
    # the refresh token died; a URI-wide ops problem, not something this brand caused.
    access_token = await get_admin_valid_access_token(db)
    return {
        "customer_id": conn["customer_id"],
        "login_customer_id": conn.get("login_customer_id") or settings.GOOGLE_ADS_MCC_CUSTOMER_ID,
        "access_token": access_token,
        "whatsapp_number": wa_number,
        # Budgets must be sent in the account's own currency (see adapters/google.budget_micros).
        "currency_code": conn.get("currency_code") or "",
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
    status=PENDING). Authenticates as URI's own admin identity (see module docstring)
    — NOT the brand's, since only an identity that's already a user on the MCC can
    call this at all.

    On Google's specific "already linked to another manager" error (the known §3.6
    friction — a previous agency's link was never removed), stores WHY so
    resolve_connection_state reports MANAGER_LINK_REFUSED with a precise, actionable
    reason next time, instead of retrying blindly."""
    # Google Ads resource names take a plain digit string (customers/9297032641),
    # but Google's OWN UI displays customer IDs hyphenated (929-703-2641) — exactly
    # the format the account-linking input's own placeholder suggests, and exactly
    # what a user would naturally copy-paste from their Google Ads dashboard.
    # Confirmed live: sending the hyphenated form produced Google's own "part of
    # the resource name is invalid" error. Accept either form (with or without
    # hyphens) — strip whatever's there, then validate what's left, so both a
    # clean 10-digit paste and the dashed display format work identically, and
    # anything else fails fast with a clear message instead of a wasted round
    # trip to Google's API. Stripped once here, at the single point this ever
    # enters the system, so the customer_id stored on the connection doc (and
    # therefore every later Google Ads API call that reads it back) is always
    # the clean, API-compatible form — not just this one request.
    client_customer_id = "".join(ch for ch in client_customer_id if ch.isdigit())
    if len(client_customer_id) != 10:
        raise InvalidCustomerId(
            "That doesn't look like a valid Google Ads Customer ID — it should be "
            "10 digits, e.g. 123-456-7890 (hyphens are fine, they're just ignored)."
        )

    conn = await get_google_ads_connection(db, user_id, brand_id)
    if not conn:
        raise AdsConnectionRequired(ConnectionState.NONE)

    mcc_id = settings.GOOGLE_ADS_MCC_CUSTOMER_ID
    access_token = await get_admin_valid_access_token(db)
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
            # CustomerClientLinkService's RPC is MutateCustomerClientLink —
            # singular, unlike bulk services (MutateCampaigns etc.) — and its
            # request field is "operation" (singular, a create/update union),
            # not the plural "operations" array most other mutate endpoints
            # use. Confirmed live: sending "operations" produced Google's own
            # "Unknown name 'operations': Cannot find field" error.
            json={"operation": {"create": {
                "clientCustomer": f"customers/{client_customer_id}",
                "status": "PENDING",
            }}},
        )
    data = _parse_json_response(resp, "manager-link request")
    if "error" in data and isinstance(data["error"], dict):
        raw = json.dumps(data["error"]).upper()
        # Google's own ManagerLinkError codes say exactly what happened. Reading
        # only the English message lumped "you already invited this account" in
        # with "another manager has it" and told the owner to remove a manager
        # that didn't exist.
        if "ALREADY_INVITED_BY_THIS_MANAGER" in raw:
            # An invitation from us is already waiting — that IS the pending state.
            await db[CONNECTIONS].update_one({"id": conn["id"]}, {"$set": {
                "manager_link_status": "pending", "manager_link_error": "",
                "customer_id": client_customer_id, "login_customer_id": mcc_id,
                "updated_at": datetime.now(timezone.utc)}})
            return {"manager_link_status": "pending"}
        if "ALREADY_MANAGED_BY_THIS_MANAGER" in raw or "ALREADY_MANAGED_IN_HIERARCHY" in raw:
            # Already linked to us — record it and read the real state back.
            await db[CONNECTIONS].update_one({"id": conn["id"]}, {"$set": {
                "manager_link_status": "pending", "manager_link_error": "",
                "customer_id": client_customer_id, "login_customer_id": mcc_id,
                "updated_at": datetime.now(timezone.utc)}})
            synced = await sync_manager_link(db, {**conn, "customer_id": client_customer_id})
            return {"manager_link_status": synced.get("manager_link_status", "pending")}
        for code, reason in _NOT_LINKABLE.items():
            if code in raw:
                raise AccountNotLinkable(reason)
        if "TOO_MANY_MANAGERS" in raw:
            reason = ("This Google Ads account already has the maximum number of managers. Remove one in "
                      "Google Ads (Admin → Access and security → Managers), then try again.")
            await db[CONNECTIONS].update_one({"id": conn["id"]}, {"$set": {
                "manager_link_status": "refused", "manager_link_error": reason,
                "customer_id": client_customer_id, "updated_at": datetime.now(timezone.utc)}})
            return {"manager_link_status": "refused", "detail": reason, "reason": reason}
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
            "manager_link_error": "",
            "manager_link_resource": ((data.get("result") or {}).get("resourceName") or ""),
            "customer_id": client_customer_id,
            "login_customer_id": mcc_id,
            "invited_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return {"manager_link_status": "pending"}


# ── Keeping our record true to Google's ──────────────────────────────────────
# An invitation is accepted (or declined) in the client's own Google Ads, so the
# only way to know is to ask. Nothing used to: a brand that linked an existing
# account stayed "waiting for you to accept" forever, even after accepting.

_ENDED_LINK_STATUSES = ("declined", "cancelled", "inactive")
_GOOGLE_LINK_STATUS = {"ACTIVE": "active", "PENDING": "pending", "REFUSED": "declined",
                       "CANCELED": "cancelled", "INACTIVE": "inactive"}
_LINK_PRIORITY = {"active": 0, "pending": 1, "declined": 2, "cancelled": 3, "inactive": 4}
_NOT_LINKABLE = {
    "CUSTOMER_NOT_FOUND": "We couldn't find a Google Ads account with that Customer ID. Check the number at the "
                          "top right of your Google Ads dashboard.",
    "INVALID_CUSTOMER_ID": "We couldn't find a Google Ads account with that Customer ID. Check the number at the "
                           "top right of your Google Ads dashboard.",
    "CUSTOMER_NOT_ENABLED": "That Google Ads account isn't active (it may be cancelled or suspended). Reactivate it "
                            "in Google Ads, or use a different account.",
    "CLIENT_HAS_NO_ADMIN_USER": "That Google Ads account has no admin user to accept the request. Add an admin in "
                                "Google Ads (Admin → Access and security), then try again.",
    "ACCOUNTS_NOT_COMPATIBLE_FOR_LINKING": "That account can't be linked — it looks like a manager or test account. "
                                           "Use the Customer ID of the Google Ads account that runs your ads.",
    "CUSTOMER_CANNOT_MANAGE_SELF": "That's URI's own manager account. Enter the Customer ID of your own Google Ads "
                                   "account.",
    "TOO_MANY_INVITES": "That Google Ads account has too many pending manager requests. Clear some in Google Ads "
                        "(Admin → Access and security → Managers), then try again.",
}
SYNC_MIN_INTERVAL_SECONDS = 20


def _mcc_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "developer-token": settings.GOOGLE_ADS_DEVELOPER_TOKEN,
        "login-customer-id": settings.GOOGLE_ADS_MCC_CUSTOMER_ID,
    }


async def _mcc_call(db, path: str, body: dict, context: str) -> dict:
    """One REST call against URI's manager account, as the admin identity."""
    access_token = await get_admin_valid_access_token(db)
    url = f"https://googleads.googleapis.com/{_API_VERSION()}/customers/{settings.GOOGLE_ADS_MCC_CUSTOMER_ID}{path}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, headers=_mcc_headers(access_token), json=body)
    data = _parse_json_response(resp, context)
    _raise_for_error(data, context)
    return data


async def _mcc_search(db, query: str, context: str) -> list:
    data = await _mcc_call(db, "/googleAds:search", {"query": query}, context)
    return data.get("results") or []


async def sync_manager_link(db, conn: dict) -> dict:
    """Ask Google what the link between URI's manager account and this brand's
    Ads account really is, and store it: accepted → active (with the account's
    own name), still waiting → pending, declined/cancelled/removed → ended.
    Returns the updated connection doc. Raises GoogleAdsConnectionError if
    Google can't be asked right now (the stored state is left as it was)."""
    customer_id = "".join(ch for ch in str(conn.get("customer_id") or "") if ch.isdigit())
    if not customer_id:
        return conn
    rows = await _mcc_search(
        db,
        "SELECT customer_client_link.resource_name, customer_client_link.status, "
        "customer_client_link.manager_link_id FROM customer_client_link "
        f"WHERE customer_client_link.client_customer = 'customers/{customer_id}'",
        "manager-link status",
    )
    now = datetime.now(timezone.utc)
    update: dict = {"manager_link_synced_at": now, "updated_at": now}
    links = []
    for row in rows:
        link = row.get("customerClientLink") or {}
        status = _GOOGLE_LINK_STATUS.get(str(link.get("status") or "").upper())
        if status:
            links.append((status, link))
    if links:
        # An account can carry an old cancelled invitation next to a live one —
        # the live one is what counts.
        status, link = sorted(links, key=lambda x: (_LINK_PRIORITY[x[0]], -int(x[1].get("managerLinkId") or 0)))[0]
        update["manager_link_status"] = status
        update["manager_link_resource"] = link.get("resourceName") or ""
        if status == "active":
            update["manager_link_error"] = ""
            try:
                clients = await _mcc_search(
                    db,
                    "SELECT customer_client.descriptive_name, customer_client.currency_code, "
                    "customer_client.time_zone, customer_client.manager, customer_client.test_account "
                    f"FROM customer_client WHERE customer_client.id = {customer_id}",
                    "linked account details",
                )
                info = (clients[0].get("customerClient") if clients else None) or {}
                update["account_name"] = info.get("descriptiveName") or conn.get("account_name") or ""
                update["currency_code"] = info.get("currencyCode") or ""
                update["time_zone"] = info.get("timeZone") or ""
                update["linked_at"] = conn.get("linked_at") or now
            except GoogleAdsConnectionError as e:   # the link is real even if the name lookup isn't
                print(f"[GoogleAds] linked account details unavailable: {e}")
    elif conn.get("manager_link_status") == "pending":
        # We believe an invitation is out, Google has none: it was withdrawn.
        update["manager_link_status"] = "cancelled"
    await db[CONNECTIONS].update_one({"id": conn["id"]}, {"$set": update})
    return {**conn, **update}


async def refresh_connection(db, user_id: Optional[str], brand_id: Optional[str], force: bool = False
                             ) -> tuple[ConnectionState, Optional[dict], str]:
    """The connection state, checked against Google when it could have changed:
    while an invitation is pending (the owner accepts it in Google Ads, not
    here), or whenever the caller asks (force — the "Check again" button).
    Returns (state, doc, sync_error) — sync_error is a plain reason when Google
    couldn't be asked, in which case the stored state is what's returned."""
    state, conn = await resolve_connection_state(db, user_id, brand_id)
    if not conn or not conn.get("customer_id"):
        return state, conn, ""
    waiting = state == ConnectionState.MANAGER_LINK_PENDING
    if not (waiting or force):
        return state, conn, ""
    last = conn.get("manager_link_synced_at")
    if last is not None and not force:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - last).total_seconds() < SYNC_MIN_INTERVAL_SECONDS:
            return state, conn, ""
    try:
        await sync_manager_link(db, conn)
    except GoogleAdsConnectionError as e:
        print(f"[GoogleAds] could not check link status: {e}")
        return state, conn, "We couldn't reach Google Ads to check just now — try again in a minute."
    state, conn = await resolve_connection_state(db, user_id, brand_id)
    return state, conn, ""


async def release_account(db, user_id: Optional[str], brand_id: Optional[str], remove: bool = False) -> dict:
    """Change account / disconnect. Ends the link on Google's side too — a
    pending invitation is withdrawn, an accepted link is removed — so URI never
    keeps access to an account the brand has let go of. An account URI created
    for the brand is left linked (unlinking would orphan it with no owner), and
    can't be released from here.

    remove=False: back to "choose your account". remove=True: Google Ads is
    disconnected for this brand altogether."""
    conn = await get_any_google_ads_connection(db, user_id, brand_id)
    if not conn:
        raise AdsConnectionRequired(ConnectionState.NONE)
    if conn.get("created_account_by_uri") and conn.get("customer_id"):
        raise AccountNotLinkable("This Google Ads account was created for you by URI, so it can't be swapped "
                                 "out here. Contact support if you need to move to a different account.")
    google_note = ""
    if conn.get("customer_id") and conn.get("manager_link_status") in ("pending", "active"):
        try:
            fresh = await sync_manager_link(db, conn)
            resource, status = fresh.get("manager_link_resource"), fresh.get("manager_link_status")
            if resource and status in ("pending", "active"):
                await _mcc_call(db, "/customerClientLinks:mutate", {"operation": {
                    "update": {"resourceName": resource, "status": "CANCELED" if status == "pending" else "INACTIVE"},
                    "updateMask": "status",
                }}, "end manager link")
        except GoogleAdsConnectionError as e:
            # Our side is still released; say plainly that Google's may not be.
            print(f"[GoogleAds] could not end the link at Google: {e}")
            google_note = ("We couldn't remove the link on Google's side just now. You can remove URI in Google "
                           "Ads under Admin → Access and security → Managers.")
    now = datetime.now(timezone.utc)
    if remove:
        await db[CONNECTIONS].delete_one({"id": conn["id"]})
    else:
        await db[CONNECTIONS].update_one({"id": conn["id"]}, {"$set": {
            "customer_id": "", "manager_link_status": "none", "manager_link_error": "",
            "manager_link_resource": "", "account_name": "", "currency_code": "", "time_zone": "",
            "updated_at": now}})
    return {"released": True, "removed": remove, "google_note": google_note}


async def create_client_account_under_mcc(
    db, user_id: Optional[str], brand_id: Optional[str], account_name: str,
) -> dict:
    """Path (b): the client has no Google Ads account — create one fresh under URI's
    MCC. A freshly-created child account is auto-linked (no separate accept step the
    way an invitation needs), so this sets manager_link_status="active" immediately.

    REST shape follows CustomerService.CreateCustomerClient (POST
    customers/{mccCustomerId}:createCustomerClient). Authenticates as URI's own admin
    identity, same reasoning as request_manager_link above — live-confirmed on
    staging that the brand's own OAuth token gets a hard NOT_ADS_USER error here."""
    conn = await get_google_ads_connection(db, user_id, brand_id)
    if not conn:
        raise AdsConnectionRequired(ConnectionState.NONE)

    mcc_id = settings.GOOGLE_ADS_MCC_CUSTOMER_ID
    access_token = await get_admin_valid_access_token(db)
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
    data = _parse_json_response(resp, "create client account")
    if "error" in data and _is_ineligible_mcc_error(json.dumps(data.get("error", {}))):
        reason = (
            "This manager account can't create new accounts yet — it needs at least one linked "
            "account with real ad spend and a clean policy history first."
        )
        newly_discovered = await set_mcc_creation_eligibility(db, False, reason)
        raise MccNotEligibleToCreateAccounts(reason, newly_discovered=newly_discovered)
    _raise_for_error(data, "create client account")
    resource_name = data.get("resourceName", "")
    new_customer_id = resource_name.split("/")[-1] if resource_name else ""

    # A real success is proof-positive the MCC IS eligible — clears the gate
    # for every brand after this one, not just this brand, without needing a
    # separate manual bootstrap step to be remembered/run.
    await set_mcc_creation_eligibility(db, True)

    await db[CONNECTIONS].update_one(
        {"id": conn["id"]},
        {"$set": {
            "customer_id": new_customer_id,
            "login_customer_id": mcc_id,
            "manager_link_status": "active",
            "currency_code": "NGN",   # as created above
            "created_account_by_uri": True,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return {"customer_id": new_customer_id}


# ── "Continue with Google": pick your account, no Customer ID, no admin screens ──
# The owner signs in to Google once. We read which Google Ads accounts they
# manage (by name), they pick one, URI's manager account sends the link request
# and — because the owner is signed in — we accept it on their behalf with
# THEIR token. They never look up a Customer ID or open Google Ads.
#
# This does NOT bring back per-brand API access (see the module docstring: every
# real Ads call still authenticates as URI's admin identity). The owner's token
# is used only to list their accounts and accept the link, is kept at most an
# hour on the connection doc, has no refresh token, and is deleted the moment
# an account is linked.

OAUTH_STATES = "jane_ads_google_oauth_states"
_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_ADWORDS_SCOPE = "https://www.googleapis.com/auth/adwords"
SIGNIN_STATE_TTL_MINUTES = 15
_ACCEPT_SETTLE_ATTEMPTS = 5
_ACCEPT_SETTLE_SECONDS = 1.5
SIGNIN_TOKEN_TTL_MINUTES = 50
_MAX_ACCOUNTS_LISTED = 25


def oauth_redirect_uri() -> str:
    """The ONE redirect URI registered with Google for this OAuth client — the
    admin connect and the owner sign-in share it, told apart by `state`."""
    base = (settings.PUBLIC_API_URL or settings.URI_GATEWAY_BASE_API_URL).rstrip("/")
    return f"{base}/jane-ads/google/admin/connect/callback"


def safe_return_to(url: str) -> str:
    """Where to send the browser after Google — only ever back into our own app."""
    fallback = (settings.WEB_APP_URL or "").strip("'\"").rstrip("/") or "https://www.urisocial.com"
    try:
        parsed = urllib.parse.urlparse(url or "")
    except ValueError:
        return fallback
    host = (parsed.hostname or "").lower()
    ours = host == "urisocial.com" or host.endswith(".urisocial.com") or host in ("localhost", "127.0.0.1")
    if parsed.scheme in ("https", "http") and ours and (parsed.scheme == "https" or host in ("localhost", "127.0.0.1")):
        return f"{parsed.scheme}://{parsed.netloc}"
    return fallback


async def ensure_brand_connection(db, user_id: Optional[str], brand_id: Optional[str]) -> dict:
    """The brand's google_ads connection doc, created if this is the first time."""
    conn = await get_any_google_ads_connection(db, user_id, brand_id)
    if conn:
        return conn
    now = datetime.now(timezone.utc)
    doc = {
        "id": f"gads_{secrets.token_hex(6)}", "user_id": user_id, "brand_id": brand_id,
        "platform": "google_ads", "connected_via": "google_ads_direct", "customer_id": "",
        "manager_link_status": "none", "account_name": "", "created_account_by_uri": False,
        "connection_status": "active", "connected_at": now, "updated_at": now,
    }
    await db[CONNECTIONS].insert_one(dict(doc))
    return doc


async def start_signin(db, user_id: Optional[str], brand_id: Optional[str], return_to: str) -> str:
    """The Google sign-in URL for this brand's owner. `state` is a one-time
    random value we keep server-side, so the callback knows which brand it is
    for and can't be replayed or forged."""
    if not settings.GOOGLE_ADS_CLIENT_ID:
        raise GoogleAdsConnectionError("Google sign-in isn't configured yet.")
    await ensure_brand_connection(db, user_id, brand_id)
    state = "b_" + secrets.token_urlsafe(24)
    await db[OAUTH_STATES].insert_one({
        "state": state, "user_id": user_id, "brand_id": brand_id,
        "return_to": safe_return_to(return_to), "created_at": datetime.now(timezone.utc), "used": False,
    })
    return _AUTH_ENDPOINT + "?" + urllib.parse.urlencode({
        "client_id": settings.GOOGLE_ADS_CLIENT_ID,
        "redirect_uri": oauth_redirect_uri(),
        "scope": _ADWORDS_SCOPE,
        "response_type": "code",
        "access_type": "online",          # no refresh token: we don't keep access
        "prompt": "select_account",
        "include_granted_scopes": "false",
        "state": state,
    })


async def take_signin_state(db, state: str) -> Optional[dict]:
    """The pending sign-in this `state` belongs to — once, and only while fresh."""
    doc = await db[OAUTH_STATES].find_one({"state": state})
    if not doc or doc.get("used"):
        return None
    created = doc.get("created_at")
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    await db[OAUTH_STATES].update_one({"state": state}, {"$set": {"used": True}})
    if created is None or datetime.now(timezone.utc) - created > timedelta(minutes=SIGNIN_STATE_TTL_MINUTES):
        return None
    return doc


def _user_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "developer-token": settings.GOOGLE_ADS_DEVELOPER_TOKEN}


async def list_user_accounts(access_token: str) -> list:
    """The Google Ads accounts the signed-in person can use, with their names.
    Accounts Google won't let us read (cancelled, suspended) are left out."""
    api = f"https://googleads.googleapis.com/{_API_VERSION()}"
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.get(f"{api}/customers:listAccessibleCustomers", headers=_user_headers(access_token))
        data = _parse_json_response(resp, "list Google Ads accounts")
        _raise_for_error(data, "list Google Ads accounts")
        accounts = []
        for resource in (data.get("resourceNames") or [])[:_MAX_ACCOUNTS_LISTED]:
            cid = resource.split("/")[-1]
            try:
                r = await client.post(
                    f"{api}/customers/{cid}/googleAds:search", headers=_user_headers(access_token),
                    json={"query": "SELECT customer.id, customer.descriptive_name, customer.currency_code, "
                                   "customer.manager, customer.test_account, customer.status FROM customer LIMIT 1"},
                )
                body = r.json()
            except Exception:
                continue
            if not isinstance(body, dict) or "error" in body:
                continue
            info = ((body.get("results") or [{}])[0]).get("customer") or {}
            accounts.append({
                "customer_id": cid,
                "name": info.get("descriptiveName") or "",
                "currency_code": info.get("currencyCode") or "",
                "is_manager": bool(info.get("manager")),
                "is_test": bool(info.get("testAccount")),
                "status": info.get("status") or "",
            })
    return accounts


def usable_accounts(accounts: list) -> list:
    """Accounts ads can actually run in: not a manager, not a test account, enabled."""
    mcc = "".join(ch for ch in str(settings.GOOGLE_ADS_MCC_CUSTOMER_ID or "") if ch.isdigit())
    return [a for a in accounts or []
            if not a.get("is_manager") and not a.get("is_test")
            and (a.get("status") or "ENABLED") == "ENABLED" and a.get("customer_id") != mcc]


def _fresh_signin(conn: Optional[dict]) -> Optional[dict]:
    signin = (conn or {}).get("google_signin") or None
    if not signin:
        return None
    at = signin.get("at")
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if at is None or datetime.now(timezone.utc) - at > timedelta(minutes=SIGNIN_TOKEN_TTL_MINUTES):
        return None
    return signin


async def accept_link_as_owner(access_token: str, customer_id: str) -> bool:
    """Accept URI's pending manager-link request from inside the owner's own
    account, using the owner's sign-in. True when it was accepted. False when
    there was nothing pending or the signed-in person isn't an admin of the
    account (the request then simply stays waiting — they can accept it in
    Google Ads, or an admin can)."""
    mcc = "".join(ch for ch in str(settings.GOOGLE_ADS_MCC_CUSTOMER_ID or "") if ch.isdigit())
    api = f"https://googleads.googleapis.com/{_API_VERSION()}"
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(
            f"{api}/customers/{customer_id}/googleAds:search", headers=_user_headers(access_token),
            json={"query": "SELECT customer_manager_link.resource_name, customer_manager_link.manager_customer, "
                           "customer_manager_link.status FROM customer_manager_link"},
        )
        data = _parse_json_response(r, "read manager requests")
        if "error" in data:
            print(f"[GoogleAds] owner could not read manager requests: {json.dumps(data['error'])[:300]}")
            return False
        pending = [
            (row.get("customerManagerLink") or {}) for row in (data.get("results") or [])
            if (row.get("customerManagerLink") or {}).get("managerCustomer") == f"customers/{mcc}"
            and (row.get("customerManagerLink") or {}).get("status") == "PENDING"
        ]
        if not pending:
            return False
        r = await client.post(
            f"{api}/customers/{customer_id}/customerManagerLinks:mutate", headers=_user_headers(access_token),
            json={"operations": [{"update": {"resourceName": pending[0]["resourceName"], "status": "ACTIVE"},
                                  "updateMask": "status"}]},
        )
        data = _parse_json_response(r, "accept manager request")
        if "error" in data:
            print(f"[GoogleAds] owner could not accept the request: {json.dumps(data['error'])[:300]}")
            return False
    return True


async def link_selected_account(db, user_id: Optional[str], brand_id: Optional[str], customer_id: str) -> dict:
    """The owner picked one of the accounts their Google sign-in showed: send
    the link request from URI's manager account, accept it as the owner, and
    read the result back from Google. Returns {state, auto_accepted, account_name}."""
    conn = await get_any_google_ads_connection(db, user_id, brand_id)
    signin = _fresh_signin(conn)
    if not conn or not signin:
        raise AccountNotLinkable("Your Google sign-in has expired. Press Continue with Google again.")
    customer_id = "".join(ch for ch in str(customer_id) if ch.isdigit())
    chosen = next((a for a in usable_accounts(signin.get("accounts")) if a["customer_id"] == customer_id), None)
    if not chosen:
        raise AccountNotLinkable("That isn't one of the Google Ads accounts your sign-in can use.")

    result = await request_manager_link(db, user_id, brand_id, customer_id)
    auto_accepted = False
    if result.get("manager_link_status") == "pending":
        try:
            auto_accepted = await accept_link_as_owner(signin["access_token"], customer_id)
        except GoogleAdsConnectionError as e:
            print(f"[GoogleAds] auto-accept unavailable: {e}")
    fresh = await get_any_google_ads_connection(db, user_id, brand_id) or conn
    if result.get("manager_link_status") != "refused":
        # Right after we approve it, Google can still answer "pending" for a few
        # seconds. Reading once told an owner whose link had just been approved
        # that "an admin needs to approve it" — then it flipped to linked on its
        # own (seen on the first real sign-in). So when we approved it ourselves,
        # give Google a moment to catch up before believing "pending".
        attempts = _ACCEPT_SETTLE_ATTEMPTS if auto_accepted else 1
        for attempt in range(attempts):
            try:
                fresh = await sync_manager_link(db, fresh)
            except GoogleAdsConnectionError as e:
                print(f"[GoogleAds] could not read the link back: {e}")
                break
            if fresh.get("manager_link_status") != "pending" or attempt == attempts - 1:
                break
            await asyncio.sleep(_ACCEPT_SETTLE_SECONDS)
    update: dict = {"updated_at": datetime.now(timezone.utc)}
    if auto_accepted:
        # So the app can say "approved, finishing" rather than "approve it" while
        # Google catches up (see owner_approval_settling).
        update["owner_approved_at"] = datetime.now(timezone.utc)
    if not fresh.get("account_name") and chosen.get("name"):
        update["account_name"] = chosen["name"]
    if not fresh.get("currency_code") and chosen.get("currency_code"):
        update["currency_code"] = chosen["currency_code"]
    if fresh.get("manager_link_status") == "active":
        # Linked: the owner's token has done its job. Don't keep it.
        await db[CONNECTIONS].update_one({"id": conn["id"]}, {"$set": {**update, "google_signin": None}})
    else:
        await db[CONNECTIONS].update_one({"id": conn["id"]}, {"$set": update})
    state, final = await resolve_connection_state(db, user_id, brand_id)
    return {"state": state.value, "auto_accepted": auto_accepted,
            "account_name": (final or {}).get("account_name", ""), "detail": result.get("reason", "")}


OWNER_APPROVAL_SETTLE_MINUTES = 3


def owner_approval_settling(conn: Optional[dict]) -> bool:
    """True while a link we approved with the owner's sign-in still reads as
    pending at Google — i.e. nothing is needed from anyone, it is just
    finishing. After a few minutes we stop assuming that and show the normal
    "approve it" steps, in case the approval didn't actually take."""
    if not conn or conn.get("manager_link_status") != "pending":
        return False
    at = conn.get("owner_approved_at")
    if at is None:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at < timedelta(minutes=OWNER_APPROVAL_SETTLE_MINUTES)


async def finish_signin(db, state_doc: dict, code: str) -> dict:
    """After Google sends the owner back: read their accounts and, when there
    is exactly one usable account, link it straight away. Returns
    {return_to, outcome, detail} — outcome is linked | pending | choose |
    no_accounts | error."""
    user_id, brand_id = state_doc.get("user_id"), state_doc.get("brand_id")
    return_to = state_doc.get("return_to") or safe_return_to("")
    try:
        tokens = await exchange_code_for_tokens(code, oauth_redirect_uri())
        accounts = await list_user_accounts(tokens["access_token"])
    except GoogleAdsConnectionError as e:
        print(f"[GoogleAds] sign-in could not be completed: {e}")
        return {"return_to": return_to, "outcome": "error",
                "detail": "We couldn't read your Google Ads accounts. Please try again."}
    conn = await ensure_brand_connection(db, user_id, brand_id)
    await db[CONNECTIONS].update_one({"id": conn["id"]}, {"$set": {
        "google_signin": {"access_token": tokens["access_token"], "accounts": accounts,
                          "at": datetime.now(timezone.utc)},
        "updated_at": datetime.now(timezone.utc),
    }})
    usable = usable_accounts(accounts)
    if not usable:
        return {"return_to": return_to, "outcome": "no_accounts", "detail": ""}
    if len(usable) > 1:
        return {"return_to": return_to, "outcome": "choose", "detail": ""}
    try:
        linked = await link_selected_account(db, user_id, brand_id, usable[0]["customer_id"])
    except GoogleAdsConnectionError as e:
        return {"return_to": return_to, "outcome": "error", "detail": str(e)}
    if linked["state"] == ConnectionState.READY.value:
        outcome = "linked"
    elif linked.get("auto_accepted"):
        outcome = "finishing"      # we approved it; Google just hasn't caught up yet
    else:
        outcome = "pending"        # the signed-in person couldn't approve it — an admin must
    return {"return_to": return_to, "outcome": outcome, "detail": linked.get("detail", "")}


async def identity_manages_mcc(access_token: str) -> bool:
    """Can this Google identity actually use URI's manager account? The admin
    connection may only ever be replaced by one that can — otherwise anyone who
    opened the admin connect link and signed in with their own Google account
    would overwrite it and take Google Ads down for every brand."""
    mcc = "".join(ch for ch in str(settings.GOOGLE_ADS_MCC_CUSTOMER_ID or "") if ch.isdigit())
    if not mcc:
        return False
    api = f"https://googleads.googleapis.com/{_API_VERSION()}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{api}/customers:listAccessibleCustomers", headers=_user_headers(access_token))
    data = _parse_json_response(resp, "check admin identity")
    if "error" in data:
        return False
    return f"customers/{mcc}" in (data.get("resourceNames") or [])


# ── A nudge when the request is still waiting a day later ────────────────────

REMIND_AFTER_HOURS = 20
REMIND_GIVE_UP_DAYS = 14


async def remind_pending_links(db, now: Optional[datetime] = None) -> dict:
    """Brands whose link request has waited about a day: check with Google
    first (it may have been accepted — then nothing is sent), and if it is
    still waiting, remind the owner ONCE by email and in the app, with the
    link back to Connected Accounts. Run hourly by the notification scheduler."""
    from app.services import brand_notifier

    now = now or datetime.now(timezone.utc)
    sent = accepted = checked = 0
    cursor = db[CONNECTIONS].find({"platform": "google_ads", "manager_link_status": "pending"})
    for conn in await cursor.to_list(length=500):
        if conn.get("link_reminder_sent_at") or not conn.get("customer_id"):
            continue
        invited = conn.get("invited_at") or conn.get("updated_at")
        if invited is None:
            continue
        if invited.tzinfo is None:
            invited = invited.replace(tzinfo=timezone.utc)
        age = now - invited
        if age < timedelta(hours=REMIND_AFTER_HOURS) or age > timedelta(days=REMIND_GIVE_UP_DAYS):
            continue
        checked += 1
        try:
            fresh = await sync_manager_link(db, conn)
        except GoogleAdsConnectionError as e:
            print(f"[GoogleAds] reminder: could not check {conn.get('id')}: {e}")
            continue
        if fresh.get("manager_link_status") != "pending":
            accepted += fresh.get("manager_link_status") == "active"
            continue
        user_id = conn.get("user_id")
        if not user_id:
            continue
        digits = "".join(ch for ch in str(conn.get("customer_id")) if ch.isdigit())
        shown = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}" if len(digits) == 10 else digits
        title = "One step left to run Google ads with URI"
        message = (
            f"Your Google Ads account {shown} is still waiting for one approval. The quickest way: open "
            "Connected Accounts and press Continue with Google — you sign in and it's done. "
            "Or approve URI in Google Ads under Admin → Access and security → Managers."
        )
        web = (settings.WEB_APP_URL or "").strip("'\"").rstrip("/") or "https://www.urisocial.com"
        link = f"{web}/workspace/?tab=connections"
        user = await db["users"].find_one({"userId": user_id}) or {}
        deliveries = {"dashboard": {"sent": True, "error": None}}
        if not user.get("notification_opt_out"):
            try:
                ok, error = await brand_notifier._send(db, "email", user, {}, user_id, title, message, link)
            except Exception as exc:
                ok, error = False, str(exc)
            deliveries["email"] = {"sent": ok, "error": error}
        await brand_notifier._log(db, user_id, "google_ads_link_reminder", title, message,
                                  {"brand_id": conn.get("brand_id"), "link": link}, deliveries)
        await db[CONNECTIONS].update_one({"id": conn["id"]}, {"$set": {"link_reminder_sent_at": now}})
        sent += 1
    return {"checked": checked, "reminded": sent, "already_accepted": accepted}
