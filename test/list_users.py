"""Check admin user."""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings
from bson import ObjectId

async def check():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    
    # List all users
    users = await db.users.find().to_list(10)
    for u in users:
        print(f"id={u['_id']} name={u.get('name')} email={u.get('email')} role={u.get('role')}")
    client.close()

if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from config import get_settings
    asyncio.run(check())
