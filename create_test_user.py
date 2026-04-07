"""
Script to create a test user in MongoDB.

Usage:
  Option A (script - requires pymongo):
    pip install pymongo
    python create_test_user.py

  Option B (curl - requires backend running on port 9003):
    curl -X POST http://localhost:9003/auth/signup \
      -H "Content-Type: application/json" \
      -d '{"email": "test@urisocial.com", "password": "Test1234!", "first_name": "Test", "last_name": "User"}'

Test credentials:
  Email:    test@urisocial.com
  Password: Test1234!
"""

import os
from pymongo import MongoClient
from passlib.context import CryptContext

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/uri_db")
MONGODB_DB = os.getenv("MONGODB_DB", "uri_db")

TEST_USER = {
    "email": "test@urisocial.com",
    "password": "Test1234!",
    "first_name": "Test",
    "last_name": "User",
}

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def main():
    client = MongoClient(MONGODB_URI)
    db = client[MONGODB_DB]
    users = db["users"]

    existing = users.find_one({"email": TEST_USER["email"]})
    if existing:
        print(f"User {TEST_USER['email']} already exists.")
        return

    hashed_password = pwd_context.hash(TEST_USER["password"])
    result = users.insert_one(
        {
            "email": TEST_USER["email"],
            "password": hashed_password,
            "first_name": TEST_USER["first_name"],
            "last_name": TEST_USER["last_name"],
        }
    )
    print(f"Created test user: {TEST_USER['email']} (id: {result.inserted_id})")
    print(f"Password: {TEST_USER['password']}")


if __name__ == "__main__":
    main()
