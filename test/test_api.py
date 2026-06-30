import requests

def test():
    # Login
    res = requests.post("http://localhost:8000/api/auth/login", data={"username": "mkdjspython12@gmail.com", "password": "ChangePassword@12"})
    token = res.json().get("access_token")
    if not token:
        print("Login failed")
        return
    
    headers = {"Authorization": f"Bearer {token}"}
    
    # Get projects
    res = requests.get("http://localhost:8000/api/projects", headers=headers)
    projects = res.json()
        
    for p in projects:
        print(f"Project: {p['name']}")
        res = requests.get(f"http://localhost:8000/api/projects/{p['_id']}/tasks", headers=headers)
        tasks = res.json()
        for t in tasks:
            print(f"  Task: {t['title']} | Reporter Name: {t.get('reporter_name')}")

if __name__ == "__main__":
    test()
