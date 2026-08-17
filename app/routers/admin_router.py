"""
Admin-only endpoints for user management
Only accessible by admin email: urisocialingsight@gmail.com
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from app.core.auth_bearer import JWTBearer
from app.core.config import settings
from app.database import get_db
from app.services.CreditService import credit_service
from app.services.TrialService import trial_service

router = APIRouter(
    prefix="/api/admin",
    tags=["Admin"],
)

def _billing_user_id(user: dict) -> str:
    """
    The id that keys user_credits/user_trials/workspaces/content_drafts/
    brand_profiles across this codebase is users.userId (a UUID set at signup —
    see auth_router.py's three insert_one calls, all of which set it, plus
    get_user_id() in billing_router.py which pulls this same field straight
    out of the JWT). It is NOT str(users._id) — the Mongo ObjectId. Every read
    below used str(_id) instead, which meant every credit/trial number this
    router ever showed an admin was looking up a different, almost-certainly-
    empty record. _id remains the right identifier for the /users/{id} route
    param itself (that lookup is genuinely by _id); this is only for querying
    the collections that key off userId.
    """
    return user.get("userId") or str(user.get("_id"))


async def _billing_summary(billing_id: str) -> tuple[int, str]:
    """
    Returns (credits_balance, subscription_tier_label) using the SAME
    canonical CreditService.get_credit_balance/TrialService.get_trial_status
    this backend uses everywhere else (the user's own dashboard, the billing
    endpoints). This router used to hand-roll its own copy of this logic —
    reading user_credits/user_trials directly and adding trial_credits
    whenever they were nonzero, with no check on whether the trial had
    actually expired by date. That drifted out of sync with the real
    balance calculation (which does check trial_end_date), so this admin
    panel could show a different number than the user's own account ever
    would. Delegating here instead of maintaining a second copy.
    """
    balance = await credit_service.get_credit_balance(billing_id)
    tier = balance.subscription_tier or "free"
    if tier == "free" and balance.trial_credits_included > 0:
        tier = "trial"
    return balance.credits_remaining, tier


def _bootstrap_admin_emails() -> set:
    return {e.strip().lower() for e in (settings.ADMIN_EMAILS or "").split(",") if e.strip()}


async def _is_admin_email(email: str, db: AsyncIOMotorDatabase) -> bool:
    """
    Admin status has two sources: the env-configured ADMIN_EMAILS allowlist
    (the bootstrap admin(s) — always valid, survives any DB state) and the
    per-user `is_admin` flag on the users collection (grantable/revocable
    from the admin UI, see /users/{id}/admin/grant|revoke below). Either
    grants access.
    """
    if email.lower() in _bootstrap_admin_emails():
        return True
    user = await db["users"].find_one({"email": email}, {"is_admin": 1})
    return bool(user and user.get("is_admin"))


async def verify_admin(
    jwt_payload: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    """Verify that the user is an admin"""
    if not jwt_payload:
        raise HTTPException(status_code=401, detail="Invalid authentication token")

    # Extract email from JWT claims
    claims = jwt_payload.get("claims", {})
    user_email = claims.get("email")

    if not user_email:
        raise HTTPException(status_code=401, detail="Invalid token: email not found")

    if not await _is_admin_email(user_email, db):
        raise HTTPException(status_code=403, detail="Access denied. Admin only.")

    return jwt_payload


@router.get("/me")
async def get_my_admin_status(
    jwt_payload: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Tells any authenticated user whether THEY are an admin — unlike every
    other endpoint in this router, deliberately not gated by verify_admin,
    since its entire purpose is answering that question for the frontend's
    nav-visibility check.
    """
    claims = jwt_payload.get("claims", {})
    user_email = claims.get("email")
    if not user_email:
        raise HTTPException(status_code=401, detail="Invalid token: email not found")
    return {"is_admin": await _is_admin_email(user_email, db)}


