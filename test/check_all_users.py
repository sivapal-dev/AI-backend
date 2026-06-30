"""
Check all users in the database directly via backend.
"""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import get_settings

settings = get_settings()

async def check_users():
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    users_collection = db.users

    print("=== ALL USERS IN DATABASE ===\n")
    count = 0
    async for user in users_collection.find().sort("created_at", -1):
        count += 1
        print(f"Name:     {user.get('name')}")
        print(f"Email:    {user.get('email')}")
        print(f"Role:     {user.get('role')}")
        print(f"Position: {user.get('position')}")
        print(f"Verified: {user.get('email_verified')}")
        print(f"ID:       {user.get('_id')}")
        print()
    print(f"Total users: {count}")

    client.close()

if __name__ == "__main__":
    asyncio.run(check_users())
