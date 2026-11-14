"""
Database Sync Script - Old VM MongoDB to New VM MongoDB Atlas
Syncs only URISocial-related collections, excluding Uri Insights collections.

COLLECTION LIST: Verified by analyzing actual db["..."] and get_db()["..."] usage in codebase
- Excludes: leads, apollo_data, lazarus (Uri Insights only)
- Includes: WhatsApp, social media, content, billing, and all related collections

This script will:
1. Copy missing collections entirely (e.g. blog_drafts, embeddings, etc.)
2. Sync existing collections by upserting documents based on _id
3. Preserve data that only exists in new VM (like newer subscription_tiers)

Usage:
    python migrations/sync_database_to_atlas.py [--dry-run] [--collection COLLECTION_NAME]

Options:
    --dry-run: Show what would be synced without actually syncing
    --collection NAME: Sync only the specified collection
    --skip-large: Skip collections with >5000 documents for quick testing
"""

import asyncio
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import argparse
from typing import Dict, List, Set

# Database connection strings
OLD_MONGODB_URI = "mongodb://urifusion:UriTest2024!@4.221.74.63:27018/Uri_Insight?authSource=admin"
NEW_MONGODB_URI = "mongodb+srv://urisocial:SweetJesus99%40@cluster0.qomenuh.mongodb.net/urisocial?appName=Cluster0"

# URISocial collections to sync (verified by analyzing codebase collection usage)
URISOCIAL_COLLECTIONS = [
    # Core settings
    "app_settings",

    # WhatsApp & Chat
    "agent_chat_messages",
    "whatsapp_sessions",

    # Social Media Connections
    "social_connections",
    "pending_page_tokens",
    "linkedin_oauth_pending",
    "x_oauth_pending",

    # Content Management
    "content_drafts",
    "content_requests",
    "content_calendar_plans",
    "content_analytics",

    # Brand & Auto Content
    "brand_profiles",
    "auto_content_settings",
    "account_analytics_context",

    # Media Generation
    "image_versions",
    "video_generation_jobs",
    "video_publish_jobs",
    "storyboard_frame_jobs",
    "blog_drafts",
    "drafts",

    # AI & Embeddings
    "embeddings",
    "influencers",
    "ai_image_generations",
    "ai_prompt_templates",
    "content_templates",

    # Billing & Credits
    "user_credits",
    "credit_transactions",
    "payment_transactions",
    "subscription_tiers",
    "user_trials",

    # Users
    "users",

    # System
    "cache",
    "trends_cache",
    "scheduler_locks",
    "notifications",
    "bug_reports",
]

# Collections that exist only in old VM (need to copy entirely)
MISSING_IN_NEW = ["blog_drafts"]


