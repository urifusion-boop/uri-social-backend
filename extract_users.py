#!/usr/bin/env python3
"""
Extract all users from MongoDB database
Outputs: CSV file with name, email, registration date
"""
import asyncio
import csv
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB", "uri_social")


async def extract_users():
    """Extract all users from database"""

    print("Connecting to MongoDB...")
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client[MONGODB_DB]

    # Get users collection
    users_collection = db["users"]

    print("Fetching users...")
    users = []

    async for user in users_collection.find({}):
        # Extract relevant fields
        user_data = {
            "name": f"{user.get('firstName', '')} {user.get('lastName', '')}".strip() or user.get('name', 'N/A'),
            "email": user.get("email", "N/A"),
            "registered_at": user.get("createdAt", user.get("created_at", "N/A"))
        }

        # Format date if it's a datetime object
        if isinstance(user_data["registered_at"], datetime):
            user_data["registered_at"] = user_data["registered_at"].strftime("%Y-%m-%d %H:%M:%S")

        users.append(user_data)

    print(f"Found {len(users)} users")

    # Sort by registration date (newest first)
    users.sort(key=lambda x: x["registered_at"] if x["registered_at"] != "N/A" else "", reverse=True)

    # Save to CSV
    output_file = f"users_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['name', 'email', 'registered_at']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        writer.writeheader()
        for user in users:
            writer.writerow(user)

    print(f"\n✅ Users exported to: {output_file}")
    print(f"Total users: {len(users)}")

    # Print summary
    print("\n📊 Summary:")
    print(f"{'Name':<30} {'Email':<35} {'Registered':<20}")
    print("-" * 85)
    for user in users[:10]:  # Show first 10
        print(f"{user['name']:<30} {user['email']:<35} {user['registered_at']:<20}")

    if len(users) > 10:
        print(f"\n... and {len(users) - 10} more users")

    client.close()
    return output_file


if __name__ == "__main__":
    output = asyncio.run(extract_users())
    print(f"\n✅ Done! Check file: {output}")
