import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_settings

async def main():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    user = await db.users.find_one({"email": "kangasiva15@gmail.com"})
    
    if user:
        import json
        print(json.dumps(user, default=str, indent=2))
    else:
        print("User not found")
        
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
