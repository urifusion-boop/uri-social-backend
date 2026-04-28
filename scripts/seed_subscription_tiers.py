#!/usr/bin/env python3
"""
Database Seed Script: Subscription Tiers with Multi-Duration Pricing
PRD: Subscription Plan Upgrade (Multi-Duration with 5% Bulk Discount)

This script updates existing subscription tiers or creates new ones with multi-duration pricing.

Usage:
    python scripts/seed_subscription_tiers.py

The script will:
1. Connect to MongoDB
2. Update existing tiers with multi-duration pricing fields
3. Create new tiers if they don't exist
4. Apply 5% discount formula: (monthly_price × months) × 0.95
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path to import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
from app.core.config import settings


# PRD Section 6: 5% discount on all non-monthly plans
DISCOUNT_RATE = 0.95


async def seed_subscription_tiers():
    """
    Seed or update subscription tiers with multi-duration pricing
    """
    print("🌱 Starting subscription tier seeding...")
    print(f"📡 Connecting to MongoDB: {settings.MONGODB_URI}")

    # Connect to MongoDB
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DB]
    tiers_collection = db["subscription_tiers"]

    # Define tiers with multi-duration pricing
    tiers_data = [
        {
            "tier_id": "starter",
            "name": "Starter Plan",
            "price_ngn_monthly": 15000,
            "price_ngn_3months": int(15000 * 3 * DISCOUNT_RATE),  # ₦42,750
            "price_ngn_6months": int(15000 * 6 * DISCOUNT_RATE),  # ₦85,500
            "price_ngn_12months": int(15000 * 12 * DISCOUNT_RATE),  # ₦171,000
            "credits_monthly": 20,
            "price_ngn": 15000,  # Legacy
            "credits": 20,  # Legacy
            "price_per_credit": 750,
            "features": [
                "20 Campaigns/Month",
                "AI Image Generation",
                "Multi-platform Formatting",
                "Email Support"
            ],
            "is_active": True
        },
        {
            "tier_id": "growth",
            "name": "Growth Plan",
            "price_ngn_monthly": 25000,
            "price_ngn_3months": int(25000 * 3 * DISCOUNT_RATE),  # ₦71,250
            "price_ngn_6months": int(25000 * 6 * DISCOUNT_RATE),  # ₦142,500
            "price_ngn_12months": int(25000 * 12 * DISCOUNT_RATE),  # ₦285,000
            "credits_monthly": 35,
            "price_ngn": 25000,  # Legacy
            "credits": 35,  # Legacy
            "price_per_credit": 714,
            "features": [
                "35 Campaigns/Month",
                "AI Image Generation",
                "Multi-platform Formatting",
                "Priority Support",
                "Advanced Analytics"
            ],
            "is_active": True
        },
        {
            "tier_id": "pro",
            "name": "Pro Plan",
            "price_ngn_monthly": 40000,
            "price_ngn_3months": int(40000 * 3 * DISCOUNT_RATE),  # ₦114,000
            "price_ngn_6months": int(40000 * 6 * DISCOUNT_RATE),  # ₦228,000
            "price_ngn_12months": int(40000 * 12 * DISCOUNT_RATE),  # ₦456,000
            "credits_monthly": 50,
            "price_ngn": 40000,  # Legacy
            "credits": 50,  # Legacy
            "price_per_credit": 800,
            "features": [
                "50 Campaigns/Month",
                "AI Image Generation",
                "Multi-platform Formatting",
                "Priority Support",
                "Advanced Analytics",
                "Team Collaboration"
            ],
            "is_active": True
        },
        {
            "tier_id": "agency",
            "name": "Agency Plan",
            "price_ngn_monthly": 80000,
            "price_ngn_3months": int(80000 * 3 * DISCOUNT_RATE),  # ₦228,000
            "price_ngn_6months": int(80000 * 6 * DISCOUNT_RATE),  # ₦456,000
            "price_ngn_12months": int(80000 * 12 * DISCOUNT_RATE),  # ₦912,000
            "credits_monthly": 100,
            "price_ngn": 80000,  # Legacy
            "credits": 100,  # Legacy
            "price_per_credit": 800,
            "features": [
                "100 Campaigns/Month",
                "AI Image Generation",
                "Multi-platform Formatting",
                "Dedicated Support",
                "Advanced Analytics",
                "Team Collaboration",
                "White Label Options"
            ],
            "is_active": True
        },
        {
            "tier_id": "custom",
            "name": "Custom Plan",
            "price_ngn_monthly": 750,
            "price_ngn_3months": 750,
            "price_ngn_6months": 750,
            "price_ngn_12months": 750,
            "credits_monthly": 1,
            "price_ngn": 750,  # Legacy
            "credits": 1,  # Legacy
            "price_per_credit": 750,
            "features": [
                "Pay Per Credit",
                "₦750 per Campaign",
                "All Features Included",
                "No Monthly Commitment"
            ],
            "is_active": True
        }
    ]

    created_count = 0
    updated_count = 0

    for tier_data in tiers_data:
        tier_id = tier_data["tier_id"]

        # Check if tier exists
        existing_tier = await tiers_collection.find_one({"tier_id": tier_id})

        tier_data["updated_at"] = datetime.utcnow()

        if existing_tier:
            # Update existing tier
            result = await tiers_collection.update_one(
                {"tier_id": tier_id},
                {"$set": tier_data}
            )
            if result.modified_count > 0:
                updated_count += 1
                print(f"✅ Updated tier: {tier_id} - {tier_data['name']}")
                print(f"   Monthly: ₦{tier_data['price_ngn_monthly']:,}")
                print(f"   3-month: ₦{tier_data['price_ngn_3months']:,} (save 5%)")
                print(f"   6-month: ₦{tier_data['price_ngn_6months']:,} (save 5%)")
                print(f"   12-month: ₦{tier_data['price_ngn_12months']:,} (save 5%)")
            else:
                print(f"ℹ️  No changes for tier: {tier_id}")
        else:
            # Create new tier
            tier_data["created_at"] = datetime.utcnow()
            await tiers_collection.insert_one(tier_data)
            created_count += 1
            print(f"✨ Created new tier: {tier_id} - {tier_data['name']}")
            print(f"   Monthly: ₦{tier_data['price_ngn_monthly']:,}")
            print(f"   3-month: ₦{tier_data['price_ngn_3months']:,} (save 5%)")
            print(f"   6-month: ₦{tier_data['price_ngn_6months']:,} (save 5%)")
            print(f"   12-month: ₦{tier_data['price_ngn_12months']:,} (save 5%)")

    # Summary
    print("\n" + "=" * 60)
    print(f"🎉 Seeding complete!")
    print(f"   Created: {created_count} new tier(s)")
    print(f"   Updated: {updated_count} existing tier(s)")
    print(f"   Total: {len(tiers_data)} tier(s) in database")
    print("=" * 60)

    # Close connection
    client.close()


if __name__ == "__main__":
    asyncio.run(seed_subscription_tiers())
