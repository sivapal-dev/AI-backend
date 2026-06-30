"""Check task response for reporter_name."""
import asyncio, httpx

async def test():
    async with httpx.AsyncClient() as client:
        # Login as admin
        r = await client.post("http://localhost:8000/api/auth/login", json={
            "email": "admin@by8flow.com", "password": "adminpass123"
        })
        print("Login:", r.status_code)
        token = r.json()["access_token"]
        
        # Get project tasks
        # First get projects
        r2 = await client.get("http://localhost:8000/api/projects", headers={"Authorization": f"Bearer {token}"})
        print("Projects:", r2.status_code)
        projects = r2.json()
        if projects:
            proj_id = projects[0]["_id"]
            print(f"Project ID: {proj_id}")
            r3 = await client.get(f"http://localhost:8000/api/projects/{proj_id}/tasks", headers={"Authorization": f"Bearer {token}"})
            print("Tasks:", r3.status_code)
            tasks = r3.json()
            if tasks:
                t = tasks[0]
                print(f"Task title: {t.get('title')}")
                print(f"Reporter: {t.get('reporter')}")
                print(f"Reporter_name: {t.get('reporter_name')}")
            else:
                print("No tasks")
        else:
            print("No projects")

if __name__ == "__main__":
    asyncio.run(test())
