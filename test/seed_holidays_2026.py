"""Seed 2026 Indian holiday list into MongoDB."""
import asyncio
from datetime import datetime, timezone
from database import get_database

HOLIDAYS_2026 = [
    ("New Year's Day", "2026-01-01", "Thursday"),
    ("Pongal", "2026-01-15", "Thursday"),
    ("Thiruvalluvar Day", "2026-01-16", "Friday"),
    ("Uzhavar Thirunal", "2026-01-17", "Saturday"),
    ("Republic Day", "2026-01-26", "Monday"),
    ("Thai Poosam", "2026-02-01", "Sunday"),
    ("Telugu New Year's Day", "2026-03-19", "Thursday"),
    ("Ramzan (Id ul Fitr)", "2026-03-21", "Saturday"),
    ("Mahaveer Jayanthi", "2026-03-31", "Tuesday"),
    ("Annual Closing of Accounts (for Banks only)", "2026-04-01", "Wednesday"),
    ("Good Friday", "2026-04-03", "Friday"),
    ("Tamil New Year / Dr. B. R. Ambedkar's Birthday", "2026-04-14", "Tuesday"),
    ("May Day", "2026-05-01", "Friday"),
    ("Bakrid (Id ul Azha)", "2026-05-28", "Thursday"),
    ("Muharram (Yam-e-Shahadat)", "2026-06-26", "Friday"),
    ("Independence Day", "2026-08-15", "Saturday"),
    ("Milad-un-Nabi (Prejihath's Birthday)", "2026-08-26", "Wednesday"),
    ("Krishna Jayanthi", "2026-09-04", "Friday"),
    ("Vinayakar Chaturthi", "2026-09-14", "Monday"),
    ("Gandhi Jayanthi", "2026-10-02", "Friday"),
    ("Ayudha Pooja", "2026-10-19", "Monday"),
    ("Vijaya Dasami", "2026-10-20", "Tuesday"),
    ("Deepavali", "2026-11-08", "Sunday"),
    ("Christmas", "2026-12-25", "Friday"),
]

YEAR = 2026


async def main():
    db = get_database()
    now = datetime.now(timezone.utc)
    inserted = 0
    skipped = 0

    for name, date_str, day in HOLIDAYS_2026:
        existing = await db["holidays"].find_one({"name": name, "year": YEAR})
        if existing:
            print(f"SKIP  {name} — already exists")
            skipped += 1
            continue

        await db["holidays"].insert_one({
            "name": name,
            "date": date_str,
            "day": day,
            "year": YEAR,
            "description": "",
            "created_at": now,
            "updated_at": now,
        })
        print(f"OK    {name} — {date_str} ({day})")
        inserted += 1

    print(f"\nDone. Inserted={inserted}, Skipped={skipped}")


if __name__ == "__main__":
    asyncio.run(main())
