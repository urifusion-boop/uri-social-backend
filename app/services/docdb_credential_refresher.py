"""
Keeps the Mongo connection in sync with DocumentDB's AWS-managed master
password rotation, proactively — so a rotation event is invisible to users
instead of an outage that's only discovered once login stops working.

Background: DocumentDB clusters with managed rotation enabled (see
MasterUserSecret on the cluster) have AWS silently rotate the real password
into a Secrets Manager secret on its own schedule. Nothing tells a
hand-maintained connection string (MONGODB_URI, sourced from SSM here) that
happened, so the app keeps authenticating with an increasingly stale
password until every DB operation starts failing with "Authentication
failed" — which has taken prod down twice.

Two halves make this permanent:
  1. STARTUP — `database.with_current_docdb_password()` swaps in the current
     password from Secrets Manager when the initial client is built, so a
     task that starts after a rotation authenticates even if the new
     password never reached SSM. (This module cannot help there: the app
     crashes in its startup index-creation before the first poll runs.)
  2. IN-FLIGHT — this module, for a rotation that lands while a task is
     already up and serving.

Deliberately a polling loop, not a reactive "retry on auth failure" wrapper:
retrying a whole request after a failed write risks re-running a handler
that already had a partial side effect (e.g. a credit already deducted
before a later query in the same handler hit the stale connection) —
duplicating it. A tight poll (default 30s) closes the exposure window to,
worst case, about one polling interval, with no risk of double-firing
anything, since it always runs AHEAD of requests rather than replaying them.

Opt-in via settings.DOCDB_SECRET_ARN — environments without managed
rotation (local dev, or any environment using a plain static password) are
completely unaffected; this module does nothing unless that's set.
"""
import asyncio
import re
from typing import Optional
from urllib.parse import quote_plus

from app.core.config import settings

# Matches mongodb://user:password@rest-of-the-uri — password is the only
# piece that ever changes on rotation; everything else (host, port, db,
# query params) is reused unchanged from the existing MONGODB_URI.
_URI_PATTERN = re.compile(r"^(mongodb(?:\+srv)?://)([^:]+):([^@]+)@(.+)$")

_last_known_password: Optional[str] = None


def _split_uri(uri: str):
    m = _URI_PATTERN.match(uri)
    if not m:
        raise ValueError("MONGODB_URI is not in the expected mongodb://user:pass@host... form")
    scheme, user, password, rest = m.groups()
    return scheme, user, password, rest


async def _fetch_current_password() -> str:
    """
    Reads the CURRENT password straight from the managed secret via boto3's
    default credential chain — on ECS that's the task's own IAM role, never
    a static key baked into the app. Run in a thread since boto3 is sync.
    """
    import boto3
    import json

    def _get():
        client = boto3.client("secretsmanager")
        resp = client.get_secret_value(SecretId=settings.DOCDB_SECRET_ARN)
        return json.loads(resp["SecretString"])["password"]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get)


async def _rebuild_client_if_changed() -> None:
    global _last_known_password

    current_password = await _fetch_current_password()
    if current_password == _last_known_password:
        return  # no rotation since last check — nothing to do

    is_first_check = _last_known_password is None
    _last_known_password = current_password

    if is_first_check:
        # Confirms the secret is reachable and matches what's already in use;
        # doesn't rebuild anything on this very first check — connect_to_mongo()
        # already built the initial client from MONGODB_URI at startup.
        return

    # A real rotation: same host/db/params, new password. Build fresh
    # clients and swap them in — get_db()/get_sdk_gateway_db() read
    # app.database.{client,sdk_gateway_client} fresh on every call, so every
    # NEW request picks this up immediately. The old clients are deliberately
    # left to idle out on their own rather than force-closed, so any
    # operation already in flight (using the old password, still valid
    # during DocumentDB's rotation grace period) finishes without being cut
    # off.
    from motor.motor_asyncio import AsyncIOMotorClient
    from app import database as db_module

    def _swap(uri: str) -> str:
        scheme, user, _old, rest = _split_uri(uri)
        return f"{scheme}{user}:{quote_plus(current_password)}@{rest}"

    db_module.client = AsyncIOMotorClient(_swap(settings.MONGODB_URI))
    # The SDK-gateway connection uses the same DocumentDB cluster + master
    # user, so it rotates in lockstep — refresh it too, or API-key auth
    # starts failing one interval later than the main DB did.
    if settings.SDK_GATEWAY_MONGODB_URI and _URI_PATTERN.match(settings.SDK_GATEWAY_MONGODB_URI):
        db_module.sdk_gateway_client = AsyncIOMotorClient(_swap(settings.SDK_GATEWAY_MONGODB_URI))
    print("🔐 DocumentDB password rotation detected — connection(s) refreshed automatically, no restart needed")


async def start_background_refresher(interval_seconds: int = 30) -> None:
    if not settings.DOCDB_SECRET_ARN:
        return  # opt-in only — see module docstring

    print(f"🔐 DocumentDB credential refresher started (checking every {interval_seconds}s)")
    while True:
        try:
            await _rebuild_client_if_changed()
        except Exception as e:
            # Never let a transient Secrets Manager hiccup kill the loop —
            # just try again next interval. The existing connection (built
            # from whatever password was last known-good) keeps serving
            # requests in the meantime.
            print(f"⚠️ DocumentDB credential refresh check failed (will retry in {interval_seconds}s): {e}")
        await asyncio.sleep(interval_seconds)
