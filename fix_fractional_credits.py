#!/usr/bin/env python3
"""
Fix fractional credits in MongoDB.
The credit system expects all credit values to be integers, but some wallets/transactions
have fractional values (e.g., 436.5) from old code that used 0.5 credit deductions.

This script rounds all fractional values to integers.
"""

import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient


async def fix_fractional_credits():
    # Get MongoDB connection string from environment
    mongo_uri = os.getenv('MONGO_URI', 'mongodb://localhost:27017')

    print(f"Connecting to MongoDB...")
    client = AsyncIOMotorClient(mongo_uri)
    db = client['uri_agent_db']

    try:
        # Fix user_credits collection
        print("\n=== Fixing user_credits collection ===")
        credits_collection = db['user_credits']

        # Use MongoDB aggregation pipeline to round all fractional fields
        result = await credits_collection.update_many(
            {},
            [
                {'$set': {
                    'subscription_credits': {'$toInt': {'$round': '$subscription_credits'}},
                    'bonus_credits': {'$toInt': {'$round': '$bonus_credits'}},
                    'total_credits': {'$toInt': {'$round': '$total_credits'}},
                    'credits_used': {'$toInt': {'$round': '$credits_used'}},
                    'credits_remaining': {'$toInt': {'$round': '$credits_remaining'}}
                }}
            ]
        )
        print(f"✅ Updated {result.modified_count} user credit wallets")

        # Fix credit_transactions collection
        print("\n=== Fixing credit_transactions collection ===")
        transactions_collection = db['credit_transactions']

        tx_result = await transactions_collection.update_many(
            {},
            [
                {'$set': {
                    'amount': {'$toInt': {'$round': '$amount'}},
                    'balance_before': {'$toInt': {'$round': '$balance_before'}},
                    'balance_after': {'$toInt': {'$round': '$balance_after'}}
                }}
            ]
        )
        print(f"✅ Updated {tx_result.modified_count} credit transactions")

        # Verify a sample wallet
        print("\n=== Verifying fixes ===")
        sample_wallet = await credits_collection.find_one({})
        if sample_wallet:
            print(f"Sample wallet (user_id={sample_wallet.get('user_id')}):")
            print(f"  subscription_credits: {sample_wallet.get('subscription_credits')} (type: {type(sample_wallet.get('subscription_credits')).__name__})")
            print(f"  bonus_credits: {sample_wallet.get('bonus_credits')} (type: {type(sample_wallet.get('bonus_credits')).__name__})")
            print(f"  credits_remaining: {sample_wallet.get('credits_remaining')} (type: {type(sample_wallet.get('credits_remaining')).__name__})")

        print("\n✅ Migration completed successfully!")

    finally:
        client.close()


if __name__ == '__main__':
    asyncio.run(fix_fractional_credits())
