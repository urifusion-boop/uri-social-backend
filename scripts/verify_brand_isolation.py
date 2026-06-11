"""
Brand Isolation Verification (PRD §2 — the non-negotiable gate).

Creates one agency, two brands (a Pidgin fashion brand + a formal law firm),
one agent assigned to both, then asserts:
  1. Each brand's Writing DNA / profile is loaded ONLY for that brand.
  2. assemble_context never bleeds one brand's data into the other.
  3. A non-member user is denied access to either brand.
  4. Scoped queries are physically constrained to one brand_id.

Cleans up everything it creates. Run:
  python -m scripts.verify_brand_isolation
"""

import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.services.AgencyService import AgencyService, AgencyRole
from app.services.BrandAccountService import BrandAccountService
from app.services.BrandContextService import BrandContextService

ADMIN = "test_admin_user_iso"
AGENT = "test_agent_user_iso"
STRANGER = "test_stranger_user_iso"


async def run():
    db = AsyncIOMotorClient(settings.MONGODB_URI)[settings.MONGODB_DB]
    created_brand_ids = []
    agency_id = None
    failures = []

    def check(cond, msg):
        print(("✅" if cond else "❌") + " " + msg)
        if not cond:
            failures.append(msg)

    try:
        # ── Setup ──────────────────────────────────────────────────────────
        agency = await AgencyService.create_agency("Iso Test Agency", ADMIN, db)
        agency_id = agency.agency_id

        fashion = await BrandAccountService.create_brand(
            owner_user_id=ADMIN, name="Lagos Fashion Co", db=db,
            agency_id=agency_id, industry="fashion",
        )
        law = await BrandAccountService.create_brand(
            owner_user_id=ADMIN, name="Formal Law Partners", db=db,
            agency_id=agency_id, industry="legal",
        )
        created_brand_ids = [fashion.brand_id, law.brand_id]

        # Agent invited, then accepts (status → active), assigned to BOTH brands
        member = await AgencyService.add_member(agency_id, AGENT, AgencyRole.AGENT, ADMIN, db)
        await db["agency_members"].update_one(
            {"agency_member_id": member.agency_member_id}, {"$set": {"status": "active"}}
        )
        await AgencyService.assign_brand(member.agency_member_id, fashion.brand_id, db)
        await AgencyService.assign_brand(member.agency_member_id, law.brand_id, db)

        # Distinct DNA per brand
        await db["writing_dna"].update_one(
            {"brand_id": fashion.brand_id},
            {"$set": {"brand_id": fashion.brand_id, "user_id": ADMIN,
                      "writing_dna_prompt": "Heavy Pidgin. Playful. Lagos market energy."}},
            upsert=True,
        )
        await db["writing_dna"].update_one(
            {"brand_id": law.brand_id},
            {"$set": {"brand_id": law.brand_id, "user_id": ADMIN,
                      "writing_dna_prompt": "Formal corporate English. No slang. Precise."}},
            upsert=True,
        )

        # ── Assertions ─────────────────────────────────────────────────────
        fashion_ctx = await BrandContextService.assemble_context(fashion.brand_id, AGENT, db)
        law_ctx = await BrandContextService.assemble_context(law.brand_id, AGENT, db)

        f_dna = (fashion_ctx["writing_dna"] or {}).get("writing_dna_prompt", "")
        l_dna = (law_ctx["writing_dna"] or {}).get("writing_dna_prompt", "")

        check("Pidgin" in f_dna, "Fashion brand loads its OWN Pidgin DNA")
        check("Pidgin" not in l_dna, "Law brand does NOT get the fashion brand's Pidgin DNA")
        check("Formal" in l_dna, "Law brand loads its OWN formal DNA")
        check(f_dna != l_dna, "The two brands have distinct DNA (no bleed)")

        # Stranger has no access to either brand
        s1 = await AgencyService.user_has_access_to_brand(STRANGER, fashion.brand_id, db)
        s2 = await AgencyService.user_has_access_to_brand(STRANGER, law.brand_id, db)
        check(not s1 and not s2, "Non-member is denied access to both brands")

        # assemble_context refuses the stranger
        denied = False
        try:
            await BrandContextService.assemble_context(fashion.brand_id, STRANGER, db)
        except PermissionError:
            denied = True
        check(denied, "assemble_context raises PermissionError for non-member")

        # scoped_query is physically constrained to one brand
        sq = BrandContextService.scoped_query({"status": "draft"}, fashion.brand_id)
        check(sq.get("brand_id") == fashion.brand_id, "scoped_query forces the brand_id boundary")

        # Agent sees exactly the two assigned brands
        ids = await AgencyService.accessible_brand_ids(AGENT, db)
        check(set(ids) == set(created_brand_ids), "Agent's accessible brands == only the 2 assigned")

    finally:
        # ── Cleanup ──────────────────────────────────────────────────────────
        for bid in created_brand_ids:
            await db["writing_dna"].delete_many({"brand_id": bid})
            await db["brand_profiles"].delete_many({"brand_id": bid})
            await db["brand_accounts"].delete_many({"brand_id": bid})
        if agency_id:
            await db["agencies"].delete_many({"agency_id": agency_id})
            await db["agency_members"].delete_many({"agency_id": agency_id})
            members = []  # member_brand_access cleaned by member id below
        await db["member_brand_access"].delete_many({"brand_id": {"$in": created_brand_ids}})

    print("\n" + ("🎉 ALL ISOLATION CHECKS PASSED" if not failures else f"💥 {len(failures)} CHECK(S) FAILED"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
