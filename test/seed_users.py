import asyncio
import logging
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
logger = logging.getLogger(__name__)

from config import get_settings

settings = get_settings()

USERS = [
    {
        "email": "vinitha@by8labs.com",
        "name": "Vinitha",
        "role": "ai_consultant",
    },
    {
        "email": "janani@by8labs.com",
        "name": "Janani",
        "role": "full_stack_developer",
    },
]


async def seed_users():
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    users_collection = db.users

    for user_data in USERS:
        email = user_data["email"]
        existing = await users_collection.find_one({"email": email})
        if existing:
            logger.info(f"User {email} already exists, skipping")
            continue

        doc = {
            "email": email,
            "name": user_data["name"],
            "role": user_data["role"],
            "avatar": None,
            "email_verified": True,
            "verification_token": None,
            "verification_token_expires": None,
            "settings": {"email_notifications": True, "weekly_digest": False},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "last_login": None,
            "verification_attempts": 0,
            "last_otp_request": None,
        }

        result = await users_collection.insert_one(doc)
        logger.info(f"Created user: {email} ({user_data['name']}) — role: {user_data['role']}")
        logger.info(f"User ID: {result.inserted_id}")

    client.close()
    logger.info("Done. Users can sign in via OTP — email is pre-verified.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(seed_users())
