"""
Quick verification: list test employees in DB.
"""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import get_settings

settings = get_settings()

TEST_EMAILS = ["hr@by8labs.com", "sivapal@by8labs.com", "santhosh@by8labs.com"]

async def verify():
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    users_collection = db.users

    print("=== Test Employees ===\n")
    for email in TEST_EMAILS:
        user = await users_collection.find_one({"email": email})
        if user:
            print(f"Name:    {user.get('name')}")
            print(f"Email:   {user.get('email')}")
            print(f"Role:    {user.get('role')}")
            print(f"Position: {user.get('position')}")
            print(f"Verified: {user.get('email_verified')}")
            print(f"ID:      {user.get('_id')}")
            print()
        else:
            print(f"[NOT FOUND] {email}\n")

    client.close()

if __name__ == "__main__":
    asyncio.run(verify())
