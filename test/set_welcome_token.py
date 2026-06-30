import asyncio
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_settings

settings = get_settings()

async def main():
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    users_collection = db.users

    email = "by8tech@gmail.com"
    welcome_token = "test_welcome_token_123"
    expires = datetime.now(timezone.utc) + timedelta(days=7)

    result = await users_collection.update_one(
        {"email": email},
        {"$set": {
            "welcome_token": welcome_token,
            "welcome_token_expires": expires,
            "email_verified": True
        }}
    )

    if result.modified_count > 0 or result.matched_count > 0:
        print(f"Successfully set/verified welcome_token for {email}")
    else:
        print(f"Failed to set welcome_token (user may not exist)")

    client.close()

if __name__ == "__main__":
    asyncio.run(main())