@router.get("/users")
async def get_all_users(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = None,
    sort_by: str = Query("createdAt", enum=["createdAt", "email", "name"]),
    sort_order: str = Query("desc", enum=["asc", "desc"]),
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Get all users with pagination, search, and sorting
    Admin only endpoint
    """
    # Build query
    query = {}
    if search:
        query["$or"] = [
            {"email": {"$regex": search, "$options": "i"}},
            {"first_name": {"$regex": search, "$options": "i"}},
            {"last_name": {"$regex": search, "$options": "i"}},
        ]

    # Count total users
    total_users = await db["users"].count_documents(query)

    # Calculate pagination
    skip = (page - 1) * limit
    total_pages = (total_users + limit - 1) // limit

    # Sort order
    sort_direction = -1 if sort_order == "desc" else 1

    # Map frontend sort_by to actual DB field names
    sort_field_map = {
        "createdAt": "created_at",
        "email": "email",
        "name": "first_name"  # Sort by first_name when sorting by name
    }
    db_sort_field = sort_field_map.get(sort_by, "created_at")

    # Fetch users
    cursor = db["users"].find(query).sort(db_sort_field, sort_direction).skip(skip).limit(limit)
    users = []

    async for user in cursor:
        user_id = str(user.get("_id"))
        billing_id = _billing_user_id(user)

        credits_balance, subscription_tier = await _billing_summary(billing_id)

        # Build full name from first_name and last_name
        first_name = user.get("first_name", "")
        last_name = user.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip() if first_name or last_name else None

        user_data = {
            "id": user_id,
            "email": user.get("email"),
            "firstName": first_name,
            "lastName": last_name,
            "name": full_name,
            "createdAt": user.get("created_at"),
            "subscription_tier": subscription_tier,
            "trial_start": user.get("trial_start"),
            "trial_end": user.get("trial_end"),
            "credits_balance": credits_balance,
            "phone": user.get("phone"),
            "is_admin": bool(user.get("is_admin")),
        }
        users.append(user_data)

    return {
        "users": users,
        "pagination": {
            "total": total_users,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
        }
    }


@router.get("/users/recent")
async def get_recent_users(
    days: int = Query(7, ge=1, le=90),
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Get recently signed up users
    Admin only endpoint
    """
    from datetime import timedelta

    cutoff_date = datetime.utcnow() - timedelta(days=days)

    query = {
        "created_at": {"$gte": cutoff_date}
    }

    cursor = db["users"].find(query).sort("created_at", -1)
    users = []

    async for user in cursor:
        user_id = str(user.get("_id"))
        billing_id = _billing_user_id(user)

        credits_balance, subscription_tier = await _billing_summary(billing_id)

        # Build full name
        first_name = user.get("first_name", "")
        last_name = user.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip() if first_name or last_name else None

        user_data = {
            "id": user_id,
            "email": user.get("email"),
            "firstName": first_name,
            "lastName": last_name,
            "name": full_name,
            "createdAt": user.get("created_at"),
            "subscription_tier": subscription_tier,
            "trial_end": user.get("trial_end"),
            "credits_balance": credits_balance,
            "is_admin": bool(user.get("is_admin")),
        }
        users.append(user_data)

    return {
        "users": users,
        "count": len(users),
        "days": days
    }


@router.get("/users/{user_id}")
async def get_user_details(
    user_id: str,
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Get detailed information about a specific user
    Admin only endpoint
    """
    from bson import ObjectId

    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    billing_id = _billing_user_id(user)

    credits_balance, subscription_tier = await _billing_summary(billing_id)

    # Build full name
    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip() if first_name or last_name else None

    # Get user's brand profiles
    brand_profiles = []
    async for profile in db["brand_profiles"].find({"user_id": billing_id}):
        brand_profiles.append({
            "id": str(profile.get("_id")),
            "brand_name": profile.get("brand_name"),
            "industry": profile.get("industry"),
            "created_at": profile.get("created_at"),
        })

    # Get user's content count — content_drafts is the real collection name
    # (generated_content doesn't exist; see carousel_generation_service.py /
    # approval_workflow_service.py / video_edit_service.py, all of which write
    # to content_drafts keyed by this same billing_id).
    content_count = await db["content_drafts"].count_documents({"user_id": billing_id})

    # Get user's workspaces — WorkspaceService.py writes these with a
    # "user_id" field, not "owner_id".
    workspaces = []
    async for workspace in db["workspaces"].find({"user_id": billing_id}):
        workspaces.append({
            "id": str(workspace.get("_id")),
            "name": workspace.get("name"),
            "created_at": workspace.get("created_at"),
        })

    user_data = {
        "id": user_id,
        "email": user.get("email"),
        "firstName": first_name,
        "lastName": last_name,
        "name": full_name,
        "phone": user.get("phone"),
        "createdAt": user.get("created_at"),
        "subscription_tier": subscription_tier,
        "trial_start": user.get("trial_start"),
        "trial_end": user.get("trial_end"),
        "credits_balance": credits_balance,
        "brand_profiles": brand_profiles,
        "content_count": content_count,
        "workspaces": workspaces,
        "is_admin": bool(user.get("is_admin")),
    }

    return user_data


@router.post("/users/{user_id}/admin/grant")
async def grant_admin(
    user_id: str,
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Grant admin access to a user. Admin-only — an existing admin must do the granting."""
    from bson import ObjectId

    result = await db["users"].update_one({"_id": ObjectId(user_id)}, {"$set": {"is_admin": True}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"user_id": user_id, "is_admin": True}


@router.post("/users/{user_id}/admin/revoke")
async def revoke_admin(
    user_id: str,
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Revoke a user's admin access. Blocks revoking your own access — a
    DB-granted admin could otherwise lock themselves out with no one else
    able to undo it (the bootstrap ADMIN_EMAILS allowlist is the only account
    guaranteed to always regain access, and self-revoke isn't worth risking
    that gap for).
    """
    from bson import ObjectId

    target = await db["users"].find_one({"_id": ObjectId(user_id)}, {"email": 1})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    acting_email = (admin_user.get("claims", {}) or {}).get("email", "")
    if target.get("email", "").lower() == acting_email.lower():
        raise HTTPException(status_code=400, detail="Cannot revoke your own admin access — ask another admin.")

    await db["users"].update_one({"_id": ObjectId(user_id)}, {"$set": {"is_admin": False}})
    return {"user_id": user_id, "is_admin": False}


class CreditAdjustRequest(BaseModel):
    amount: int = Field(..., description="Signed delta — positive grants, negative claws back")
    reason: Optional[str] = Field(default=None, description="Admin's free-text note for this adjustment")


async def _resolve_billing_id(user_id: str, db: AsyncIOMotorDatabase) -> str:
    """
    The credits/trial/adjust endpoints take the users._id (what the frontend
    has as user.id from the list/detail views), but user_credits/user_trials
    are keyed by users.userId — see _billing_user_id's docstring above. Resolve
    it here so a credit or trial adjustment actually lands on the record the
    user's own session reads, instead of silently writing a phantom record
    under the wrong id.
    """
    from bson import ObjectId

    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _billing_user_id(user)


@router.post("/users/{user_id}/credits/adjust")
async def adjust_user_credits(
    user_id: str,
    body: CreditAdjustRequest,
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Adjust a user's bonus credit balance by a signed amount, floored at 0.
    Replaces raw MongoDB edits with an auditable, logged operation.
    """
    billing_id = await _resolve_billing_id(user_id, db)
    wallet = await credit_service.admin_adjust_credits(billing_id, body.amount, notes=body.reason)
    return {
        "user_id": user_id,
        "credits_balance": wallet.credits_remaining,
        "bonus_credits": wallet.bonus_credits,
        "total_credits": wallet.total_credits,
    }


@router.post("/users/{user_id}/trial/adjust")
async def adjust_user_trial_credits(
    user_id: str,
    body: CreditAdjustRequest,
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Adjust a user's remaining trial credits by a signed amount, floored at 0."""
    billing_id = await _resolve_billing_id(user_id, db)
    try:
        status = await trial_service.admin_adjust_trial_credits(billing_id, body.amount, notes=body.reason)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return status


@router.post("/users/{user_id}/trial/expire")
async def expire_user_trial(
    user_id: str,
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Force-expire a user's trial (credits_remaining=0, trial_used=True)."""
    billing_id = await _resolve_billing_id(user_id, db)
    try:
        status = await trial_service.admin_expire_trial(billing_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return status


@router.get("/stats")
async def get_admin_stats(
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Get overall platform statistics
    Admin only endpoint
    """
    from datetime import timedelta

    # Total users
    total_users = await db["users"].count_documents({})

    # Users in last 7 days
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    new_users_7d = await db["users"].count_documents({"created_at": {"$gte": seven_days_ago}})

    # Users in last 30 days
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    new_users_30d = await db["users"].count_documents({"created_at": {"$gte": thirty_days_ago}})

    # Users by subscription tier (from user_credits collection)
    subscription_stats = {}
    async for doc in db["user_credits"].aggregate([
        {"$group": {"_id": "$subscription_tier", "count": {"$sum": 1}}}
    ]):
        tier = doc["_id"] or "free"
        subscription_stats[tier] = doc["count"]

    # Total content generated
    total_content = await db["generated_content"].count_documents({})

    # Total brand profiles
    total_brands = await db["brand_profiles"].count_documents({})

    # Total workspaces
    total_workspaces = await db["workspaces"].count_documents({})

    return {
        "total_users": total_users,
        "new_users_7d": new_users_7d,
        "new_users_30d": new_users_30d,
        "subscription_stats": subscription_stats,
        "total_content": total_content,
        "total_brands": total_brands,
        "total_workspaces": total_workspaces,
    }


@router.get("/users/export/emails")
async def export_user_emails(
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Export all user emails
    Admin only endpoint
    """
    cursor = db["users"].find({}, {"email": 1, "first_name": 1, "last_name": 1, "created_at": 1}).sort("created_at", -1)
    emails = []

    async for user in cursor:
        emails.append({
            "email": user.get("email"),
            "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or "N/A",
            "registered_at": user.get("created_at"),
        })

    return {
        "emails": emails,
        "total": len(emails)
    }


# ── Brand-profile integrity scan (read-only) ──────────────────────────────────
# Diagnoses the historical cross-brand data leak (fixed in
# services/brand_profile_service.py — a bare {"user_id": user_id} query used to
# match ANY of a user's brand_profiles docs instead of the one actually requested).
# The code fix stops NEW corruption; documents already overwritten before the fix
# shipped are still wrong and need identifying here, then repairing separately.
# This endpoint only reads — it changes nothing.

_IDENTITY_FIELDS = [
    "brand_name", "industry", "website", "tagline", "product_description",
    "target_audience", "primary_goal",
]


@router.get("/brand-profiles/integrity-scan")
async def brand_profile_integrity_scan(
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Two independent signals, both reported (a profile can trip either or both):

    1. name_mismatch — brand_profiles.brand_name doesn't match the brand's real
       name in brand_accounts for the SAME brand_id. Strong evidence this document
       was written for a different brand and never corrected.
    2. shared_content — two or more DIFFERENT brand_ids under the same user_id
       have byte-identical identity fields (name/industry/tagline/etc). Two
       genuinely independent brands don't naturally end up identical; this is the
       exact fingerprint of the old bug (one document silently serving several
       brands).
    """
    brands_by_id = {}
    async for b in db["brand_accounts"].find({}, {"brand_id": 1, "name": 1, "owner_user_id": 1}):
        brands_by_id[b["brand_id"]] = b

    profiles = []
    async for p in db["brand_profiles"].find({}):
        p["_id"] = str(p["_id"])
        profiles.append(p)

    name_mismatches = []
    for p in profiles:
        bid = p.get("brand_id")
        if not bid or bid not in brands_by_id:
            continue
        real_name = (brands_by_id[bid].get("name") or "").strip().lower()
        stored_name = (p.get("brand_name") or "").strip().lower()
        if real_name and stored_name and real_name != stored_name:
            name_mismatches.append({
                "brand_id": bid,
                "user_id": p.get("user_id"),
                "brand_real_name": brands_by_id[bid].get("name"),
                "profile_stored_name": p.get("brand_name"),
                "profile_updated_at": str(p.get("updated_at")),
            })

    by_user = {}
    for p in profiles:
        uid = p.get("user_id")
        if uid:
            by_user.setdefault(uid, []).append(p)

    shared_content_groups = []
    for uid, user_profiles in by_user.items():
        if len(user_profiles) < 2:
            continue
        by_signature = {}
        for p in user_profiles:
            sig = tuple((p.get(f) or "") for f in _IDENTITY_FIELDS)
            by_signature.setdefault(sig, []).append(p)
        for sig, group in by_signature.items():
            distinct_brand_ids = {p.get("brand_id") for p in group}
            if len(group) > 1 and len(distinct_brand_ids) > 1 and any(sig):
                shared_content_groups.append({
                    "user_id": uid,
                    "shared_brand_name": sig[0],
                    "affected_brand_ids": sorted(distinct_brand_ids),
                    "count": len(group),
                })

    return {
        "status": True,
        "total_profiles_scanned": len(profiles),
        "name_mismatches": name_mismatches,
        "name_mismatch_count": len(name_mismatches),
        "shared_content_groups": shared_content_groups,
        "shared_content_group_count": len(shared_content_groups),
    }
