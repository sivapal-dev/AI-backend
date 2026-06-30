"""
Seed script to populate holidays for 2026.
Run: python db_seed_holidays.py
"""
import asyncio
from datetime import date
from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings

HOLIDAYS_2026 = [
    {"name": "New Year's Day", "date": "2026-01-01", "day": "Thursday"},
    {"name": "Pongal", "date": "2026-01-15", "day": "Thursday"},
    {"name": "Thiruvalluvar Day", "date": "2026-01-16", "day": "Friday"},
    {"name": "Uzhavar Thirunal", "date": "2026-01-17", "day": "Saturday"},
    {"name": "Republic Day", "date": "2026-01-26", "day": "Monday"},
    {"name": "Thai Poosam", "date": "2026-02-01", "day": "Sunday"},
    {"name": "Telugu New Year's Day", "date": "2026-03-19", "day": "Thursday"},
    {"name": "Ramzan (Id ul Fitr)", "date": "2026-03-21", "day": "Saturday"},
    {"name": "Mahaveer Jayanthi", "date": "2026-03-31", "day": "Tuesday"},
    {"name": "Annual Closing of Accounts (for Banks only)", "date": "2026-04-01", "day": "Wednesday"},
    {"name": "Good Friday", "date": "2026-04-03", "day": "Friday"},
    {"name": "Tamil New Year / Dr. B. R. Ambedkar's Birthday", "date": "2026-04-14", "day": "Tuesday"},
    {"name": "May Day", "date": "2026-05-01", "day": "Friday"},
    {"name": "Bakrid (Id ul Azha)", "date": "2026-05-28", "day": "Thursday"},
    {"name": "Muharram (Yam-e-Shahadat)", "date": "2026-06-26", "day": "Friday"},
    {"name": "Independence Day", "date": "2026-08-15", "day": "Saturday"},
    {"name": "Milad-un-Nabi (Prejihath's Birthday)", "date": "2026-08-26", "day": "Wednesday"},
    {"name": "Krishna Jayanthi", "date": "2026-09-04", "day": "Friday"},
    {"name": "Vinayakar Chaturthi", "date": "2026-09-14", "day": "Monday"},
    {"name": "Gandhi Jayanthi", "date": "2026-10-02", "day": "Friday"},
    {"name": "Ayudha Pooja", "date": "2026-10-19", "day": "Monday"},
    {"name": "Vijaya Dasami", "date": "2026-10-20", "day": "Tuesday"},
    {"name": "Deepavali", "date": "2026-11-08", "day": "Sunday"},
    {"name": "Christmas", "date": "2026-12-25", "day": "Friday"},
]


async def main():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]

    inserted = 0
    for h in HOLIDAYS_2026:
        # Check if already exists
        existing = await db["holidays"].find_one({
            "name": h["name"],
            "year": 2026
        })
        if existing:
            print(f"  Skipping '{h['name']}' — already exists")
            continue
        
        doc = {
            "name": h["name"],
            "date": h["date"],
            "day": h["day"],
            "year": 2026,
            "created_at": date.today().isoformat(),
            "updated_at": date.today().isoformat(),
        }
        await db["holidays"].insert_one(doc)
        inserted += 1
        print(f"  Added: {h['name']} ({h['date']})")

    print(f"\nDone. {inserted} holidays inserted.")


if __name__ == "__main__":
    asyncio.run(main())
