"""
Admin Script: Grant Test Credits to Users
Professional approach using existing service layer
"""
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings
from app.database import connect_to_mongo
from app.services.CreditService import credit_service


async def grant_credits_to_user(user_id: str, credits: int = 1000, tier: str = "test"):
    """
    Grant test credits to a specific user

    Args:
        user_id: User ID to grant credits to
        credits: Number of credits to grant (default: 1000)
        tier: Tier identifier (default: "test")
    """
    try:
        # Check if user already has wallet
        existing_wallet = await credit_service.get_user_wallet(user_id)

        if existing_wallet:
            print(f"✅ User {user_id} already has wallet:")
            print(f"   - Current credits: {existing_wallet.credits_remaining}/{existing_wallet.total_credits}")
            print(f"   - Tier: {existing_wallet.subscription_tier}")

            response = input(f"\n⚠️  Replace with {credits} test credits? (yes/no): ")
            if response.lower() != 'yes':
                print("❌ Cancelled - no changes made")
                return False

        # Allocate credits using proper service method
        wallet = await credit_service.allocate_credits(
            user_id=user_id,
            tier_id=tier,
            credits=credits,
            reason="bonus"  # Mark as bonus instead of subscription
        )

        print(f"\n✅ Successfully granted {credits} credits to user {user_id}")
        print(f"   - Total credits: {wallet.total_credits}")
        print(f"   - Credits remaining: {wallet.credits_remaining}")
        print(f"   - Tier: {wallet.subscription_tier}")
        print(f"   - Next renewal: {wallet.next_renewal}")

        return True

    except Exception as e:
        print(f"❌ Error granting credits: {str(e)}")
        return False


async def grant_credits_to_all_users(credits: int = 1000):
    """
    Grant test credits to ALL users without wallets

    Args:
        credits: Number of credits to grant to each user
    """
    client = None
    try:
        # Connect to database
        from app.database import get_db
        db = get_db()
        users_collection = db["users"]

        # Find all users
        all_users = await users_collection.find({}).to_list(length=None)

        print(f"📊 Found {len(all_users)} total users")

        # Check which users have wallets
        users_without_wallets = []
        users_with_wallets = []

        for user in all_users:
            user_id = str(user.get("_id"))
            wallet = await credit_service.get_user_wallet(user_id)

            if wallet:
                users_with_wallets.append({
                    "user_id": user_id,
                    "email": user.get("email"),
                    "credits": wallet.credits_remaining
                })
            else:
                users_without_wallets.append({
                    "user_id": user_id,
                    "email": user.get("email")
                })

        print(f"\n📈 Users with wallets: {len(users_with_wallets)}")
        if users_with_wallets:
            for user in users_with_wallets[:5]:  # Show first 5
                print(f"   - {user['email']}: {user['credits']} credits")
            if len(users_with_wallets) > 5:
                print(f"   ... and {len(users_with_wallets) - 5} more")

        print(f"\n📉 Users without wallets: {len(users_without_wallets)}")
        if users_without_wallets:
            for user in users_without_wallets[:5]:  # Show first 5
                print(f"   - {user['email']}")
            if len(users_without_wallets) > 5:
                print(f"   ... and {len(users_without_wallets) - 5} more")

        if not users_without_wallets:
            print("\n✅ All users already have credit wallets!")
            return True

        # Ask for confirmation
        print(f"\n⚠️  This will grant {credits} test credits to {len(users_without_wallets)} users")
        response = input("Continue? (yes/no): ")

        if response.lower() != 'yes':
            print("❌ Cancelled - no changes made")
            return False

        # Grant credits to each user
        success_count = 0
        failed_count = 0

        for user in users_without_wallets:
            try:
                await credit_service.allocate_credits(
                    user_id=user["user_id"],
                    tier_id="test",
                    credits=credits,
                    reason="bonus"
                )
                print(f"✅ Granted {credits} credits to {user['email']}")
                success_count += 1
            except Exception as e:
                print(f"❌ Failed for {user['email']}: {str(e)}")
                failed_count += 1

        print(f"\n📊 Results:")
        print(f"   - Success: {success_count}")
        print(f"   - Failed: {failed_count}")

        return True

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Main execution"""
    # Initialize database connection
    connect_to_mongo(settings.MONGODB_DB)
    print("✅ Connected to database")

    print("=" * 60)
    print("URI Social - Grant Test Credits")
    print("=" * 60)
    print("\nOptions:")
    print("1. Grant credits to specific user")
    print("2. Grant credits to ALL users without wallets")
    print("3. Exit")

    choice = input("\nSelect option (1-3): ")

    if choice == "1":
        user_id = input("Enter user ID: ").strip()
        credits = input("Enter credits to grant (default: 1000): ").strip()
        credits = int(credits) if credits else 1000

        await grant_credits_to_user(user_id, credits)

    elif choice == "2":
        credits = input("Enter credits to grant per user (default: 1000): ").strip()
        credits = int(credits) if credits else 1000

        await grant_credits_to_all_users(credits)

    elif choice == "3":
        print("👋 Goodbye!")
        return
    else:
        print("❌ Invalid option")


if __name__ == "__main__":
    asyncio.run(main())
