"""
Agency Router — Agency Accounts feature (PRD §3, §4, §5, §7)

Endpoints (all under /agency):
  POST   /agency                         create agency (caller becomes admin)
  GET    /agency                         get caller's agency
  PATCH  /agency                         rename agency (admin)
  GET    /agency/roster                  roster: brands + at-a-glance status
  GET    /agency/brands                  brand-switcher list (access-filtered)
  POST   /agency/brands                  add a brand (admin)
  POST   /agency/brands/duplicate        duplicate from existing brand (admin)
  PATCH  /agency/brands/{brand_id}       update a brand (admin)
  DELETE /agency/brands/{brand_id}       archive a brand (admin)
  DELETE /agency/brands/{brand_id}/permanent  permanently delete an archived brand (admin)
  GET    /agency/members                 list members (admin)
  POST   /agency/members                 invite member (admin)
  DELETE /agency/members/{member_id}     remove member (admin)
  POST   /agency/members/{member_id}/brands/{brand_id}    assign brand (admin)
  DELETE /agency/members/{member_id}/brands/{brand_id}    unassign brand (admin)
  POST   /agency/wallet/topup            top up wallet (admin)
  PATCH  /agency/settings                toggle per-brand caps (admin)
  GET    /agency/reports/portfolio       portfolio dashboard (admin)
  GET    /agency/reports/brand/{brand_id}  per-brand client report
"""

import asyncio
from datetime import datetime
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

from app.dependencies import get_db_dependency
from app.core.auth_bearer import JWTBearer
from app.core.config import settings
from app.domain.responses.uri_response import UriResponse
from app.services.EmailService import email_service

from app.models.agency import (
    CreateAgencyRequest, InviteAgencyMemberRequest, TopUpWalletRequest, UpdateAgencyRequest, AgencyRole,
)
from app.models.brand_account import CreateBrandRequest, DuplicateBrandRequest, UpdateBrandRequest
from app.services.AgencyService import AgencyService
from app.services.BrandAccountService import BrandAccountService
from app.services.AgencyCreditService import AgencyCreditService

router = APIRouter(prefix="/agency", tags=["Agency"])


def _uid(token: dict) -> str:
    uid = (token.get("claims") or {}).get("userId")
    if not uid:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    return uid


def _email(token: dict) -> Optional[str]:
    return (token.get("claims") or {}).get("email")


def _object_id_or_none(user_id: str) -> Optional[ObjectId]:
    try:
        return ObjectId(user_id)
    except (InvalidId, TypeError):
        return None


async def _require_admin(user_id: str, db: AsyncIOMotorDatabase):
    agency = await AgencyService.get_agency_for_user(user_id, db)
    if not agency:
        raise HTTPException(status_code=404, detail="No agency for this user")
    if not await AgencyService.is_agency_admin(user_id, agency.agency_id, db):
        raise HTTPException(status_code=403, detail="Agency admin required")
    return agency


# ── Agency ────────────────────────────────────────────────────────────────

