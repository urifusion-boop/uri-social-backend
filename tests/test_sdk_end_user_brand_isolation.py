"""
Regression tests for SDK end-user brand isolation (fix/sdk-end-user-brand-isolation).

Each developer using the SDK can have thousands of their own end-users, each needing
an isolated brand (colors, logo, voice, generated content). The mechanism —
X-End-User-ID -> MultiTenantService.get_or_create_end_user() -> SDKEndUser.brand_profile_id
-- existed on paper (the model field, the client/end-user tracking, even the write-path
link_end_user_to_brand_profile()) but was never actually wired up: both
get_flexible_brand_context() (/social-media/*) and the /api/v1/* SDK surface always
resolved every end-user of a developer onto that developer's own single personal brand.

These tests hit the real staging Mongo directly (no live server needed), mirroring
tests/test_brand_profile_isolation.py's pattern, with clearly namespaced disposable
ids, cleaned up before and after.
"""
import os

import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient

from app.services.MultiTenantService import MultiTenantService
from app.services.BrandAccountService import BrandAccountService
from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService

MONGODB_URI = os.getenv(
    "MONGODB_URI",
    "mongodb://urifusion:UriTest2024%21@4.221.74.63:27018/Uri_Insight?authSource=admin",
)


def _db():
    return AsyncIOMotorClient(MONGODB_URI)["Uri_Insight"]


@pytest_asyncio.fixture
async def isolated_sdk_setup():
    """
    A disposable developer_user_id + sdk_client, cleaned up before and after.
    Yields (developer_user_id, sdk_client_id, db).
    """
    developer_user_id = "TEST_sdkdev_" + os.urandom(6).hex()
    db = _db()

    async def _cleanup():
        # sdk_end_users/brand_profiles/brand_accounts created for each test's
        # end-users are cleaned up per-test (they're keyed by external_user_id /
        # brand_id, not developer_user_id, since that's the whole point of the
        # isolation being tested) — this only clears the client + the
        # developer's own personal-brand docs.
        await db["sdk_client_profiles"].delete_many({"developer_id": developer_user_id})
        await db["brand_profiles"].delete_many({"user_id": developer_user_id})
        await db["brand_accounts"].delete_many({"owner_user_id": developer_user_id})

    await _cleanup()
    yield developer_user_id, db
    await _cleanup()


async def _make_client_and_end_user(developer_user_id, external_user_id, db, external_name=None):
    sdk_client = await MultiTenantService.get_or_create_sdk_client(
        api_key_hash=f"hash_{developer_user_id}",
        api_key_prefix="urisocial_test",
        developer_id=developer_user_id,
        company_name="Test SDK Client",
        db=db,
    )
    end_user = await MultiTenantService.get_or_create_end_user(
        sdk_client_id=sdk_client.sdk_client_id,
        external_user_id=external_user_id,
        external_email=None,
        external_name=external_name,
        db=db,
    )
    return sdk_client, end_user


