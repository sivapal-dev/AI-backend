"""Inspect tasks and reporter fields."""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings
from bson import ObjectId

async def inspect():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    
    # Find a task
    task = await db.tasks.find_one()
    if task:
        print("Task _id:", task["_id"])
        print("Task title:", task.get("title"))
        print("Reporter raw:", task.get("reporter"), type(task.get("reporter")))
        print("Reporter as str:", str(task.get("reporter")) if task.get("reporter") else None)
        
        # Check if reporter exists in users
        reporter_id = task.get("reporter")
        if reporter_id:
            # Try both ObjectId and string
            try:
                user = await db.users.find_one({"_id": ObjectId(reporter_id)})
                if user:
                    print("User found by ObjectId:", user.get("name"))
                else:
                    # Try as string (maybe it's stored as string in user _id?)
                    user2 = await db.users.find_one({"_id": str(reporter_id)})
                    if user2:
                        print("User found by string _id:", user2.get("name"))
                    else:
                        print("User NOT found")
            except Exception as e:
                print("Error looking up user:", e)
    else:
        print("No tasks found")
    
    client.close()

if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from config import get_settings
    asyncio.run(inspect())
