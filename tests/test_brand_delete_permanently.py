"""
Unit tests for BrandAccountService.delete_brand_permanently() — the hard-delete
counterpart to the existing soft-archive. Removes the brand_account and every
collection scoped by brand_id: the cloned-on-duplicate set (brand_profiles,
writing_dna) plus social_connections and member_brand_access.

Hits the real staging Mongo directly with clearly namespaced, disposable ids,
cleaned up before and after each test.
"""
import os

import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient

from app.services.BrandAccountService import BrandAccountService

MONGODB_URI = os.getenv(
    "MONGODB_URI",
    "mongodb://urifusion:UriTest2024%21@4.221.74.63:27018/Uri_Insight?authSource=admin",
)

_SCOPED_COLLECTIONS = ["brand_accounts", "brand_profiles", "writing_dna",
                       "social_connections", "member_brand_access"]


def _db():
    return AsyncIOMotorClient(MONGODB_URI)["Uri_Insight"]


@pytest_asyncio.fixture
async def doomed_brand():
    """A disposable brand_id with seed data in every scoped collection."""
    brand_id = "TEST_delete_perm_" + os.urandom(6).hex()
    user_id = "TEST_delete_perm_user_" + os.urandom(6).hex()
    db = _db()

    async def _cleanup():
        for coll in _SCOPED_COLLECTIONS:
            await db[coll].delete_many({"brand_id": brand_id})

    await _cleanup()
    yield brand_id, user_id, db
    await _cleanup()


class TestDeleteBrandPermanently:
    @pytest.mark.asyncio
    async def test_removes_the_brand_account_itself(self, doomed_brand):
        brand_id, user_id, db = doomed_brand
        await db["brand_accounts"].insert_one({
            "brand_id": brand_id, "owner_user_id": user_id, "agency_id": "agcy_test",
            "name": "Doomed", "status": "archived",
        })

        counts = await BrandAccountService.delete_brand_permanently(brand_id, db)

        assert counts["brand_accounts"] == 1
        assert await db["brand_accounts"].find_one({"brand_id": brand_id}) is None

    @pytest.mark.asyncio
    async def test_removes_every_scoped_collection(self, doomed_brand):
        brand_id, user_id, db = doomed_brand
        await db["brand_accounts"].insert_one({
            "brand_id": brand_id, "owner_user_id": user_id, "status": "archived", "name": "Doomed",
        })
        await db["brand_profiles"].insert_one({"brand_id": brand_id, "user_id": user_id, "brand_name": "Doomed"})
        await db["writing_dna"].insert_one({"brand_id": brand_id, "some_field": "x"})
        await db["social_connections"].insert_one({
            "id": f"conn_{brand_id}", "brand_id": brand_id, "platform": "facebook", "page_id": "123",
        })
        await db["member_brand_access"].insert_one({"brand_id": brand_id, "agency_member_id": "mem_1"})

        counts = await BrandAccountService.delete_brand_permanently(brand_id, db)

        assert all(counts[c] == 1 for c in _SCOPED_COLLECTIONS)
        for coll in _SCOPED_COLLECTIONS:
            assert await db[coll].count_documents({"brand_id": brand_id}) == 0

    @pytest.mark.asyncio
    async def test_missing_collections_report_zero_not_an_error(self, doomed_brand):
        brand_id, user_id, db = doomed_brand
        await db["brand_accounts"].insert_one({
            "brand_id": brand_id, "owner_user_id": user_id, "status": "archived", "name": "Doomed",
        })
        # No brand_profiles/writing_dna/social_connections/member_brand_access seeded.

        counts = await BrandAccountService.delete_brand_permanently(brand_id, db)

        assert counts["brand_accounts"] == 1
        assert counts["brand_profiles"] == 0
        assert counts["writing_dna"] == 0
        assert counts["social_connections"] == 0
        assert counts["member_brand_access"] == 0

    @pytest.mark.asyncio
    async def test_does_not_touch_a_different_brands_data(self, doomed_brand):
        brand_id, user_id, db = doomed_brand
        other_brand_id = f"{brand_id}_untouched"

        await db["brand_accounts"].insert_one({
            "brand_id": brand_id, "owner_user_id": user_id, "status": "archived", "name": "Doomed",
        })
        await db["brand_accounts"].insert_one({
            "brand_id": other_brand_id, "owner_user_id": user_id, "status": "active", "name": "Safe",
        })
        await db["brand_profiles"].insert_one({"brand_id": other_brand_id, "user_id": user_id, "brand_name": "Safe"})

        await BrandAccountService.delete_brand_permanently(brand_id, db)

        assert await db["brand_accounts"].find_one({"brand_id": other_brand_id}) is not None
        assert await db["brand_profiles"].find_one({"brand_id": other_brand_id}) is not None

        # cleanup the extra brand this test created
        await db["brand_accounts"].delete_many({"brand_id": other_brand_id})
        await db["brand_profiles"].delete_many({"brand_id": other_brand_id})
