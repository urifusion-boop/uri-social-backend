from pymongo import MongoClient
from collections import defaultdict
from datetime import datetime

# Connect to MongoDB
client = MongoClient('mongodb+srv://urisocial:SweetJesus99%40@cluster0.qomenuh.mongodb.net/urisocial?appName=Cluster0')
db = client['urisocial']

print("=== Fixing Duplicate Brand Profiles ===\n")

# Find all duplicates
profiles = list(db.brand_profiles.find({}))
by_user = defaultdict(list)
for p in profiles:
    by_user[p['user_id']].append(p)

duplicates = {uid: profs for uid, profs in by_user.items() if len(profs) > 1}

print(f"Found {len(duplicates)} users with duplicate profiles\n")

# For each user with duplicates, keep the newest one (latest updated_at)
deleted_count = 0
for user_id, user_profiles in duplicates.items():
    print(f"User {user_id}: {len(user_profiles)} profiles")
    
    # Sort by updated_at descending (newest first)
    sorted_profiles = sorted(user_profiles, key=lambda p: p.get('updated_at', datetime.min), reverse=True)
    
    # Keep the newest, delete the rest
    keep = sorted_profiles[0]
    delete = sorted_profiles[1:]
    
    print(f"  Keeping: _id={keep['_id']}, updated={keep.get('updated_at')}")
    
    for dup in delete:
        print(f"  Deleting: _id={dup['_id']}, updated={dup.get('updated_at')}")
        result = db.brand_profiles.delete_one({'_id': dup['_id']})
        deleted_count += result.deleted_count
    
    print()

print(f"Deleted {deleted_count} duplicate profiles\n")

# Create unique index on user_id
print("Creating unique index on user_id...")
try:
    db.brand_profiles.create_index('user_id', unique=True)
    print("✅ Unique index created successfully!")
except Exception as e:
    print(f"❌ Error creating index: {e}")

print("\n=== Done ===")
