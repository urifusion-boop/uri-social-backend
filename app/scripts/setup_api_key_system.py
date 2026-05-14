"""
Setup Script for API Key Authentication System

Run this script to set up the API key authentication system in your MongoDB database.
This creates the necessary indexes and validates the configuration.

Usage:
    python -m app.scripts.setup_api_key_system
"""

import asyncio
import sys
import os
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings


async def create_api_key_indexes():
    """Create indexes for api_keys collection"""

    print("🔧 Connecting to MongoDB...")
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    db = client[settings.DATABASE_NAME]

    try:
        # Create indexes
        print("📊 Creating indexes for api_keys collection...")

        # 1. Unique index on key_hash (for fast lookups)
        await db.api_keys.create_index(
            "key_hash",
            unique=True,
            name="idx_key_hash_unique"
        )
        print("✅ Created unique index on key_hash")

        # 2. Index on user_id (for listing user's keys)
        await db.api_keys.create_index(
            "user_id",
            name="idx_user_id"
        )
        print("✅ Created index on user_id")

        # 3. Compound index on user_id + status (for active key queries)
        await db.api_keys.create_index(
            [("user_id", 1), ("status", 1)],
            name="idx_user_id_status"
        )
        print("✅ Created compound index on user_id + status")

        # 4. Index on status (for admin queries)
        await db.api_keys.create_index(
            "status",
            name="idx_status"
        )
        print("✅ Created index on status")

        # 5. Index on expires_at (for cleanup jobs)
        await db.api_keys.create_index(
            "expires_at",
            name="idx_expires_at",
            sparse=True  # Only index documents with expires_at field
        )
        print("✅ Created sparse index on expires_at")

        # 6. Index on created_at (for sorting)
        await db.api_keys.create_index(
            "created_at",
            name="idx_created_at"
        )
        print("✅ Created index on created_at")

        # List all indexes
        print("\n📋 Current indexes on api_keys collection:")
        indexes = await db.api_keys.list_indexes().to_list(length=None)
        for idx in indexes:
            print(f"  - {idx.get('name')}: {idx.get('key')}")

        print("\n✅ All indexes created successfully!")

    except Exception as e:
        print(f"\n❌ Error creating indexes: {e}")
        raise
    finally:
        client.close()
        print("\n🔌 Database connection closed")


async def validate_configuration():
    """Validate environment configuration"""

    print("\n🔍 Validating configuration...")

    errors = []
    warnings = []

    # Check MongoDB URL
    if not hasattr(settings, "MONGODB_URL") or not settings.MONGODB_URL:
        errors.append("MONGODB_URL not configured")
    else:
        print(f"✅ MONGODB_URL: {settings.MONGODB_URL[:20]}...")

    # Check Database Name
    if not hasattr(settings, "DATABASE_NAME") or not settings.DATABASE_NAME:
        errors.append("DATABASE_NAME not configured")
    else:
        print(f"✅ DATABASE_NAME: {settings.DATABASE_NAME}")

    # Check CRON_SECRET
    if not hasattr(settings, "CRON_SECRET") or not settings.CRON_SECRET:
        warnings.append("CRON_SECRET not configured - rate limit resets won't work")
    else:
        print(f"✅ CRON_SECRET: {'*' * 20}")

    # Check CORS configuration
    if not hasattr(settings, "ENVIRONMENT"):
        warnings.append("ENVIRONMENT not set - defaulting to 'development'")
    else:
        print(f"✅ ENVIRONMENT: {settings.ENVIRONMENT}")

    # Check JWT secret (for API key management endpoints)
    if not hasattr(settings, "JWT_SECRET") or not settings.JWT_SECRET:
        warnings.append("JWT_SECRET not configured - API key management endpoints won't work")
    else:
        print(f"✅ JWT_SECRET: {'*' * 20}")

    # Print results
    print("\n" + "=" * 60)
    if errors:
        print("❌ ERRORS (must fix):")
        for error in errors:
            print(f"  - {error}")

    if warnings:
        print("\n⚠️  WARNINGS (recommended to fix):")
        for warning in warnings:
            print(f"  - {warning}")

    if not errors and not warnings:
        print("✅ All configuration checks passed!")

    print("=" * 60)

    if errors:
        print("\n❌ Cannot proceed with errors. Please fix configuration.")
        sys.exit(1)


