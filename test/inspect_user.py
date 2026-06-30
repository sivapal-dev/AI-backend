"""Inspect full user doc."""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings
from bson import ObjectId

async def inspect():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    
    user = await db.users.find_one({"_id": ObjectId("69ef67ff4cb136c5d4ffb979")})
    if user:
        print("User fields:", list(user.keys()))
        print("name:", user.get("name"))
        print("email:", user.get("email"))
    client.close()

if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from config import get_settings
    asyncio.run(inspect())
