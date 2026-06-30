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
    
    collections = await db.list_collection_names()
    print("Searching for occurrences of 'kangasiva15@gmail.com'...")
    
    for coll_name in collections:
        coll = db[coll_name]
        # We search for any document containing the email string anywhere in its fields
        cursor = coll.find()
        found_count = 0
        async for doc in cursor:
            doc_str = str(doc)
            if "kangasiva15@gmail.com" in doc_str:
                found_count += 1
                print(f"[{coll_name}] Found in document ID: {doc.get('_id')}")
        
        if found_count > 0:
            print(f"Total found in '{coll_name}': {found_count}\n")
            
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
