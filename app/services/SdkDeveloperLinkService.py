"""
SDK Developer Link Service

Bridges an SDK Gateway `Developer` identity to a real, billable
uri-social-backend user account.

Why this exists: app/middleware/api_key_auth.py validates gateway-issued
API keys directly against the SDK Gateway's own MongoDB, then hands the
gateway's Developer._id to the rest of the app as if it were a native
uri-social-backend user_id. A gateway Developer and a uri-social-backend
user are two unrelated accounts in two entirely separate databases — that
raw ID has no users doc and no credit wallet, so every credit-checked
endpoint (CreditService.check_sufficient_credits) permanently blocks it,
while endpoints that don't require a pre-existing user (e.g.
BrandAccountService.get_or_create_personal_brand) silently create data
owned by a meaningless, unbilled ID.

This resolves that developer_id to a real, persistent uri-social-backend
user on first use, mirroring the existing Google-signup pattern in
app/routers/auth_router.py's google_auth() (mint a fresh internal userId,
activate the same trial every new signup gets) — with one deliberate
difference: it never matches an *existing* user by email. The SDK
Gateway's key-creation endpoint does not require Developer.is_verified,
so matching by email would let anyone claim a victim's real account by
signing up on the gateway with the victim's (unverified) email address.
Matching purely on the gateway's own server-controlled developer_id avoids
that entirely — it can only ever resolve to a fresh account or a
previously-created one, never someone else's.

Performance note: this runs on every gateway-key-authenticated request, so
the hot path is a single indexed read (`find_one`) for the overwhelming
common case of an already-linked developer. The write path (atomic
upsert + trial activation) only runs once, on first-ever use of a given
developer_id — protected by a unique index (see app/main.py's startup
event) against the case where a burst of concurrent first-requests race
each other.
"""

import uuid
from datetime import datetime
from typing import Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.services.TrialService import trial_service


async def _get_gateway_developer_profile(developer_id: str, gateway_db) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Best-effort lookup of the developer's email/first_name/last_name for
    display purposes only — never used for account matching, see module
    docstring. Returns (email, first_name, last_name), any of which may be
    None if the lookup fails or fields are unset."""
    try:
        from bson import ObjectId
        doc = await gateway_db.developers.find_one({"_id": ObjectId(developer_id)})
        if not doc:
            return None, None, None
        return doc.get("email"), doc.get("first_name"), doc.get("last_name")
    except Exception as e:
        print(f"⚠️ Could not fetch gateway developer profile for {developer_id}: {e}")
        return None, None, None


async def _create_linked_user(developer_id: str, db: AsyncIOMotorDatabase, gateway_db) -> str:
    """First-ever-use path: atomically create the linked user. Race-safe
    against concurrent first-requests via the unique index on
    sdk_gateway_developer_id — the loser of the race gets DuplicateKeyError
    and simply re-reads what the winner just created."""
    now = datetime.utcnow()
    email, first_name, last_name = (None, None, None)
    if gateway_db is not None:
        email, first_name, last_name = await _get_gateway_developer_profile(developer_id, gateway_db)

    new_user_id = str(uuid.uuid4())
    try:
        await db["users"].insert_one({
            "userId": new_user_id,
            "email": email or f"sdk-{developer_id}@sdk.urisocial.com",
            "password": None,
            "first_name": first_name or "SDK",
            "last_name": last_name or "Developer",
            "sdk_gateway_developer_id": developer_id,
            "auth_provider": "sdk_gateway",
            "role": "user",
            "created_at": now,
            "updated_at": now,
            "is_active": True,
            "email_verified": False,
            "account_status": "active",
            "last_login_at": now,
            "last_seen_at": now,
            "phone": None,
            "timezone": "UTC",
            "language": "en",
        })
    except DuplicateKeyError:
        # Another concurrent request won the race and already created this
        # developer's user — fall through to re-read it below.
        pass
    else:
        try:
            await trial_service.activate_trial(new_user_id)
        except Exception as e:
            print(f"⚠️ Trial activation failed for SDK-linked user {new_user_id}: {e}")
        return new_user_id

    existing = await db["users"].find_one({"sdk_gateway_developer_id": developer_id})
    if not existing:
        # Vanishingly unlikely (would mean the winner's insert was rolled
        # back between the DuplicateKeyError and this read), but don't
        # silently return a bogus id if it somehow happens.
        raise RuntimeError(f"Failed to resolve linked user for SDK developer {developer_id} after insert race")
    return existing["userId"]


async def resolve_or_create_user_for_developer(
    developer_id: str,
    db: AsyncIOMotorDatabase,
    gateway_db=None,
) -> str:
    """
    Map an SDK Gateway developer_id to a real uri-social-backend user_id,
    creating the account on first use. Idempotent and race-safe: concurrent
    calls with the same developer_id resolve to exactly one created user.

    Returns the resolved uri-social-backend user_id (a fresh UUID string on
    first use, the same one on every subsequent call for this developer).
    """
    existing = await db["users"].find_one(
        {"sdk_gateway_developer_id": developer_id},
        {"userId": 1},
    )
    if existing:
        return existing["userId"]

    return await _create_linked_user(developer_id, db, gateway_db)
