"""Check user document for reporter."""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings
from bson import ObjectId

async def check_user():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    
    # Find the user with the ID from the task
    reporter_id = "69ef67ff4cb136c5d4ffb979"
    user = await db.users.find_one({"_id": ObjectId(reporter_id)})
    if user:
        print("User found:")
        print("  _id:", user["_id"])
        print("  name:", user.get("name"))
        print("  email:", user.get("email"))
        print("  role:", user.get("role"))
    else:
        print("User not found")
    
    client.close()

if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from config import get_settings
    asyncio.run(check_user())
