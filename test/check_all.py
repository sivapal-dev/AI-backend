import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from bson import ObjectId

load_dotenv()

async def check_all_tasks():
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client.by8flow

    cursor = db.tasks.find()
    unmapped = []
    mapped = []
    
    async for doc in cursor:
        rid = doc.get("reporter")
        if not rid:
            unmapped.append((str(doc.get("_id")), "NO_REPORTER_FIELD"))
            continue
            
        rid_str = str(rid)
        try:
            oid = ObjectId(rid_str)
            user = await db.users.find_one({"_id": oid})
            if user:
                mapped.append((str(doc.get("_id")), rid_str, user.get("name")))
            else:
                unmapped.append((str(doc.get("_id")), rid_str, "USER_NOT_FOUND"))
        except Exception as e:
            unmapped.append((str(doc.get("_id")), rid_str, f"INVALID_OBJECTID: {e}"))

    print(f"Total mapped: {len(mapped)}")
    print(f"Total unmapped: {len(unmapped)}")
    for u in unmapped:
        print(f"Task {u[0]} has unmapped reporter: {u[1]} ({u[2]})")

if __name__ == "__main__":
    asyncio.run(check_all_tasks())
