import asyncio
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_settings

async def main():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    
    email = "sivapal@by8labs.com"
    welcome_token = "test_welcome_token_123"
    welcome_token_expires = datetime.now(timezone.utc) + timedelta(days=7)
    
    result = await db.users.update_one(
        {"email": email},
        {"$set": {
            "welcome_token": welcome_token,
            "welcome_token_expires": welcome_token_expires,
            "email_verified": True
        }}
    )
    if result.modified_count > 0 or result.matched_count > 0:
        print("Successfully reset welcome token for sivapal@by8labs.com")
    else:
        print("User not found or not updated")
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
