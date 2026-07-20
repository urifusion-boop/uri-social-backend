"""
Regression tests for the cross-brand data leak in BrandProfileService (fix/brand-
profile-cross-brand-leak).

A user with several brands has one brand_profiles document per brand, all sharing the
same user_id. get()/save() used to fall back to a bare {"user_id": user_id} scope when
a brand had no document of its own — matching whichever OTHER brand's document Mongo
returned first instead of correctly reporting "no profile yet". Confirmed live on
production: fetching the personal brand or a freshly-created agency brand returned a
completely different, unrelated brand's data.

These tests hit the real staging Mongo directly (no live server needed) with clearly
namespaced, disposable user/brand ids, cleaned up before and after each test.
"""
import os

import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient

from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
from app.models.brand_account import BrandAccount

MONGODB_URI = os.getenv(
    "MONGODB_URI",
    "mongodb://urifusion:UriTest2024%21@4.221.74.63:27018/Uri_Insight?authSource=admin",
)


def _db():
    return AsyncIOMotorClient(MONGODB_URI)["Uri_Insight"]


@pytest_asyncio.fixture
async def isolated_user():
    """A disposable user_id, cleaned up before and after the test."""
    user_id = "TEST_isolation_" + os.urandom(6).hex()
    db = _db()
    await db["brand_profiles"].delete_many({"user_id": user_id})
    await db["brand_accounts"].delete_many({"owner_user_id": user_id})
    yield user_id, db
    await db["brand_profiles"].delete_many({"user_id": user_id})
    await db["brand_accounts"].delete_many({"owner_user_id": user_id})


class TestBrandProfileGetIsolation:
    @pytest.mark.asyncio
    async def test_agency_brand_with_no_doc_does_not_leak_another_brands_data(self, isolated_user):
        user_id, db = isolated_user
        brand_a = f"{user_id}_brand_a"
        brand_b_no_doc = f"{user_id}_brand_b"

        await db["brand_profiles"].insert_one({
            "user_id": user_id, "brand_id": brand_a, "brand_name": "Brand A",
            "industry": "brand-a-industry", "onboarding_completed": True,
        })

        result = await BrandProfileService.get(user_id, db, brand_id=brand_b_no_doc)
        assert result["status"] is False
        assert result.get("responseData") is None

    @pytest.mark.asyncio
    async def test_personal_brand_with_no_doc_does_not_leak_another_brands_data(self, isolated_user):
        user_id, db = isolated_user
        brand_a = f"{user_id}_brand_a"
        personal_bid = BrandAccount.personal_brand_id(user_id)

        await db["brand_profiles"].insert_one({
            "user_id": user_id, "brand_id": brand_a, "brand_name": "Brand A",
            "industry": "brand-a-industry", "onboarding_completed": True,
        })

        result = await BrandProfileService.get(user_id, db, brand_id=personal_bid)
        assert result["status"] is False
        assert result.get("responseData") is None

    @pytest.mark.asyncio
    async def test_brand_with_its_own_doc_is_still_fetched_correctly(self, isolated_user):
        user_id, db = isolated_user
        brand_a = f"{user_id}_brand_a"

        await db["brand_profiles"].insert_one({
            "user_id": user_id, "brand_id": brand_a, "brand_name": "Brand A",
            "industry": "brand-a-industry", "onboarding_completed": True,
        })

        result = await BrandProfileService.get(user_id, db, brand_id=brand_a)
        assert result["status"] is True
        assert result["responseData"]["brand_name"] == "Brand A"
        assert result["responseData"]["brand_id"] == brand_a


class TestBrandProfileSaveIsolation:
    @pytest.mark.asyncio
    async def test_saving_personal_brand_does_not_overwrite_another_brands_doc(self, isolated_user):
        user_id, db = isolated_user
        brand_a = f"{user_id}_brand_a"
        personal_bid = BrandAccount.personal_brand_id(user_id)

        await db["brand_profiles"].insert_one({
            "user_id": user_id, "brand_id": brand_a, "brand_name": "Brand A Untouched",
            "industry": "brand-a-industry", "onboarding_completed": True,
        })

        await BrandProfileService.save(
            user_id, {"brand_name": "My Personal Brand", "industry": "personal-industry",
                       "onboarding_completed": True},
            db, brand_id=personal_bid,
        )

        brand_a_doc = await db["brand_profiles"].find_one({"brand_id": brand_a}, {"_id": 0})
        personal_doc = await db["brand_profiles"].find_one({"brand_id": personal_bid}, {"_id": 0})
        total = await db["brand_profiles"].count_documents({"user_id": user_id})

        assert brand_a_doc is not None
        assert brand_a_doc["brand_name"] == "Brand A Untouched"
        assert personal_doc is not None
        assert personal_doc["brand_name"] == "My Personal Brand"
        assert total == 2


class TestBrandProfileIdentityFallback:
    """fix/brand-profile-identity-fallback: an un-onboarded agency brand should
    inherit STYLE defaults (colors, voice, industry) from the personal brand for
    content generation, but must never borrow the personal brand's brand_name or
    show it as its own identity — confirmed live: the Brand Playbook page was
    showing a completely different brand's name and logo for a brand that had
    simply never completed onboarding yet."""

    @pytest.mark.asyncio
    async def test_unonboarded_agency_brand_keeps_its_own_name(self, isolated_user):
        user_id, db = isolated_user
        personal_bid = BrandAccount.personal_brand_id(user_id)
        agency_brand_id = f"{user_id}_agency_brand"

        await db["brand_accounts"].insert_one({
            "brand_id": personal_bid, "owner_user_id": user_id, "agency_id": None,
            "name": "Real Personal Biz", "status": "active",
        })
        await db["brand_profiles"].insert_one({
            "user_id": user_id, "brand_id": personal_bid, "brand_name": "Real Personal Biz",
            "industry": "Fashion", "brand_colors": ["#111", "#222"],
            "logo_url": "https://cdn/personal-logo.png", "onboarding_completed": True,
        })
        await db["brand_accounts"].insert_one({
            "brand_id": agency_brand_id, "owner_user_id": user_id, "agency_id": "agcy_test",
            "name": "Brand New Sub-Brand", "status": "active",
        })

        result = await BrandProfileService.get(user_id, db, brand_id=agency_brand_id)
        data = result["responseData"]

        assert data["brand_name"] == "Brand New Sub-Brand"
        assert data["brand_colors"] == ["#111", "#222"]
        assert data["industry"] == "Fashion"
        assert data["onboarding_completed"] is False

    @pytest.mark.asyncio
    async def test_agency_brand_with_no_brand_accounts_entry_gets_empty_name_not_borrowed(self, isolated_user):
        # Defensive: if the brand_accounts lookup somehow misses, still must not
        # silently show the personal brand's name.
        user_id, db = isolated_user
        personal_bid = BrandAccount.personal_brand_id(user_id)
        agency_brand_id = f"{user_id}_agency_brand_orphan"

        await db["brand_profiles"].insert_one({
            "user_id": user_id, "brand_id": personal_bid, "brand_name": "Real Personal Biz",
            "industry": "Fashion", "onboarding_completed": True,
        })
        # Note: no brand_accounts entry created for agency_brand_id at all.

        result = await BrandProfileService.get(user_id, db, brand_id=agency_brand_id)
        data = result["responseData"]

        assert data["brand_name"] == ""
        assert data["industry"] == "Fashion"  # style default still applies
