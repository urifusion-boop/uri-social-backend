"""
Cleanup script to permanently delete archived custom visual guides

This script removes all guides with status="archived" to allow users to
re-upload the same images after deletion.

Run with:
    python -m app.scripts.cleanup_archived_guides
"""

import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import os


async def cleanup_archived_guides():
    """Delete all archived custom visual guides"""

    # Get MongoDB connection string
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DB_NAME", "uri_social_db")

    print(f"[Cleanup] Connecting to MongoDB: {mongo_uri}")
    print(f"[Cleanup] Database: {db_name}")

    client = AsyncIOMotorClient(mongo_uri)
    db = client[db_name]

    try:
        # Count archived guides
        archived_count = await db["custom_visual_guides"].count_documents({"status": "archived"})
        print(f"[Cleanup] Found {archived_count} archived guides")

        if archived_count == 0:
            print("[Cleanup] ✅ No archived guides to clean up")
            return

        # Delete all archived guides
        result = await db["custom_visual_guides"].delete_many({"status": "archived"})

        print(f"[Cleanup] ✅ Deleted {result.deleted_count} archived guides")
        print(f"[Cleanup] Users can now re-upload these images")

    except Exception as e:
        print(f"[Cleanup] ❌ Error: {e}")
        raise
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(cleanup_archived_guides())
