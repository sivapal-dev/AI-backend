import asyncio
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from passlib.context import CryptContext
import sys
from pathlib import Path
import logging

sys.path.insert(0, str(Path(__file__).parent))
logger = logging.getLogger(__name__)

from config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ADMIN_EMAIL = "by8tech@gmail.com"
ADMIN_NAME = "By8Tech Admin"


async def seed_admin():
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    users_collection = db.users

    existing = await users_collection.find_one({"email": ADMIN_EMAIL})
    if existing:
        logger.info(f"User {ADMIN_EMAIL} already exists")
        if existing.get("role") != "admin":
            await users_collection.update_one(
                {"email": ADMIN_EMAIL},
                {"$set": {"role": "admin", "updated_at": datetime.now(timezone.utc)}},
            )
            logger.info(f"Updated {ADMIN_EMAIL} to admin role")
        else:
            logger.info(f"{ADMIN_EMAIL} is already an admin")
        client.close()
        return

    admin_doc = {
        "email": ADMIN_EMAIL,
        "name": ADMIN_NAME,
        "role": "admin",
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

    result = await users_collection.insert_one(admin_doc)
    logger.info(f"Created admin user: {ADMIN_EMAIL}")
    logger.info(f"User ID: {result.inserted_id}")
    logger.info(
        "\nNote: This user can request OTP to sign in. The admin role is pre-configured."
    )
    client.close()


if __name__ == "__main__":
    asyncio.run(seed_admin())
