"""Test get_project_tasks endpoint directly."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
from main import app
from bson import ObjectId

client = TestClient(app)

# Override dependency to return a test user
def get_test_user():
    return {
        "id": "69ea4d6e69a790e18456810a",  # admin
        "email": "by8tech@gmail.com",
        "name": "By8Tech Admin",
        "role": "admin",
        "email_verified": True,
    }

app.dependency_overrides = {}
from dependencies import get_current_active_user
app.dependency_overrides[get_current_active_user] = get_test_user

# Call endpoint within lifespan context to ensure db connection
with TestClient(app) as client:
    # First, list projects to get a valid project ID
    proj_response = client.get("/api/projects")
    print("List Projects Status:", proj_response.status_code)
    if proj_response.is_success:
        projects = proj_response.json()
        if projects:
            project_id = projects[0]["_id"]
            print(f"Using Project ID: {project_id}")
            response = client.get(f"/api/projects/{project_id}/tasks")
            print("Tasks Status:", response.status_code)
            if response.is_success:
                tasks = response.json()
                if tasks:
                    t = tasks[0]
                    print("First task:")
                    print("  title:", t.get("title"))
                    print("  reporter:", t.get("reporter"))
                    print("  reporter_name:", t.get("reporter_name"))
                else:
                    print("No tasks")
            else:
                print("Error getting tasks:", response.text)
        else:
            print("No projects found in database")
    else:
        print("Error listing projects:", proj_response.text)