async def test_database_connection():
    """Test MongoDB connection"""

    print("\n🧪 Testing database connection...")

    try:
        client = AsyncIOMotorClient(settings.MONGODB_URL)
        db = client[settings.DATABASE_NAME]

        # Try to ping the database
        await client.admin.command('ping')
        print("✅ Successfully connected to MongoDB")

        # Check if api_keys collection exists
        collections = await db.list_collection_names()
        if "api_keys" in collections:
            count = await db.api_keys.count_documents({})
            print(f"✅ api_keys collection exists ({count} documents)")
        else:
            print("ℹ️  api_keys collection will be created on first insert")

        client.close()
        return True

    except Exception as e:
        print(f"❌ Failed to connect to MongoDB: {e}")
        return False


async def create_sample_api_key():
    """Create a sample API key for testing (optional)"""

    response = input("\n❓ Do you want to create a sample API key for testing? (y/n): ")

    if response.lower() != 'y':
        print("⏭️  Skipping sample API key creation")
        return

    from app.models.api_key import APIKey, APIKeyScope
    from datetime import datetime, timedelta

    print("\n📝 Creating sample API key...")

    client = AsyncIOMotorClient(settings.MONGODB_URL)
    db = client[settings.DATABASE_NAME]

    try:
        # Generate API key
        api_key_string = APIKey.generate_api_key()
        key_hash = APIKey.hash_api_key(api_key_string)

        # Create API key object
        api_key = APIKey(
            user_id="test_user_123",
            key_hash=key_hash,
            key_prefix=APIKey.get_key_prefix(api_key_string),
            name="Test API Key",
            description="Sample API key for testing SDK integration",
            scopes=APIKeyScope.get_default_scopes(),
            status="active",
            environment="development",
            expires_at=datetime.utcnow() + timedelta(days=30)
        )

        # Insert into database
        result = await db.api_keys.insert_one(api_key.to_dict())

        print("\n✅ Sample API key created successfully!")
        print("\n" + "=" * 60)
        print("🔑 API KEY (save this - you won't see it again!):")
        print(f"   {api_key_string}")
        print("\n📋 Details:")
        print(f"   ID: {result.inserted_id}")
        print(f"   Name: {api_key.name}")
        print(f"   User ID: {api_key.user_id}")
        print(f"   Environment: {api_key.environment}")
        print(f"   Expires: {api_key.expires_at.isoformat() if api_key.expires_at else 'Never'}")
        print(f"   Scopes: {', '.join(api_key.scopes[:3])}... ({len(api_key.scopes)} total)")
        print("=" * 60)

        print("\n🧪 Test this API key with:")
        print(f'   curl -H "X-API-Key: {api_key_string}" http://localhost:8000/api/v1/billing/credits')

    except Exception as e:
        print(f"❌ Failed to create sample API key: {e}")
    finally:
        client.close()


async def main():
    """Main setup function"""

    print("=" * 60)
    print("🚀 URI Social API Key System Setup")
    print("=" * 60)

    # Step 1: Validate configuration
    await validate_configuration()

    # Step 2: Test database connection
    connected = await test_database_connection()
    if not connected:
        print("\n❌ Setup failed - cannot connect to database")
        sys.exit(1)

    # Step 3: Create indexes
    await create_api_key_indexes()

    # Step 4: Optional - Create sample API key
    await create_sample_api_key()

    print("\n" + "=" * 60)
    print("✅ Setup complete!")
    print("=" * 60)
    print("\n📚 Next steps:")
    print("   1. Integrate SDK routers into main.py (see INTEGRATION_GUIDE.md)")
    print("   2. Configure cron jobs for rate limit resets (see app/cron/reset_api_key_limits.py)")
    print("   3. Test API key authentication with sample requests")
    print("   4. Create API keys for your users via dashboard")
    print("\n🔗 Documentation:")
    print("   - API Key Management: /social-media/api-keys/*")
    print("   - SDK Endpoints: /api/v1/*")
    print("   - Rate Limit Resets: /cron/reset-*-limits")
    print("\n")


if __name__ == "__main__":
    asyncio.run(main())
