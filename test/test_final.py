"""Test AI generation with JSON mode fix."""
import asyncio, httpx, json

async def test():
    async with httpx.AsyncClient() as client:
        # Login
        r = await client.post("http://localhost:8000/api/auth/login", json={
            "email": "hr@by8flow.com", "password": "hrpass123"
        })
        print("Login:", r.status_code)
        token = r.json()["access_token"]
        
        # Generate tasks
        r2 = await client.post(
            "http://localhost:8000/api/ai/generate-tasks",
            headers={"Authorization": f"Bearer {token}"},
            json={"markdown": "# Test Project\n## Features\n- User login\n- Dashboard view", "project_id": "test123"},
            timeout=60.0
        )
        print("AI status:", r2.status_code)
        data = r2.json()
        print("Success:", data.get("success"))
        print("Count:", data.get("count"))
        if "tasks" in data:
            print("First task:", json.dumps(data["tasks"][0], indent=2)[:300])
        else:
            print("No tasks key")

if __name__ == "__main__":
    asyncio.run(test())
