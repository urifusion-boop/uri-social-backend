"""
Script to create credit wallets with 0 credits for all users without wallets.
This fixes the issue where users with expired trials have no wallet and bypass credit checks.

Usage:
    export MONGODB_URL="your_mongodb_connection_string"
    python3 create_wallets_for_users_without.py
"""
from pymongo import MongoClient
from datetime import datetime
import os

# MongoDB connection from environment variable
MONGODB_URL = os.getenv('MONGODB_URL')
if not MONGODB_URL:
    raise ValueError("MONGODB_URL environment variable is required")

client = MongoClient(MONGODB_URL)
db = client['urisocial']

# Get all users
all_users = list(db['users'].find({}, {'userId': 1, 'email': 1}))
print(f'Total users in system: {len(all_users)}')
print()

# Find users without credit wallets
users_without_wallets = []
for user in all_users:
    user_id = user.get('userId')
    wallet = db['user_credits'].find_one({'user_id': user_id})

    if not wallet:
        # Check if they have an active trial
        trial = db['user_trials'].find_one({'user_id': user_id})
        has_active_trial = False
        if trial:
            expires_at = trial.get('trial_end_date')
            credits_remaining = trial.get('credits_remaining', 0)
            if expires_at and credits_remaining > 0 and expires_at > datetime.utcnow():
                has_active_trial = True

        users_without_wallets.append({
            'user_id': user_id,
            'email': user.get('email'),
            'has_active_trial': has_active_trial
        })

print(f'Users without credit wallets: {len(users_without_wallets)}')
print()

# Create wallets for users without active trials
created_count = 0
for user in users_without_wallets:
    if not user['has_active_trial']:
        # Create wallet with 0 credits
        wallet = {
            'user_id': user['user_id'],
            'bonus_credits': 0,
            'subscription_credits': 0,
            'total_credits': 0,
            'credits_used': 0,
            'credits_remaining': 0,
            'subscription_tier': None,
            'next_renewal': None,
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        }

        db['user_credits'].insert_one(wallet)
        created_count += 1
        print(f'Created wallet for {user["email"]}')

print()
print(f'Created {created_count} wallets with 0 credits')
print(f'Skipped {len(users_without_wallets) - created_count} users with active trials')
