"""
Create MongoDB indexes for billing collections
PRD Section 11: Must log all credit usage events (requires performant queries)

Run this script once to create optimal indexes for the billing system.
"""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings


async def create_billing_indexes():
    """
    Create indexes for all billing-related collections
    Optimizes queries for:
    - Credit balance lookups
    - Transaction history
    - Payment verification
    - Subscription management
    """
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DB]

    print("🔧 Creating billing system indexes...")

    # ==================== user_credits collection ====================
    print("\n📊 user_credits indexes:")

    # Primary lookup: by user_id
    await db.user_credits.create_index("user_id", unique=True)
    print("  ✅ user_id (unique)")

    # Query active subscriptions
    await db.user_credits.create_index("subscription_tier")
    print("  ✅ subscription_tier")

    # Query by renewal date (for auto-renewal jobs)
    await db.user_credits.create_index("next_renewal")
    print("  ✅ next_renewal")

    # Compound index for low credit warnings
    await db.user_credits.create_index([("user_id", 1), ("credits_remaining", 1)])
    print("  ✅ user_id + credits_remaining (compound)")

    # ==================== credit_transactions collection ====================
    print("\n📊 credit_transactions indexes:")

    # Primary lookup: transaction history by user
    await db.credit_transactions.create_index([("user_id", 1), ("created_at", -1)])
    print("  ✅ user_id + created_at (compound, desc)")

    # Lookup transactions by campaign
    await db.credit_transactions.create_index("campaign_id")
    print("  ✅ campaign_id")

    # Query by transaction type (for analytics)
    await db.credit_transactions.create_index("type")
    print("  ✅ type")

    # Query by reason (for audit trails)
    await db.credit_transactions.create_index("reason")
    print("  ✅ reason")

    # ==================== subscription_tiers collection ====================
    print("\n📊 subscription_tiers indexes:")

    # Primary lookup: by tier_id
    await db.subscription_tiers.create_index("tier_id", unique=True)
    print("  ✅ tier_id (unique)")

    # Query active tiers
    await db.subscription_tiers.create_index("is_active")
    print("  ✅ is_active")

    # Sort by price
    await db.subscription_tiers.create_index("price_ngn")
    print("  ✅ price_ngn")

    # ==================== payment_transactions collection ====================
    print("\n📊 payment_transactions indexes:")

    # Primary lookup: by transaction reference (SQUAD ref)
    await db.payment_transactions.create_index("transaction_ref", unique=True)
    print("  ✅ transaction_ref (unique)")

    # Payment history by user
    await db.payment_transactions.create_index([("user_id", 1), ("created_at", -1)])
    print("  ✅ user_id + created_at (compound, desc)")

    # Query by status (pending, completed, failed)
    await db.payment_transactions.create_index("status")
    print("  ✅ status")

    # Query by gateway
    await db.payment_transactions.create_index("gateway")
    print("  ✅ gateway")

    # Compound index for pending payment cleanup jobs
    await db.payment_transactions.create_index([("status", 1), ("created_at", -1)])
    print("  ✅ status + created_at (compound, desc)")

    # ==================== content_requests/drafts (PRD 9: Campaign Tracking) ====================
    print("\n📊 content_requests indexes (for retry tracking):")

    # Query campaigns by user
    await db.content_requests.create_index([("user_id", 1), ("created_at", -1)])
    print("  ✅ user_id + created_at (compound, desc)")

    # Lookup by campaign_id (for credit deduction linking)
    await db.content_requests.create_index("request_id")
    print("  ✅ request_id")

    print("\n✅ All billing indexes created successfully!")
    print("\n📈 Index Summary:")
    print("  - user_credits: 4 indexes")
    print("  - credit_transactions: 4 indexes")
    print("  - subscription_tiers: 3 indexes")
    print("  - payment_transactions: 5 indexes")
    print("  - content_requests: 2 indexes")
    print("  TOTAL: 18 indexes")

    client.close()


if __name__ == "__main__":
    asyncio.run(create_billing_indexes())
