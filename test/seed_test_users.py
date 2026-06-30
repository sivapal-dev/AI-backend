"""
Seed script to add test employees for development/testing.
Run: python -m backend.seed_test_users
"""
import asyncio
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import get_settings

settings = get_settings()

TEST_USERS = [
    {
        "email": "hr@by8labs.com",
        "name": "Nancy S",
        "role": "hr",
        "position": "hr",
        "email_verified": True,
    },
    {
        "email": "sivapal@by8labs.com",
        "name": "Sivapal P",
        "role": "junior_fullstack_developer",
        "position": "junior_fullstack_developer",
        "email_verified": True,
    },
    {
        "email": "santhosh@by8labs.com",
        "name": "Santhosh",
        "role": "junior_fullstack_developer",
        "position": "junior_fullstack_developer",
        "email_verified": True,
    },
]

async def seed_test_users():
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    users_collection = db.users

    created = 0
    for user in TEST_USERS:
        existing = await users_collection.find_one({"email": user["email"]})
        if existing:
            print(f"[SKIP] User {user['email']} already exists (skipping)")
            continue

        user_doc = {
            "email": user["email"],
            "name": user["name"],
            "role": user["role"],
            "position": user["position"],
            "avatar": None,
            "email_verified": user["email_verified"],
            "verification_token": None,
            "verification_token_expires": None,
            "settings": {"email_notifications": True, "weekly_digest": False},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "last_login": None,
            "verification_attempts": 0,
            "last_otp_request": None,
        }

        result = await users_collection.insert_one(user_doc)
        print(f"[OK] Created user: {user['email']} (name: {user['name']}, role: {user['role']}, position: {user['position']})")
        created += 1

    client.close()
    print(f"\nDone. {created} new user(s) created.")

if __name__ == "__main__":
    asyncio.run(seed_test_users())
