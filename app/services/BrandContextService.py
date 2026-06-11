"""
Brand Context Service — Agency Accounts feature (PRD §2.3 assembleJaneContext)

The data-access enforcement layer. brand_id is the hard boundary: Jane assembles
context for exactly one brand and never queries across brands. Use scoped_query
to build every Jane DB filter so the brand_id is impossible to forget.
"""

from typing import Optional, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.AgencyService import AgencyService


class BrandContextService:
    """Assembles + scopes all Jane data access by brand_id."""

    @staticmethod
    def scoped_query(base: Optional[Dict[str, Any]], brand_id: str) -> Dict[str, Any]:
        """Return a Mongo filter forced to a single brand_id (the hard boundary)."""
        q = dict(base or {})
        q["brand_id"] = brand_id
        return q

    @staticmethod
    async def assemble_context(
        brand_id: str, user_id: str, db: AsyncIOMotorDatabase
    ) -> Dict[str, Any]:
        """
        Verify access, then load ONLY this brand's data. No cross-brand queries.
        Mirrors the PRD's assembleJaneContext contract.
        """
        if not await AgencyService.user_has_access_to_brand(user_id, brand_id, db):
            raise PermissionError("Access denied to brand")

        sq = lambda base=None: BrandContextService.scoped_query(base, brand_id)

        playbook = await db["brand_profiles"].find_one(sq())
        writing_dna = await db["writing_dna"].find_one(sq())

        for doc in (playbook, writing_dna):
            if doc:
                doc.pop("_id", None)

        return {
            "brand_id": brand_id,
            "playbook": playbook,
            "writing_dna": writing_dna,
            # Callers load lists (calendar/performance/conversation) with sq() as needed.
        }
