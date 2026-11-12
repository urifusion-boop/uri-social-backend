"""
Uri Market Intelligence — access control (PRD §21: "Owner or administrator
can manage business context, sources, budgets and workspace users. Editors
can create topics, run authorized scans, review insights and create
drafts. Viewers can read permitted insights and evidence.").

Deliberately NOT built on AgencyRole or WorkspaceRole:
- AgencyRole is a two-valued (admin | agent) enum consumed in places across
  the agency billing/member-management system this module can't fully
  audit. Adding a third value there risks any existing
  "if role == ADMIN: ... else: (agent behaviour)" pattern silently treating
  a new viewer as a full agent everywhere ELSE in the app except here.
- WorkspaceRole has a real viewer tier, but it's scoped to an entirely
  different client_id-based "Workspace" feature — unrelated to brand access.

Instead: a small, additive, MI-only layer. Absence of an MIAccessGrant
record means FULL access (today's behaviour, unchanged for everyone) —
this can only ever RESTRICT a user below what the underlying agency/brand
system already grants them, never grant more. Only an existing agency
admin (for an agency-owned brand) or the brand's own owner (for a personal
brand) can restrict someone else to view-only.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_active_brand_context, get_db_dependency
from .models import MIAccessLevel

# Matches router.py's own dev-compatibility shim: aws/dev doesn't have
# get_flexible_brand_context (the API-key/SDK-aware resolver added on
# aws/prod) yet, and this feature only ever needs regular JWT auth anyway.
get_flexible_brand_context = get_active_brand_context


async def get_mi_access_level(db: AsyncIOMotorDatabase, brand_id: str, user_id: str) -> MIAccessLevel:
    grant = await db["mi_access"].find_one({"brand_id": brand_id, "user_id": user_id})
    if grant is None:
        return MIAccessLevel.FULL
    return MIAccessLevel(grant["level"])


async def can_manage_mi_access(db: AsyncIOMotorDatabase, brand_id: str, user_id: str) -> bool:
    """An agency admin for the brand's owning agency, or the brand's own
    owner for a personal (non-agency) brand."""
    from app.models.brand_account import BrandAccount
    from app.services.AgencyService import AgencyService

    brand_doc = await db["brand_accounts"].find_one({"brand_id": brand_id})
    if brand_doc is None:
        # No brand_accounts record at all (e.g. a legacy personal brand
        # predating that collection) — fail open to "is the owner" via the
        # deterministic personal brand id, never fail closed and lock
        # someone out of managing their own brand.
        return brand_id == BrandAccount.personal_brand_id(user_id)

    if brand_doc.get("agency_id"):
        return await AgencyService.is_agency_admin(user_id, brand_doc["agency_id"], db)
    return brand_doc.get("owner_user_id") == user_id


async def require_mi_write_access(
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
) -> dict:
    """Drop-in replacement for get_flexible_brand_context on MUTATING
    endpoints only — every read endpoint stays on the plain brand-context
    dependency, open to view-only users too."""
    level = await get_mi_access_level(db, ctx["brand_id"], ctx["user_id"])
    if level == MIAccessLevel.VIEW_ONLY:
        raise HTTPException(status_code=403, detail="You have view-only access to this workspace's Market Intelligence")
    return ctx
