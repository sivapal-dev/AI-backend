"""Trigger AI generate-tasks to see debug logs."""
import asyncio, httpx

async def test():
    async with httpx.AsyncClient() as client:
        # Login
        r = await client.post("http://localhost:8000/api/auth/login", json={
            "email": "hr@by8flow.com", "password": "hrpass123"
        })
        token = r.json()["access_token"]
        print("Logged in, token:", token[:20])
        
        # Call generate-tasks
        r2 = await client.post(
            "http://localhost:8000/api/ai/generate-tasks",
            headers={"Authorization": f"Bearer {token}"},
            json={"markdown": "# Todo App\n- User login\n- Dashboard", "project_id": "test123"},
            timeout=60.0
        )
        print("Status:", r2.status_code)
        print("Body:", r2.text[:500])

if __name__ == "__main__":
    asyncio.run(test())
