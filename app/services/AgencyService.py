"""
Agency Service — Agency Accounts feature

Agency CRUD, membership, member-to-brand assignment, and the access-control
predicate that everything else depends on: user_has_access_to_brand.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId

from app.models.agency import (
    Agency,
    AgencyMember,
    MemberBrandAccess,
    AgencyRole,
)

AGENCIES = "agencies"
AGENCY_MEMBERS = "agency_members"
MEMBER_BRAND_ACCESS = "member_brand_access"
BRANDS = "brand_accounts"


class AgencyService:
    """Business logic for agencies, members, and brand access."""

    # ── Agencies ──────────────────────────────────────────────────────────

    @staticmethod
    async def create_agency(
        name: str, owner_user_id: str, db: AsyncIOMotorDatabase, plan_tier: str = "agency_starter"
    ) -> Agency:
        agency = Agency(
            agency_id=Agency.generate_agency_id(),
            name=name,
            owner_user_id=owner_user_id,
            plan_tier=plan_tier,
        )
        result = await db[AGENCIES].insert_one(agency.to_dict())
        agency.id = str(result.inserted_id)

        # Owner becomes the first admin member
        owner = await db["users"].find_one({"_id": ObjectId(owner_user_id)}) if ObjectId.is_valid(owner_user_id) else None
        await AgencyService.add_member(
            agency_id=agency.agency_id,
            user_id=owner_user_id,
            role=AgencyRole.ADMIN,
            invited_by_user_id=None,
            db=db,
            email=(owner or {}).get("email"),
        )
        return agency

    @staticmethod
    async def get_agency(agency_id: str, db: AsyncIOMotorDatabase) -> Optional[Agency]:
        doc = await db[AGENCIES].find_one({"agency_id": agency_id})
        if not doc:
            return None
        doc["_id"] = str(doc["_id"])
        return Agency(**doc)

    @staticmethod
    async def get_agency_for_user(user_id: str, db: AsyncIOMotorDatabase) -> Optional[Agency]:
        """The agency a user belongs to (V1: at most one active membership)."""
        member = await db[AGENCY_MEMBERS].find_one({"user_id": user_id, "status": "active"})
        if not member:
            return None
        return await AgencyService.get_agency(member["agency_id"], db)

    @staticmethod
    async def get_agency_for_brand(brand_id: str, db: AsyncIOMotorDatabase) -> Optional[Agency]:
        brand = await db[BRANDS].find_one({"brand_id": brand_id}, {"agency_id": 1})
        if not brand or not brand.get("agency_id"):
            return None
        return await AgencyService.get_agency(brand["agency_id"], db)

    # ── Members ───────────────────────────────────────────────────────────

    @staticmethod
    async def _holds_other_active_agency(user_id: str, agency_id: str, db: AsyncIOMotorDatabase) -> bool:
        """
        V1 invariant: a user belongs to at most one agency. True if `user_id` is
        already an active member of some agency other than `agency_id` — binding
        them into a second agency here would silently break get_agency_for_user
        (find_one with no tiebreak), leaving the invite active in the DB but the
        user's dashboard still pinned to their original agency.
        """
        other = await db[AGENCY_MEMBERS].find_one(
            {"user_id": user_id, "status": "active", "agency_id": {"$ne": agency_id}}
        )
        return other is not None

    @staticmethod
    async def add_member(
        agency_id: str,
        user_id: Optional[str],
        role: AgencyRole,
        invited_by_user_id: Optional[str],
        db: AsyncIOMotorDatabase,
        email: Optional[str] = None,
    ) -> AgencyMember:
        # An invite into a second agency for someone who already belongs to one
        # stays "invited" rather than silently activating — see _holds_other_active_agency.
        blocked = bool(
            user_id and invited_by_user_id is not None
            and await AgencyService._holds_other_active_agency(user_id, agency_id, db)
        )

        # Dedup by user_id (registered) or by email (pending invite)
        dedup = {"agency_id": agency_id}
        if user_id:
            dedup["user_id"] = user_id
        elif email:
            dedup["email"] = email.lower()
        existing = await db[AGENCY_MEMBERS].find_one(dedup)
        if existing:
            if existing.get("status") == "removed":
                new_status = "invited" if blocked else ("active" if user_id else "invited")
                await db[AGENCY_MEMBERS].update_one(
                    {"_id": existing["_id"]},
                    {"$set": {"status": new_status,
                              "role": role.value if hasattr(role, "value") else role,
                              "updated_at": datetime.utcnow()}},
                )
                existing["status"] = new_status
            existing["_id"] = str(existing["_id"])
            return AgencyMember(**existing)

        is_active = (invited_by_user_id is None or user_id) and not blocked
        member = AgencyMember(
            agency_member_id=AgencyMember.generate_member_id(),
            agency_id=agency_id,
            user_id=user_id,
            email=email.lower() if email else None,
            role=role,
            invited_by_user_id=invited_by_user_id,
            # self-add (owner) is active; registered invite is active; email-only is pending
            status="active" if is_active else "invited",
            joined_at=datetime.utcnow() if is_active else None,
        )
        result = await db[AGENCY_MEMBERS].insert_one(member.to_dict())
        member.id = str(result.inserted_id)
        return member

    @staticmethod
    async def bind_pending_invites(user_id: str, email: Optional[str], db: AsyncIOMotorDatabase) -> None:
        """When a user logs in, claim any pending email invites addressed to them."""
        if not email:
            return
        async for invite in db[AGENCY_MEMBERS].find({"email": email.lower(), "user_id": None}):
            if await AgencyService._holds_other_active_agency(user_id, invite["agency_id"], db):
                continue  # stays pending — they already belong to another agency
            await db[AGENCY_MEMBERS].update_one(
                {"_id": invite["_id"]},
                {"$set": {"user_id": user_id, "status": "active",
                          "joined_at": datetime.utcnow(), "updated_at": datetime.utcnow()}},
            )

    @staticmethod
    async def get_member(agency_id: str, user_id: str, db: AsyncIOMotorDatabase) -> Optional[AgencyMember]:
        doc = await db[AGENCY_MEMBERS].find_one({"agency_id": agency_id, "user_id": user_id})
        if not doc:
            return None
        doc["_id"] = str(doc["_id"])
        return AgencyMember(**doc)

    @staticmethod
    async def list_members(agency_id: str, db: AsyncIOMotorDatabase) -> List[AgencyMember]:
        out: List[AgencyMember] = []
        async for doc in db[AGENCY_MEMBERS].find({"agency_id": agency_id, "status": {"$ne": "removed"}}):
            doc["_id"] = str(doc["_id"])
            out.append(AgencyMember(**doc))
        return out

    @staticmethod
    async def remove_member(agency_member_id: str, db: AsyncIOMotorDatabase) -> bool:
        result = await db[AGENCY_MEMBERS].update_one(
            {"agency_member_id": agency_member_id},
            {"$set": {"status": "removed", "updated_at": datetime.utcnow()}},
        )
        # Drop their brand assignments
        await db[MEMBER_BRAND_ACCESS].delete_many({"agency_member_id": agency_member_id})
        return result.modified_count > 0

    @staticmethod
    async def is_agency_admin(user_id: str, agency_id: str, db: AsyncIOMotorDatabase) -> bool:
        member = await AgencyService.get_member(agency_id, user_id, db)
        return bool(member and member.status == "active" and member.role == AgencyRole.ADMIN.value)

    # ── Member ↔ brand assignment ─────────────────────────────────────────

    @staticmethod
    async def assign_brand(agency_member_id: str, brand_id: str, db: AsyncIOMotorDatabase) -> MemberBrandAccess:
        existing = await db[MEMBER_BRAND_ACCESS].find_one(
            {"agency_member_id": agency_member_id, "brand_id": brand_id}
        )
        if existing:
            existing["_id"] = str(existing["_id"])
            return MemberBrandAccess(**existing)
        access = MemberBrandAccess(agency_member_id=agency_member_id, brand_id=brand_id)
        result = await db[MEMBER_BRAND_ACCESS].insert_one(access.to_dict())
        access.id = str(result.inserted_id)
        return access

    @staticmethod
    async def unassign_brand(agency_member_id: str, brand_id: str, db: AsyncIOMotorDatabase) -> bool:
        result = await db[MEMBER_BRAND_ACCESS].delete_one(
            {"agency_member_id": agency_member_id, "brand_id": brand_id}
        )
        return result.deleted_count > 0

    # ── The access predicate everything depends on ────────────────────────

    @staticmethod
    async def user_has_access_to_brand(user_id: str, brand_id: str, db: AsyncIOMotorDatabase) -> bool:
        """
        True if:
          - it's the user's personal solo brand (agency_id is null + they own it), OR
          - they're an admin of the brand's agency, OR
          - they have an explicit member_brand_access row for it.
        """
        brand = await db[BRANDS].find_one({"brand_id": brand_id})
        if not brand:
            return False

        agency_id = brand.get("agency_id")

        # Solo brand: only the owner
        if not agency_id:
            return brand.get("owner_user_id") == user_id

        # Agency brand: must be an active member of that agency
        member = await AgencyService.get_member(agency_id, user_id, db)
        if not member or member.status != "active":
            return False

        # Admins see all brands in their agency
        if member.role == AgencyRole.ADMIN.value:
            return True

        # Agents need an explicit assignment
        access = await db[MEMBER_BRAND_ACCESS].find_one(
            {"agency_member_id": member.agency_member_id, "brand_id": brand_id}
        )
        return access is not None

    @staticmethod
    async def accessible_brand_ids(user_id: str, db: AsyncIOMotorDatabase) -> List[str]:
        """All brand_ids a user can operate (solo personal + agency-assigned/admin)."""
        ids: List[str] = []

        # Solo personal brands owned by the user
        async for b in db[BRANDS].find(
            {"owner_user_id": user_id, "agency_id": None, "status": "active"}, {"brand_id": 1}
        ):
            ids.append(b["brand_id"])

        agency = await AgencyService.get_agency_for_user(user_id, db)
        if agency:
            member = await AgencyService.get_member(agency.agency_id, user_id, db)
            if member and member.role == AgencyRole.ADMIN.value:
                async for b in db[BRANDS].find(
                    {"agency_id": agency.agency_id, "status": "active"}, {"brand_id": 1}
                ):
                    ids.append(b["brand_id"])
            elif member:
                async for a in db[MEMBER_BRAND_ACCESS].find(
                    {"agency_member_id": member.agency_member_id}, {"brand_id": 1}
                ):
                    ids.append(a["brand_id"])

        # De-dupe, preserve order
        seen = set()
        return [x for x in ids if not (x in seen or seen.add(x))]
