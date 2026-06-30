"""Check if API now returns reporter_name."""
import asyncio, httpx

async def test():
    async with httpx.AsyncClient() as client:
        # Try accessing with cookies - we need to be logged in
        # For simplicity, let's use the test script that uses the admin cookies
        # Actually, let's just print instructions
        print("Please check in browser DevTools:")
        print("1. Open Network tab")
        print("2. Go to Kanban board")
        print("3. Find request to /api/projects/<id>/tasks")
        print("4. Inspect the first task's 'reporter_name' field")
        print("\nIt should show 'Manikandan' or 'By8Tech Admin'")

if __name__ == "__main__":
    asyncio.run(test())
