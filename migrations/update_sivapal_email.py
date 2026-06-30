import asyncio
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_settings

async def main():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    
    old_email = "kangasiva15@gmail.com"
    new_email = "sivapal@by8labs.com"
    
    print(f"Finding user record for {old_email}...")
    user = await db.users.find_one({"email": old_email})
    
    if not user:
        print(f"Error: User with email {old_email} not found.")
        client.close()
        return
        
    print(f"User found: {user.get('name')} (ID: {user.get('_id')})")
    
    # Generate new welcome token valid for 7 days
    welcome_token = "test_welcome_token_123"
    welcome_token_expires = datetime.now(timezone.utc) + timedelta(days=7)
    
    print(f"Updating email to {new_email} and renewing welcome token...")
    result = await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "email": new_email,
            "welcome_token": welcome_token,
            "welcome_token_expires": welcome_token_expires,
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    if result.modified_count > 0 or result.matched_count > 0:
        print("Successfully updated user email and welcome token in the database!")
        
        # Verify the change
        updated_user = await db.users.find_one({"_id": user["_id"]})
        print("\nUpdated record details:")
        print(f"  Name: {updated_user.get('name')}")
        print(f"  Email: {updated_user.get('email')}")
        print(f"  Welcome Token: {updated_user.get('welcome_token')}")
        print(f"  Welcome Token Expires: {updated_user.get('welcome_token_expires')}")
    else:
        print("No changes made to the database record.")
        
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
