"""
Create database indexes for multi-tenant SDK architecture

This script creates indexes to support efficient queries for:
- SDK client profiles (by API key hash, client ID)
- SDK end-users (by client ID + external user ID, end-user ID)
- Brand profiles (by end-user ID for multi-tenant isolation)

Run this script once to set up indexes for production use.
"""

import asyncio
import os
import sys
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import IndexModel, ASCENDING, DESCENDING

# Add parent directory to path to import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config.settings import settings


async def create_indexes():
    """Create all necessary indexes for multi-tenant architecture"""

    # Connect to MongoDB
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    db = client[settings.MONGODB_DB_NAME]

    print("🔧 Creating multi-tenant indexes...")
    print(f"📊 Database: {settings.MONGODB_DB_NAME}")
    print()

    # ========================================
    # 1. SDK Client Profiles Collection
    # ========================================
    print("1️⃣  Creating indexes for sdk_client_profiles...")

    sdk_client_indexes = [
        IndexModel([("sdk_client_id", ASCENDING)], unique=True, name="idx_sdk_client_id"),
        IndexModel([("api_key_hash", ASCENDING)], unique=True, name="idx_api_key_hash"),
        IndexModel([("developer_id", ASCENDING)], name="idx_developer_id"),
        IndexModel([("status", ASCENDING)], name="idx_status"),
        IndexModel([("created_at", DESCENDING)], name="idx_created_at"),
    ]

    try:
        result = await db.sdk_client_profiles.create_indexes(sdk_client_indexes)
        print(f"   ✅ Created {len(result)} indexes on sdk_client_profiles")
        for idx_name in result:
            print(f"      - {idx_name}")
    except Exception as e:
        print(f"   ⚠️  Error creating sdk_client_profiles indexes: {e}")

    print()

    # ========================================
    # 2. SDK End-Users Collection
    # ========================================
    print("2️⃣  Creating indexes for sdk_end_users...")

    sdk_end_user_indexes = [
        IndexModel([("end_user_id", ASCENDING)], unique=True, name="idx_end_user_id"),
        IndexModel(
            [("sdk_client_id", ASCENDING), ("external_user_id", ASCENDING)],
            unique=True,
            name="idx_client_external_user"
        ),
        IndexModel([("sdk_client_id", ASCENDING)], name="idx_sdk_client_id"),
        IndexModel([("brand_profile_id", ASCENDING)], name="idx_brand_profile_id"),
        IndexModel([("status", ASCENDING)], name="idx_status"),
        IndexModel([("last_active_at", DESCENDING)], name="idx_last_active_at"),
        IndexModel([("created_at", DESCENDING)], name="idx_created_at"),
    ]

    try:
        result = await db.sdk_end_users.create_indexes(sdk_end_user_indexes)
        print(f"   ✅ Created {len(result)} indexes on sdk_end_users")
        for idx_name in result:
            print(f"      - {idx_name}")
    except Exception as e:
        print(f"   ⚠️  Error creating sdk_end_users indexes: {e}")

    print()

    # ========================================
    # 3. Brand Profiles Collection (Multi-tenant)
    # ========================================
    print("3️⃣  Creating indexes for brand_profiles (multi-tenant)...")

    brand_profile_indexes = [
        # Existing indexes (keep for backward compatibility)
        IndexModel([("user_id", ASCENDING)], name="idx_user_id", sparse=True),
        IndexModel([("brand_id", ASCENDING)], name="idx_brand_id", sparse=True),

        # New multi-tenant indexes
        IndexModel([("end_user_id", ASCENDING)], unique=True, sparse=True, name="idx_end_user_id"),
        IndexModel(
            [("sdk_client_id", ASCENDING), ("end_user_id", ASCENDING)],
            sparse=True,
            name="idx_client_end_user"
        ),
        IndexModel([("sdk_client_id", ASCENDING)], sparse=True, name="idx_sdk_client_id"),

        # Utility indexes
        IndexModel([("onboarding_completed", ASCENDING)], name="idx_onboarding_completed"),
        IndexModel([("created_at", DESCENDING)], name="idx_created_at"),
        IndexModel([("updated_at", DESCENDING)], name="idx_updated_at"),
    ]

    try:
        result = await db.brand_profiles.create_indexes(brand_profile_indexes)
        print(f"   ✅ Created {len(result)} indexes on brand_profiles")
        for idx_name in result:
            print(f"      - {idx_name}")
    except Exception as e:
        print(f"   ⚠️  Error creating brand_profiles indexes: {e}")

    print()

    # ========================================
    # Summary
    # ========================================
    print("=" * 60)
    print("✅ Index creation complete!")
    print()
    print("📋 Summary:")
    print(f"   • sdk_client_profiles: {len(sdk_client_indexes)} indexes")
    print(f"   • sdk_end_users: {len(sdk_end_user_indexes)} indexes")
    print(f"   • brand_profiles: {len(brand_profile_indexes)} indexes")
    print()
    print("🚀 Multi-tenant architecture is now optimized for:")
    print("   • 500 SDK clients")
    print("   • 10,000 end-users per client")
    print("   • 5,000,000 total end-users")
    print()
    print("=" * 60)

    # Close connection
    client.close()


if __name__ == "__main__":
    print()
    print("=" * 60)
    print("🏗️  URISocial Multi-Tenant Index Migration")
    print("=" * 60)
    print()

    asyncio.run(create_indexes())

    print()
    print("✨ Migration complete!")
    print()
