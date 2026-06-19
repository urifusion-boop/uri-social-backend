"""
Migration: introduce brand_id as the isolation boundary for Jane data.

Agency Accounts (PRD §2.2): brand_id, not user_id, is the context boundary for
everything Jane does. This migration is idempotent and safe to re-run.

For every distinct user that owns Jane data, it:
  1. Ensures a personal BrandAccount exists (agency_id=None, deterministic brand_id).
  2. Stamps brand_id = <personal brand_id> on all that user's Jane documents
     across the brand-scoped collections, where brand_id is missing.

Solo SMEs keep working unchanged — their brand_id is a 1:1 personal brand.

Run:  python -m migrations.add_brand_id_to_jane_data
"""

import asyncio
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.models.brand_account import BrandAccount

# Jane collections scoped per-brand. Each holds a user_id we can key off.
JANE_COLLECTIONS = [
    "brand_profiles",
    "writing_dna",
    "blog_posts",
    "content_drafts",
    "content_requests",
    "content_calendar",
    "social_connections",
    "performance_posts",
]

BRANDS = "brand_accounts"


async def migrate():
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DB]

    print("=" * 80)
    print("BRAND_ID MIGRATION — Agency Accounts foundation")
    print("=" * 80)

    # 1. Collect every distinct user_id that owns Jane data
    user_ids = set()
    for coll in JANE_COLLECTIONS:
        try:
            for uid in await db[coll].distinct("user_id"):
                if uid:
                    user_ids.add(uid)
        except Exception as e:
            print(f"  (skip {coll}: {e})")

    # Include all registered users too (so empty accounts still get a brand)
    for uid in await db["users"].distinct("_id"):
        user_ids.add(str(uid))

    print(f"\nFound {len(user_ids)} users with Jane data / accounts.\n")

    brands_created = 0
    docs_stamped = 0

    for user_id in user_ids:
        brand_id = BrandAccount.personal_brand_id(user_id)

        # 1. Ensure personal brand exists
        existing = await db[BRANDS].find_one({"brand_id": brand_id})
        if not existing:
            name = "My Brand"
            industry = None
            profile = await db["brand_profiles"].find_one({"user_id": user_id})
            if profile:
                name = profile.get("brand_name") or name
                industry = profile.get("industry")
            await db[BRANDS].insert_one(
                BrandAccount(
                    brand_id=brand_id,
                    agency_id=None,
                    owner_user_id=user_id,
                    name=name,
                    industry=industry,
                ).to_dict()
            )
            brands_created += 1

        # 2. Stamp brand_id where missing on this user's Jane docs
        for coll in JANE_COLLECTIONS:
            try:
                res = await db[coll].update_many(
                    {"user_id": user_id, "$or": [{"brand_id": {"$exists": False}}, {"brand_id": None}]},
                    {"$set": {"brand_id": brand_id, "updated_at": datetime.utcnow()}},
                )
                docs_stamped += res.modified_count
            except Exception as e:
                print(f"  (skip {coll} for {user_id}: {e})")

    # 3. Helpful indexes (idempotent)
    await db[BRANDS].create_index("brand_id", unique=True)
    await db[BRANDS].create_index([("owner_user_id", 1), ("agency_id", 1)])
    for coll in JANE_COLLECTIONS:
        try:
            await db[coll].create_index("brand_id")
        except Exception:
            pass

    print(f"\n✅ Personal brands created: {brands_created}")
    print(f"✅ Jane documents stamped with brand_id: {docs_stamped}")
    print("✅ Indexes ensured.")
    print("\nMigration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