class TestSDKEndUserBrandIsolation:
    @pytest.mark.asyncio
    async def test_two_end_users_under_same_sdk_client_never_share_a_brand(self, isolated_sdk_setup):
        developer_user_id, db = isolated_sdk_setup

        _, end_user_alpha = await _make_client_and_end_user(developer_user_id, "ext_alpha", db)
        _, end_user_beta = await _make_client_and_end_user(developer_user_id, "ext_beta", db)

        brand_id_alpha = await MultiTenantService.get_or_create_end_user_brand_id(
            end_user_alpha, developer_user_id, db
        )
        brand_id_beta = await MultiTenantService.get_or_create_end_user_brand_id(
            end_user_beta, developer_user_id, db
        )

        assert brand_id_alpha != brand_id_beta

        profile_alpha = await db["brand_profiles"].find_one({"brand_id": brand_id_alpha})
        profile_beta = await db["brand_profiles"].find_one({"brand_id": brand_id_beta})
        assert profile_alpha is not None
        assert profile_beta is not None
        assert profile_alpha["brand_id"] != profile_beta["brand_id"]

        await db["brand_profiles"].delete_many({"brand_id": {"$in": [brand_id_alpha, brand_id_beta]}})
        await db["brand_accounts"].delete_many({"brand_id": {"$in": [brand_id_alpha, brand_id_beta]}})
        await db["sdk_end_users"].delete_many({"external_user_id": {"$in": ["ext_alpha", "ext_beta"]}})

    @pytest.mark.asyncio
    async def test_repeat_calls_same_external_user_id_reuse_the_same_brand(self, isolated_sdk_setup):
        developer_user_id, db = isolated_sdk_setup

        sdk_client, _ = await _make_client_and_end_user(developer_user_id, "ext_repeat", db)

        # First call: creates the brand.
        end_user_first = await MultiTenantService.get_or_create_end_user(
            sdk_client_id=sdk_client.sdk_client_id, external_user_id="ext_repeat",
            external_email=None, external_name=None, db=db,
        )
        brand_id_first = await MultiTenantService.get_or_create_end_user_brand_id(
            end_user_first, developer_user_id, db
        )

        # Second call: re-fetch the end-user (as a real request would) and resolve again.
        end_user_second = await MultiTenantService.get_or_create_end_user(
            sdk_client_id=sdk_client.sdk_client_id, external_user_id="ext_repeat",
            external_email=None, external_name=None, db=db,
        )
        brand_id_second = await MultiTenantService.get_or_create_end_user_brand_id(
            end_user_second, developer_user_id, db
        )

        assert brand_id_first == brand_id_second
        assert end_user_second.brand_profile_id == brand_id_first

        brand_count = await db["brand_accounts"].count_documents({"brand_id": brand_id_first})
        assert brand_count == 1

        await db["brand_profiles"].delete_many({"brand_id": brand_id_first})
        await db["brand_accounts"].delete_many({"brand_id": brand_id_first})
        await db["sdk_end_users"].delete_many({"external_user_id": "ext_repeat"})

    @pytest.mark.asyncio
    async def test_developers_personal_brand_untouched_by_sdk_end_users(self, isolated_sdk_setup):
        developer_user_id, db = isolated_sdk_setup

        personal_brand = await BrandAccountService.get_or_create_personal_brand(developer_user_id, db)
        await BrandProfileService.save(
            developer_user_id,
            {"brand_name": "Developer's Own Brand", "onboarding_completed": True},
            db, brand_id=personal_brand.brand_id,
        )

        _, end_user = await _make_client_and_end_user(developer_user_id, "ext_gamma", db)
        end_user_brand_id = await MultiTenantService.get_or_create_end_user_brand_id(
            end_user, developer_user_id, db
        )

        assert end_user_brand_id != personal_brand.brand_id

        personal_profile = await db["brand_profiles"].find_one({"brand_id": personal_brand.brand_id})
        assert personal_profile["brand_name"] == "Developer's Own Brand"

        await db["brand_profiles"].delete_many({"brand_id": end_user_brand_id})
        await db["brand_accounts"].delete_many({"brand_id": end_user_brand_id})
        await db["sdk_end_users"].delete_many({"external_user_id": "ext_gamma"})

    @pytest.mark.asyncio
    async def test_end_user_brand_name_defaults_to_external_identity(self, isolated_sdk_setup):
        """Guards get_or_create_end_user_brand_id's brand-naming fallback."""
        developer_user_id, db = isolated_sdk_setup

        _, end_user = await _make_client_and_end_user(
            developer_user_id, "ext_delta", db, external_name="Delta Corp"
        )
        brand_id = await MultiTenantService.get_or_create_end_user_brand_id(end_user, developer_user_id, db)

        brand_account = await db["brand_accounts"].find_one({"brand_id": brand_id})
        assert brand_account["name"] == "Delta Corp"

        await db["brand_profiles"].delete_many({"brand_id": brand_id})
        await db["brand_accounts"].delete_many({"brand_id": brand_id})
        await db["sdk_end_users"].delete_many({"external_user_id": "ext_delta"})
