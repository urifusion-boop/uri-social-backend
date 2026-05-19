"""
Migration script to add new fields to existing users.

Adds the following fields with defaults:
- role: "user"
- created_at: extracted from ObjectId timestamp
- updated_at: extracted from ObjectId timestamp
- is_active: True
- email_verified: False
- account_status: "active"
- last_login_at: None
- last_seen_at: None
- phone: None
- timezone: "UTC"
- language: "en"

Run this script once to migrate all existing users.
"""

import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime


async def migrate_users():
    # Connect to production database
    client = AsyncIOMotorClient("mongodb://urifusion:UriTest2024%21@4.221.74.63:27018/?authSource=admin")
    db = client["Uri_Insight"]

    print("=" * 80)
    print("USER FIELDS MIGRATION")
    print("=" * 80)

    # Get all users without the new fields
    users_cursor = db["users"].find({})
    users = await users_cursor.to_list(length=None)

    total_users = len(users)
    print(f"\nFound {total_users} users to migrate")

    updated_count = 0
    skipped_count = 0

    for user in users:
        user_id = user.get("userId")
        email = user.get("email")

        # Check if user already has new fields (skip if already migrated)
        if "role" in user and "created_at" in user:
            skipped_count += 1
            print(f"⏭️  Skipped {email} (already migrated)")
            continue

        # Extract timestamp from MongoDB ObjectId
        obj_id = user.get("_id")
        created_from_id = obj_id.generation_time if obj_id else datetime.utcnow()

        # Prepare update document
        update_doc = {}

        # Only add fields that don't exist
        if "role" not in user:
            update_doc["role"] = "user"

        if "created_at" not in user:
            update_doc["created_at"] = created_from_id

        if "updated_at" not in user:
            update_doc["updated_at"] = created_from_id

        if "is_active" not in user:
            update_doc["is_active"] = True

        if "email_verified" not in user:
            # Mark Google auth users as verified
            update_doc["email_verified"] = user.get("auth_provider") == "google"

        if "account_status" not in user:
            update_doc["account_status"] = "active"

        if "last_login_at" not in user:
            update_doc["last_login_at"] = None

        if "last_seen_at" not in user:
            update_doc["last_seen_at"] = None

        if "phone" not in user:
            update_doc["phone"] = None

        if "timezone" not in user:
            update_doc["timezone"] = "UTC"

        if "language" not in user:
            update_doc["language"] = "en"

        # Update the user
        if update_doc:
            result = await db["users"].update_one(
                {"_id": obj_id},
                {"$set": update_doc}
            )

            if result.modified_count > 0:
                updated_count += 1
                print(f"✅ Updated {email} - added {len(update_doc)} fields")
            else:
                print(f"⚠️  {email} - no changes made")
        else:
            skipped_count += 1
            print(f"⏭️  Skipped {email} (no updates needed)")

    print("\n" + "=" * 80)
    print("MIGRATION COMPLETE")
    print("=" * 80)
    print(f"Total users: {total_users}")
    print(f"Updated: {updated_count}")
    print(f"Skipped: {skipped_count}")
    print("=" * 80)

    client.close()


if __name__ == "__main__":
    asyncio.run(migrate_users())