class DatabaseSyncService:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.old_client = None
        self.new_client = None
        self.old_db = None
        self.new_db = None
        self.stats = {
            "collections_processed": 0,
            "documents_inserted": 0,
            "documents_updated": 0,
            "documents_skipped": 0,
            "errors": 0,
        }

    async def connect(self):
        """Connect to both databases."""
        print("🔌 Connecting to databases...")
        self.old_client = AsyncIOMotorClient(OLD_MONGODB_URI, serverSelectionTimeoutMS=10000)
        self.new_client = AsyncIOMotorClient(NEW_MONGODB_URI, serverSelectionTimeoutMS=10000)

        # Test connections
        await self.old_client.admin.command("ping")
        await self.new_client.admin.command("ping")

        self.old_db = self.old_client["Uri_Insight"]
        self.new_db = self.new_client["urisocial"]

        print("✅ Connected to both databases\n")

    async def disconnect(self):
        """Close database connections."""
        if self.old_client:
            self.old_client.close()
        if self.new_client:
            self.new_client.close()
        print("\n🔌 Disconnected from databases")

    async def get_collection_stats(self, collection_name: str) -> Dict:
        """Get document counts for a collection in both databases."""
        old_count = await self.old_db[collection_name].count_documents({})
        new_count = await self.new_db[collection_name].count_documents({})
        return {
            "collection": collection_name,
            "old_count": old_count,
            "new_count": new_count,
            "difference": old_count - new_count,
        }

    async def sync_collection(self, collection_name: str, skip_large: bool = False) -> Dict:
        """
        Sync a collection from old DB to new DB.
        Strategy:
        - For collections missing in new VM: Copy all documents
        - For existing collections: Upsert documents by _id (insert new, update existing)
        """
        print(f"\n📦 Processing collection: {collection_name}")

        stats = await self.get_collection_stats(collection_name)
        old_count = stats["old_count"]
        new_count = stats["new_count"]

        print(f"   Old VM: {old_count} documents")
        print(f"   New VM: {new_count} documents")
        print(f"   Difference: {stats['difference']}")

        if old_count == 0:
            print(f"   ⏭️  Skipping {collection_name} (empty in old VM)")
            return {"inserted": 0, "updated": 0, "skipped": old_count, "errors": 0}

        if skip_large and old_count > 5000:
            print(f"   ⏭️  Skipping {collection_name} (--skip-large flag, {old_count} docs)")
            return {"inserted": 0, "updated": 0, "skipped": old_count, "errors": 0}

        if self.dry_run:
            print(f"   🔍 [DRY RUN] Would sync {old_count} documents")
            return {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

        # Get all _id values from new VM to determine what exists
        print(f"   📥 Fetching existing IDs from new VM...")
        existing_ids: Set[ObjectId] = set()
        async for doc in self.new_db[collection_name].find({}, {"_id": 1}):
            existing_ids.add(doc["_id"])

        print(f"   📤 Syncing documents from old VM...")
        inserted = 0
        updated = 0
        skipped = 0
        errors = 0

        # Process in batches for better performance
        batch_size = 100
        batch = []

        cursor = self.old_db[collection_name].find({})
        async for doc in cursor:
            doc_id = doc["_id"]

            # Check if document exists in new VM
            if doc_id in existing_ids:
                # Document exists - update it
                batch.append({"update": doc})
            else:
                # Document doesn't exist - insert it
                batch.append({"insert": doc})

            # Process batch when it reaches batch_size
            if len(batch) >= batch_size:
                result = await self._process_batch(collection_name, batch)
                inserted += result["inserted"]
                updated += result["updated"]
                errors += result["errors"]
                batch = []

                # Progress indicator
                processed = inserted + updated + errors
                if processed % 500 == 0:
                    print(f"   Progress: {processed}/{old_count} documents processed...")

        # Process remaining batch
        if batch:
            result = await self._process_batch(collection_name, batch)
            inserted += result["inserted"]
            updated += result["updated"]
            errors += result["errors"]

        print(f"   ✅ Completed:")
        print(f"      Inserted: {inserted}")
        print(f"      Updated: {updated}")
        print(f"      Errors: {errors}")

        return {"inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors}

    async def _process_batch(self, collection_name: str, batch: List[Dict]) -> Dict:
        """Process a batch of insert/update operations."""
        inserted = 0
        updated = 0
        errors = 0

        for item in batch:
            try:
                if "insert" in item:
                    await self.new_db[collection_name].insert_one(item["insert"])
                    inserted += 1
                elif "update" in item:
                    doc = item["update"]
                    await self.new_db[collection_name].replace_one(
                        {"_id": doc["_id"]},
                        doc,
                        upsert=True
                    )
                    updated += 1
            except Exception as e:
                errors += 1
                print(f"      ⚠️  Error processing document: {e}")

        return {"inserted": inserted, "updated": updated, "errors": errors}

    async def sync_all_collections(self, skip_large: bool = False):
        """Sync all URISocial collections."""
        print("=" * 70)
        print("📊 DATABASE SYNC - Old VM → New VM MongoDB Atlas")
        print("=" * 70)
        print(f"Mode: {'🔍 DRY RUN' if self.dry_run else '🔥 LIVE SYNC'}")
        print(f"Collections to sync: {len(URISOCIAL_COLLECTIONS)}")
        print("=" * 70)

        for collection_name in URISOCIAL_COLLECTIONS:
            try:
                result = await self.sync_collection(collection_name, skip_large=skip_large)
                self.stats["collections_processed"] += 1
                self.stats["documents_inserted"] += result["inserted"]
                self.stats["documents_updated"] += result["updated"]
                self.stats["documents_skipped"] += result["skipped"]
                self.stats["errors"] += result["errors"]
            except Exception as e:
                print(f"   ❌ Error syncing {collection_name}: {e}")
                self.stats["errors"] += 1

        # Print final summary
        print("\n" + "=" * 70)
        print("📊 SYNC SUMMARY")
        print("=" * 70)
        print(f"Collections processed: {self.stats['collections_processed']}/{len(URISOCIAL_COLLECTIONS)}")
        print(f"Documents inserted: {self.stats['documents_inserted']}")
        print(f"Documents updated: {self.stats['documents_updated']}")
        print(f"Documents skipped: {self.stats['documents_skipped']}")
        print(f"Errors: {self.stats['errors']}")
        print("=" * 70)

    async def sync_single_collection(self, collection_name: str):
        """Sync a single collection."""
        if collection_name not in URISOCIAL_COLLECTIONS:
            print(f"❌ Collection '{collection_name}' is not in URISocial collections list")
            print(f"Available collections: {', '.join(URISOCIAL_COLLECTIONS)}")
            return

        print("=" * 70)
        print(f"📊 SYNCING SINGLE COLLECTION: {collection_name}")
        print("=" * 70)
        print(f"Mode: {'🔍 DRY RUN' if self.dry_run else '🔥 LIVE SYNC'}")
        print("=" * 70)

        try:
            result = await self.sync_collection(collection_name)
            print("\n" + "=" * 70)
            print("📊 SYNC SUMMARY")
            print("=" * 70)
            print(f"Documents inserted: {result['inserted']}")
            print(f"Documents updated: {result['updated']}")
            print(f"Errors: {result['errors']}")
            print("=" * 70)
        except Exception as e:
            print(f"❌ Error syncing {collection_name}: {e}")


async def main():
    parser = argparse.ArgumentParser(description="Sync database from old VM to new VM MongoDB Atlas")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synced without syncing")
    parser.add_argument("--collection", type=str, help="Sync only the specified collection")
    parser.add_argument("--skip-large", action="store_true", help="Skip collections with >5000 documents")

    args = parser.parse_args()

    service = DatabaseSyncService(dry_run=args.dry_run)

    try:
        await service.connect()

        if args.collection:
            await service.sync_single_collection(args.collection)
        else:
            await service.sync_all_collections(skip_large=args.skip_large)

    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await service.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
