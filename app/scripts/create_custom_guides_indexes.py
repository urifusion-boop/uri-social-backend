#!/usr/bin/env python3
"""
Create database indexes for custom_visual_guides collections

Run this script once to set up indexes for optimal query performance.
"""

import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient
from core.config import settings


async def create_indexes():
    """Create indexes for custom visual guides collections"""

    # Connect to MongoDB
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client.get_default_database()

    print("Creating indexes for custom_visual_guides...")

    # custom_visual_guides collection
    guides_collection = db["custom_visual_guides"]

    # Index for user queries
    await guides_collection.create_index([("user_id", 1), ("status", 1)])
    print("✓ Created index: user_id + status")

    # Index for user + brand queries
    await guides_collection.create_index([("user_id", 1), ("brand_id", 1)])
    print("✓ Created index: user_id + brand_id")

    # Index for match outcome filtering
    await guides_collection.create_index("match_outcome")
    print("✓ Created index: match_outcome")

    # Index for original image hash (deduplication)
    await guides_collection.create_index([("user_id", 1), ("original_image_hash", 1)], unique=True)
    print("✓ Created index: user_id + original_image_hash (unique)")

    # Index for sorting by upload date
    await guides_collection.create_index([("user_id", 1), ("uploaded_at", -1)])
    print("✓ Created index: user_id + uploaded_at (desc)")

    # guide_usage_events collection
    events_collection = db["guide_usage_events"]

    # Index for guide usage queries
    await events_collection.create_index([("guide_id", 1), ("used_at", -1)])
    print("✓ Created index: guide_id + used_at (desc)")

    print("\n✅ All indexes created successfully!")

    client.close()


if __name__ == "__main__":
    asyncio.run(create_indexes())
