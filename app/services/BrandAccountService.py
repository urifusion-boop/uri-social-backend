"""
Brand Account Service — Agency Accounts feature

Brand CRUD, personal-brand auto-provisioning (solo SMEs), duplicate-from-existing
template cloning, and access-aware brand listing.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.brand_account import BrandAccount
from app.services.AgencyService import AgencyService, AgencyRole

BRANDS = "brand_accounts"

# Jane collections that hold one doc-per-brand and are cloned on duplicate.
_PROFILE_COLLECTIONS = ["brand_profiles", "writing_dna"]


class BrandAccountService:
    """Business logic for brand accounts (the Jane isolation unit)."""

    @staticmethod
    async def get_brand(brand_id: str, db: AsyncIOMotorDatabase) -> Optional[BrandAccount]:
        doc = await db[BRANDS].find_one({"brand_id": brand_id})
        if not doc:
            return None
        doc["_id"] = str(doc["_id"])
        return BrandAccount(**doc)

    @staticmethod
    async def get_or_create_personal_brand(user_id: str, db: AsyncIOMotorDatabase) -> BrandAccount:
        """
        Solo SME path: every user has exactly one personal brand (agency_id=None).
        Deterministic brand_id so the migration and runtime agree. Idempotent.
        """
        brand_id = BrandAccount.personal_brand_id(user_id)
        existing = await db[BRANDS].find_one({"brand_id": brand_id})
        if existing:
            existing["_id"] = str(existing["_id"])
            return BrandAccount(**existing)

        # Seed name/industry from the user's existing brand_profiles doc if present
        name = "My Brand"
        industry = None
        profile = await db["brand_profiles"].find_one({"user_id": user_id})
        if profile:
            name = profile.get("brand_name") or name
            industry = profile.get("industry")

        brand = BrandAccount(
            brand_id=brand_id,
            agency_id=None,
            owner_user_id=user_id,
            name=name,
            industry=industry,
        )
        await db[BRANDS].insert_one(brand.to_dict())
        return brand

    @staticmethod
    async def create_brand(
        owner_user_id: str,
        name: str,
        db: AsyncIOMotorDatabase,
        agency_id: Optional[str] = None,
        industry: Optional[str] = None,
        logo_url: Optional[str] = None,
        monthly_credit_cap: Optional[float] = None,
    ) -> BrandAccount:
        brand = BrandAccount(
            brand_id=BrandAccount.generate_brand_id(),
            agency_id=agency_id,
            owner_user_id=owner_user_id,
            name=name,
            industry=industry,
            logo_url=logo_url,
            monthly_credit_cap=monthly_credit_cap,
        )
        await db[BRANDS].insert_one(brand.to_dict())

        # Seed a brand_profiles doc scoped to the new brand_id. Agency brands are
        # created via the streamlined form (PRD §3.4), not the full onboarding
        # wizard — so mark onboarding complete and let the playbook be edited later.
        await db["brand_profiles"].update_one(
            {"brand_id": brand.brand_id},
            {"$setOnInsert": {
                "brand_id": brand.brand_id,
                "user_id": owner_user_id,
                "brand_name": name,
                "industry": industry or "",
                "onboarding_completed": True,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }},
            upsert=True,
        )
        return brand

    @staticmethod
    async def duplicate_from_existing(
        template_brand_id: str,
        owner_user_id: str,
        name: str,
        db: AsyncIOMotorDatabase,
        agency_id: Optional[str] = None,
        industry: Optional[str] = None,
    ) -> Optional[BrandAccount]:
        """Clone playbook/DNA structure from a template brand into a new brand."""
        template = await BrandAccountService.get_brand(template_brand_id, db)
        if not template:
            return None

        new_brand = await BrandAccountService.create_brand(
            owner_user_id=owner_user_id,
            name=name,
            db=db,
            agency_id=agency_id,
            industry=industry or template.industry,
        )

        # Clone the per-brand profile docs (brand_profiles, writing_dna)
        for coll in _PROFILE_COLLECTIONS:
            src = await db[coll].find_one({"brand_id": template_brand_id})
            if src:
                src.pop("_id", None)
                src["brand_id"] = new_brand.brand_id
                src["user_id"] = owner_user_id
                src["created_at"] = datetime.utcnow()
                src["updated_at"] = datetime.utcnow()
                # Brand-name fields should reflect the new brand, not the template
                if coll == "brand_profiles":
                    src["brand_name"] = name
                await db[coll].replace_one({"brand_id": new_brand.brand_id}, src, upsert=True)

        return new_brand

    @staticmethod
    async def list_brands_for_user(user_id: str, db: AsyncIOMotorDatabase) -> List[BrandAccount]:
        ids = await AgencyService.accessible_brand_ids(user_id, db)
        out: List[BrandAccount] = []
        async for doc in db[BRANDS].find({"brand_id": {"$in": ids}, "status": "active"}):
            doc["_id"] = str(doc["_id"])
            out.append(BrandAccount(**doc))
        return out

    @staticmethod
    async def list_brands_for_agency(agency_id: str, db: AsyncIOMotorDatabase) -> List[BrandAccount]:
        out: List[BrandAccount] = []
        async for doc in db[BRANDS].find({"agency_id": agency_id, "status": "active"}):
            doc["_id"] = str(doc["_id"])
            out.append(BrandAccount(**doc))
        return out

    @staticmethod
    async def update_brand(brand_id: str, updates: Dict[str, Any], db: AsyncIOMotorDatabase) -> Optional[BrandAccount]:
        clean = {k: v for k, v in updates.items() if v is not None}
        if clean:
            clean["updated_at"] = datetime.utcnow()
            await db[BRANDS].update_one({"brand_id": brand_id}, {"$set": clean})
        return await BrandAccountService.get_brand(brand_id, db)

    @staticmethod
    async def archive_brand(brand_id: str, db: AsyncIOMotorDatabase) -> bool:
        """PRD open-Q #2 default: archive (recoverable), purge later."""
        result = await db[BRANDS].update_one(
            {"brand_id": brand_id},
            {"$set": {"status": "archived", "archived_at": datetime.utcnow(), "updated_at": datetime.utcnow()}},
        )
        return result.modified_count > 0
