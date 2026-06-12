"""
Migration script to add Canvas Editor fields to existing collections

This script:
1. Adds canvas_editor_enabled flag to brand_profiles
2. Adds document/preview_url fields to content_drafts
3. Creates document_edits collection
4. Creates draft_renders collection

Run with: python -m app.scripts.add_canvas_editor_fields
"""

import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import os
from datetime import datetime


async def add_canvas_editor_fields():
    """Add Canvas Editor fields to database collections"""

    # Get MongoDB connection
    mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGODB_DB", "uri_social_db")

    print(f"[Migration] Connecting to MongoDB: {mongo_uri}")
    print(f"[Migration] Database: {db_name}")

    client = AsyncIOMotorClient(mongo_uri)
    db = client[db_name]

    try:
        # 1. Add canvas_editor_enabled to brand_profiles
        print("\n[1/4] Adding canvas_editor_enabled to brand_profiles...")
        result = await db["brand_profiles"].update_many(
            {"canvas_editor_enabled": {"$exists": False}},
            {"$set": {
                "canvas_editor_enabled": False,  # Disabled by default
                "updated_at": datetime.utcnow()
            }}
        )
        print(f"✅ Updated {result.modified_count} brand profiles")

        # 2. Add document fields to content_drafts
        print("\n[2/4] Adding document fields to content_drafts...")
        result = await db["content_drafts"].update_many(
            {"document": {"$exists": False}},
            {"$set": {
                "document": None,           # Layered JSON document
                "document_version": 1,      # Version tracking
                "preview_url": None,        # Rendered preview URL
                "editor_metadata": {}       # Additional editor metadata
            }}
        )
        print(f"✅ Updated {result.modified_count} content drafts")

        # 3. Create document_edits collection (if not exists)
        print("\n[3/4] Creating document_edits collection...")
        collections = await db.list_collection_names()
        if "document_edits" not in collections:
            await db.create_collection("document_edits")
            # Create index for fast lookups
            await db["document_edits"].create_index([("draft_id", 1), ("created_at", -1)])
            await db["document_edits"].create_index([("user_id", 1)])
            print("✅ Created document_edits collection with indexes")
        else:
            print("✅ document_edits collection already exists")

        # 4. Create draft_renders collection (if not exists)
        print("\n[4/4] Creating draft_renders collection...")
        if "draft_renders" not in collections:
            await db.create_collection("draft_renders")
            # Create unique index for (draft_id, aspect_ratio, document_version)
            await db["draft_renders"].create_index(
                [("draft_id", 1), ("aspect_ratio", 1), ("document_version", 1)],
                unique=True
            )
            await db["draft_renders"].create_index([("draft_id", 1)])
            print("✅ Created draft_renders collection with indexes")
        else:
            print("✅ draft_renders collection already exists")

        print("\n" + "="*60)
        print("✅ Migration completed successfully!")
        print("="*60)
        print("\nNext steps:")
        print("1. Enable canvas editor for test users:")
        print("   db.brand_profiles.updateOne(")
        print("     {user_id: 'YOUR_USER_ID'},")
        print("     {$set: {canvas_editor_enabled: true}}")
        print("   )")
        print("\n2. Test image generation with layered documents")

    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        raise
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(add_canvas_editor_fields())