@router.post("")
async def create_agency(
    body: CreateAgencyRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    user_id = _uid(token)
    existing = await AgencyService.get_agency_for_user(user_id, db)
    if existing:
        raise HTTPException(status_code=409, detail="User already belongs to an agency")
    agency = await AgencyService.create_agency(body.name, user_id, db, plan_tier=body.plan_tier)
    return UriResponse.create_response("agency", agency.to_public_dict())


@router.get("")
async def get_agency(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    # Claim any pending email invites addressed to this user before resolving
    await AgencyService.bind_pending_invites(_uid(token), _email(token), db)
    agency = await AgencyService.get_agency_for_user(_uid(token), db)
    return UriResponse.get_single_data_response("agency", agency.to_public_dict() if agency else None)


@router.post("/upgrade")
async def upgrade_solo_to_agency(
    body: CreateAgencyRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    PRD open-Q #3: a solo SME upgrades to an agency, bringing their existing brand in.
    Creates the agency (caller = admin) and attaches their personal brand to it
    (nullable agency_id makes this clean — all existing Jane data stays scoped to
    the same brand_id, now owned by the agency).
    """
    user_id = _uid(token)
    if await AgencyService.get_agency_for_user(user_id, db):
        raise HTTPException(status_code=409, detail="User already belongs to an agency")

    agency = await AgencyService.create_agency(body.name, user_id, db, plan_tier=body.plan_tier)

    personal = await BrandAccountService.get_or_create_personal_brand(user_id, db)
    await db["brand_accounts"].update_one(
        {"brand_id": personal.brand_id},
        {"$set": {"agency_id": agency.agency_id, "updated_at": datetime.utcnow()}},
    )
    return UriResponse.create_response("agency", {
        **agency.to_public_dict(),
        "migrated_brand_id": personal.brand_id,
    })


@router.patch("")
async def update_agency(
    body: UpdateAgencyRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    agency = await _require_admin(_uid(token), db)
    await db["agencies"].update_one(
        {"agency_id": agency.agency_id},
        {"$set": {"name": body.name, "updated_at": datetime.utcnow()}},
    )
    agency = await AgencyService.get_agency(agency.agency_id, db)
    return UriResponse.update_response("agency", agency.to_public_dict())


@router.patch("/settings")
async def update_settings(
    per_brand_caps_enabled: bool,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    agency = await _require_admin(_uid(token), db)
    await db["agencies"].update_one(
        {"agency_id": agency.agency_id},
        {"$set": {"per_brand_caps_enabled": per_brand_caps_enabled, "updated_at": datetime.utcnow()}},
    )
    return UriResponse.update_response("agency", {"per_brand_caps_enabled": per_brand_caps_enabled})


# ── Brands ──────────────────────────────────────────────────────────────────

@router.get("/brands")
async def list_brands(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Brand-switcher list — only brands the caller can access."""
    brands = await BrandAccountService.list_brands_for_user(_uid(token), db)
    return UriResponse.get_list_data_response("brand", [b.to_public_dict() for b in brands])


@router.post("/brands")
async def add_brand(
    body: CreateBrandRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    agency = await _require_admin(_uid(token), db)
    if agency.max_brands is not None:
        count = len(await BrandAccountService.list_brands_for_agency(agency.agency_id, db))
        if count >= agency.max_brands:
            raise HTTPException(status_code=403, detail="Brand limit reached for plan")
    brand = await BrandAccountService.create_brand(
        owner_user_id=_uid(token), name=body.name, db=db, agency_id=agency.agency_id,
        industry=body.industry, logo_url=body.logo_url, monthly_credit_cap=body.monthly_credit_cap,
    )
    return UriResponse.create_response("brand", brand.to_public_dict())


@router.post("/brands/duplicate")
async def duplicate_brand(
    body: DuplicateBrandRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    agency = await _require_admin(_uid(token), db)
    # Template must belong to the same agency
    if not await AgencyService.user_has_access_to_brand(_uid(token), body.template_brand_id, db):
        raise HTTPException(status_code=403, detail="No access to template brand")
    brand = await BrandAccountService.duplicate_from_existing(
        template_brand_id=body.template_brand_id, owner_user_id=_uid(token),
        name=body.name, db=db, agency_id=agency.agency_id, industry=body.industry,
    )
    if not brand:
        raise HTTPException(status_code=404, detail="Template brand not found")
    return UriResponse.create_response("brand", brand.to_public_dict())


@router.patch("/brands/{brand_id}")
async def update_brand(
    brand_id: str,
    body: UpdateBrandRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    await _require_admin(_uid(token), db)
    if not await AgencyService.user_has_access_to_brand(_uid(token), brand_id, db):
        raise HTTPException(status_code=403, detail="No access to brand")
    brand = await BrandAccountService.update_brand(brand_id, body.dict(exclude_none=True), db)
    return UriResponse.update_response("brand", brand.to_public_dict() if brand else None)


@router.delete("/brands/{brand_id}")
async def archive_brand(
    brand_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    await _require_admin(_uid(token), db)
    if not await AgencyService.user_has_access_to_brand(_uid(token), brand_id, db):
        raise HTTPException(status_code=403, detail="No access to brand")
    ok = await BrandAccountService.archive_brand(brand_id, db)
    return UriResponse.update_response("brand", {"brand_id": brand_id, "archived": ok})


@router.delete("/brands/{brand_id}/permanent")
async def delete_brand_permanently(
    brand_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Permanently and irreversibly delete a brand and all its data (profile,
    playbook, social connections, member access grants). The brand must already
    be archived first via DELETE /agency/brands/{brand_id} — a deliberate
    two-step gate before anything irreversible happens."""
    await _require_admin(_uid(token), db)
    if not await AgencyService.user_has_access_to_brand(_uid(token), brand_id, db):
        raise HTTPException(status_code=403, detail="No access to brand")

    brand = await BrandAccountService.get_brand(brand_id, db)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    if brand.status != "archived":
        raise HTTPException(
            status_code=400,
            detail="Brand must be archived first (DELETE /agency/brands/{brand_id}) before permanent deletion",
        )

    deleted_counts = await BrandAccountService.delete_brand_permanently(brand_id, db)
    return UriResponse.update_response("brand", {
        "brand_id": brand_id, "permanently_deleted": True, "deleted_counts": deleted_counts,
    })


# ── Roster (PRD §3.2) ────────────────────────────────────────────────────────

@router.get("/roster")
async def roster(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Morning-triage surface: each accessible brand + at-a-glance status."""
    user_id = _uid(token)
    brands = await BrandAccountService.list_brands_for_user(user_id, db)

    today_start = datetime(datetime.utcnow().year, datetime.utcnow().month, datetime.utcnow().day)
    cards = []
    for b in brands:
        bid = b.brand_id
        pending = await db["content_drafts"].count_documents({"brand_id": bid, "approval_status": "pending"})
        scheduled_today = await db["content_drafts"].count_documents(
            {"brand_id": bid, "status": "scheduled", "scheduled_datetime": {"$gte": today_start}}
        )
        consumed_month = await AgencyCreditService.brand_usage_this_month(bid, db)
        last_post = await db["blog_posts"].find_one({"brand_id": bid}, sort=[("updated_at", -1)])
        cards.append({
            **b.to_public_dict(),
            "pending_approvals": pending,
            "scheduled_today": scheduled_today,
            "credits_consumed_this_month": consumed_month,
            "monthly_credit_cap": b.monthly_credit_cap,
            "last_activity": (last_post or {}).get("updated_at").isoformat() if last_post and last_post.get("updated_at") else None,
        })
    return UriResponse.get_list_data_response("roster_brand", cards)


# ── Members (PRD §5.2) ───────────────────────────────────────────────────────

@router.get("/members")
async def list_members(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    agency = await _require_admin(_uid(token), db)
    members = await AgencyService.list_members(agency.agency_id, db)
    out = []
    for m in members:
        pub = m.to_public_dict()
        assigned = await db["member_brand_access"].distinct("brand_id", {"agency_member_id": m.agency_member_id})
        pub["assigned_brand_ids"] = assigned
        out.append(pub)
    return UriResponse.get_list_data_response("agency_member", out)


@router.post("/members")
async def invite_member(
    body: InviteAgencyMemberRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    agency = await _require_admin(_uid(token), db)
    # If the email already has an account, link immediately; otherwise store a
    # pending invite that auto-binds when they sign up / log in.
    user = await db["users"].find_one({"email": body.email})
    member = await AgencyService.add_member(
        agency.agency_id,
        str(user["_id"]) if user else None,
        body.role,
        _uid(token),
        db,
        email=body.email,
    )

    inviter = await db["users"].find_one({"_id": _object_id_or_none(_uid(token))})
    asyncio.ensure_future(email_service.send_email(
        to_email=body.email,
        subject=f"You've been invited to join {agency.name} on URI Social",
        template_name="agency_invite",
        template_vars={
            "agency_name": agency.name,
            "inviter_name": (inviter or {}).get("first_name") or "A teammate",
            "role": body.role.value if hasattr(body.role, "value") else body.role,
            "has_account": bool(user),
            "app_url": settings.WEB_APP_URL or "https://app.urisocial.com",
        },
    ))

    if member.status != "active":
        msg = (
            "Invite sent — they'll get access once they sign up"
            if not user
            else "Invite sent — they already belong to another agency, so they'll need to leave it first"
        )
    else:
        msg = "Member added"
    return UriResponse.create_response("agency_member", {**member.to_public_dict(), "message": msg})


@router.delete("/members/{member_id}")
async def remove_member(
    member_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    await _require_admin(_uid(token), db)
    ok = await AgencyService.remove_member(member_id, db)
    return UriResponse.update_response("agency_member", {"agency_member_id": member_id, "removed": ok})


@router.post("/members/{member_id}/brands/{brand_id}")
async def assign_member_brand(
    member_id: str,
    brand_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    await _require_admin(_uid(token), db)
    access = await AgencyService.assign_brand(member_id, brand_id, db)
    return UriResponse.create_response("member_brand_access", {
        "agency_member_id": member_id, "brand_id": brand_id, "assigned": True,
    })


@router.delete("/members/{member_id}/brands/{brand_id}")
async def unassign_member_brand(
    member_id: str,
    brand_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    await _require_admin(_uid(token), db)
    ok = await AgencyService.unassign_brand(member_id, brand_id, db)
    return UriResponse.update_response("member_brand_access", {
        "agency_member_id": member_id, "brand_id": brand_id, "unassigned": ok,
    })


# ── Wallet (PRD §4) ──────────────────────────────────────────────────────────

@router.post("/wallet/topup")
async def topup_wallet(
    body: TopUpWalletRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    agency = await _require_admin(_uid(token), db)
    balance = await AgencyCreditService.top_up(agency.agency_id, body.credits, db)
    return UriResponse.update_response("agency_wallet", {"wallet_credits": balance})


# ── Reporting (PRD §7) ───────────────────────────────────────────────────────

@router.get("/reports/portfolio")
async def portfolio_report(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    agency = await _require_admin(_uid(token), db)
    brands = await BrandAccountService.list_brands_for_agency(agency.agency_id, db)

    per_brand = []
    total_consumed = 0.0
    total_published = 0
    for b in brands:
        consumed = await AgencyCreditService.brand_usage_this_month(b.brand_id, db)
        published = await db["blog_posts"].count_documents({"brand_id": b.brand_id, "status": "published"})
        pending = await db["content_drafts"].count_documents({"brand_id": b.brand_id, "approval_status": "pending"})
        total_consumed += consumed
        total_published += published
        per_brand.append({
            "brand_id": b.brand_id, "name": b.name,
            "credits_consumed_this_month": consumed,
            "posts_published": published, "pending_approvals": pending,
            "needs_attention": pending > 0 or consumed == 0,
        })

    return UriResponse.get_single_data_response("portfolio_report", {
        "agency_id": agency.agency_id,
        "wallet_credits": agency.wallet_credits,
        "total_credits_consumed_this_month": total_consumed,
        "total_posts_published": total_published,
        "brand_count": len(brands),
        "per_brand": per_brand,
    })


@router.get("/reports/brand/{brand_id}")
async def brand_report(
    brand_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    if not await AgencyService.user_has_access_to_brand(_uid(token), brand_id, db):
        raise HTTPException(status_code=403, detail="No access to brand")

    brand = await BrandAccountService.get_brand(brand_id, db)
    published = await db["blog_posts"].count_documents({"brand_id": brand_id, "status": "published"})
    consumed = await AgencyCreditService.brand_usage_this_month(brand_id, db)
    top_posts_cursor = db["blog_posts"].find(
        {"brand_id": brand_id, "status": "published"}, {"_id": 0, "generated_title": 1, "primary_keyword": 1}
    ).limit(5)
    top_posts = await top_posts_cursor.to_list(length=5)

    return UriResponse.get_single_data_response("brand_report", {
        "brand_id": brand_id,
        "name": brand.name if brand else None,
        "posts_published": published,
        "credits_consumed_this_month": consumed,
        "top_posts": top_posts,
    })
