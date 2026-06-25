"""
Script to create credit wallets with 0 credits for all users without wallets.
This fixes the issue where users with expired trials have no wallet and bypass credit checks.
"""
from pymongo import MongoClient
from datetime import datetime

# MongoDB connection
client = MongoClient('mongodb+srv://urisocial:SweetJesus99%40@cluster0.qomenuh.mongodb.net/urisocial?appName=Cluster0')
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
