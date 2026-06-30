"""Check tasks for the Todo App project."""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings
from bson import ObjectId

async def check():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    
    # Find the project
    proj = await db.projects.find_one({"name": "Todo App"})
    if proj:
        print(f"Project: {proj['name']}")
        print(f"  created_by: {proj.get('created_by')}")
        print(f"  created_by_name: {proj.get('created_by_name', 'MISSING')}")
        
        # Find tasks for this project
        tasks = await db.tasks.find({"project_id": str(proj["_id"])}).to_list(10)
        print(f"\nTasks ({len(tasks)}):")
        for t in tasks:
            print(f"  title: {t.get('title')}")
            print(f"    reporter: {t.get('reporter')}")
            print(f"    reporter_name: {t.get('reporter_name', 'MISSING')}")
    else:
        print("Project not found")
    client.close()

if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from config import get_settings
    asyncio.run(check())
