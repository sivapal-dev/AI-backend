"""
Migration: Add 'position' field to all existing users.
Sets position to empty string for users that don't have it.
Run once: python -m migrations.add_position_field
"""

import asyncio
from database import get_database
import logging

logger = logging.getLogger(__name__)

async def migrate():
    db = get_database()
    result = await db.users.update_many(
        {"position": {"$exists": False}},
        {"$set": {"position": ""}}
    )
    logger.info(f"Migration complete. Modified {result.modified_count} users.")

if __name__ == "__main__":
    asyncio.run(migrate())
