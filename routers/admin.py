from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from bson import ObjectId
from datetime import datetime, timedelta, timezone
from models.user import UserCreate, UserResponse, UserRole
from models.task import TaskCreate
from dependencies import get_current_active_user, rate_limiter, validate_object_id, require_admin, require_admin_or_hr
from services.auth_service import auth_service
from services.email_service import email_service
from config import get_settings
import secrets

router = APIRouter(prefix="/admin", tags=["Admin"])
settings = get_settings()


def _get_db():
    from database import get_database
    return get_database()


@router.get("/users")
async def list_users(
    skip: int = 0,
    limit: int = 50,
    admin_user: dict = Depends(require_admin_or_hr),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    cursor = db.users.find().skip(skip).limit(limit).sort("created_at", -1)
    users = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        doc.pop("password", None)
        doc.pop("verification_token", None)
        doc.pop("verification_token_expires", None)
        users.append(doc)
    return users


@router.get("/users/{user_id}", response_model=dict)
async def get_user(
    user_id: str,
    admin_user: dict = Depends(require_admin_or_hr),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user ID")
    user = await db.users.find_one({"_id": oid})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user["_id"] = str(user["_id"])
    user.pop("password", None)
    user.pop("verification_token", None)
    user.pop("verification_token_expires", None)
    return user


@router.post("/users", response_model=dict)
async def create_user(
    user_data: UserCreate,
    admin_user: dict = Depends(require_admin_or_hr),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    import re
    existing = await db.users.find_one(
        {"email": {"$regex": f"^{re.escape(user_data.email)}$", "$options": "i"}}
    )
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    existing_name = await db.users.find_one(
        {"name": {"$regex": f"^{re.escape(user_data.name)}$", "$options": "i"}}
    )
    if existing_name:
        raise HTTPException(status_code=400, detail="Username already taken")

    user_doc = {
        "email": user_data.email,
        "name": user_data.name,
        "role": user_data.role.value,
        "position": user_data.position,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "is_active": True,
        "email_verified": False,
        "welcome_token": secrets.token_urlsafe(32),
        "welcome_token_expires": datetime.now(timezone.utc) + timedelta(days=7),
        "settings": {
            "email_notifications": True,
            "weekly_digest": False,
            "notifications": {
                "task_assigned": True,
                "task_updated": True,
                "task_commented": True,
                "bug_assigned": True,
                "bug_updated": True,
                "meeting_scheduled": True,
                "meeting_reminder": True,
                "project_invite": True,
                "system": True,
            },
            "ams_enabled": True
        },
        "leave_balance": {
            "annual_total": 18,
            "annual_used": 0,
            "annual_pending": 0,
            "emergency_total": 10,
            "emergency_used": 0,
            "emergency_pending": 0
        }
    }
    result = await db.users.insert_one(user_doc)
    
    # Send welcome email with verification link
    verify_url = f"{settings.frontend_url}/otp-verify?email={user_data.email}&welcome_token={user_doc['welcome_token']}"
    await email_service.send_welcome_email(
        to_email=user_data.email,
        name=user_data.name,
        verify_url=verify_url
    )
    
    return {"id": str(result.inserted_id), "message": "User created successfully"}


@router.put("/users/{user_id}", response_model=dict)
async def update_user(
    user_id: str,
    user_data: dict,
    admin_user: dict = Depends(require_admin_or_hr),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user ID")
    existing = await db.users.find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    allowed = {"name", "email", "role", "position", "is_active"}
    update = {k: v for k, v in user_data.items() if k in allowed and v is not None}
    if not update:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    update["updated_at"] = datetime.now(timezone.utc)
    await db.users.update_one({"_id": oid}, {"$set": update})
    return {"message": "User updated successfully"}


@router.delete("/users/{user_id}", response_model=dict)
async def delete_user(
    user_id: str,
    admin_user: dict = Depends(require_admin_or_hr),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user ID")
    existing = await db.users.find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    if str(existing["_id"]) == admin_user["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
        
    # Hard delete user
    await db.users.delete_one({"_id": oid})
    
    # Cascade clean up references (tasks assigned, bugs assigned, leaves, daily checkins, etc)
    await db.tasks.update_many({"assignee_id": user_id}, {"$set": {"assignee_id": None, "assignee": None}})
    await db.bugs.update_many({"assignee_id": user_id}, {"$set": {"assignee_id": None, "assignee": None}})
    await db.leaves.delete_many({"user_id": {"$in": [ObjectId(user_id), user_id]}})
    await db.daily_checkins.delete_many({"user_id": user_id})
    
    # Clean up activity logs, notifications, comments, and project teams
    await db.activity_logs.delete_many({"user_id": user_id})
    await db.notifications.delete_many({"user_id": user_id})
    await db.comments.delete_many({"author_id": user_id})
    await db.projects.update_many(
        {},
        {"$pull": {"team": {"$in": [ObjectId(user_id), user_id]}}}
    )
    
    return {"message": "User deleted successfully"}


@router.post("/users/{user_id}/reset-password", response_model=dict)
async def reset_password(
    user_id: str,
    admin_user: dict = Depends(require_admin_or_hr),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user ID")
    existing = await db.users.find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    welcome_token = secrets.token_urlsafe(32)
    welcome_token_expires = datetime.now(timezone.utc) + timedelta(days=7)
    await db.users.update_one(
        {"_id": oid},
        {
            "$set": {
                "welcome_token": welcome_token,
                "welcome_token_expires": welcome_token_expires,
                "email_verified": False,
                "updated_at": datetime.now(timezone.utc),
            }
        }
    )

    verify_url = f"{settings.frontend_url}/otp-verify?email={existing['email']}&welcome_token={welcome_token}"
    await email_service.send_welcome_email(
        to_email=existing["email"],
        name=existing.get("name", "User"),
        verify_url=verify_url
    )

    return {"message": "Welcome access reset email sent successfully"}


@router.get("/stats", response_model=dict)
async def get_stats(
    admin_user: dict = Depends(require_admin),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    total_users = await db.users.count_documents({})
    total_projects = await db.projects.count_documents({})
    total_tasks = await db.tasks.count_documents({})
    total_bugs = await db.bugs.count_documents({})
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    new_tasks_week = await db.tasks.count_documents({"created_at": {"$gte": week_ago}})
    active_projects = await db.projects.count_documents({"status": "active"})
    recent_users = await db.users.count_documents({"created_at": {"$gte": month_ago}})
    return {
        "total_users": total_users,
        "total_projects": total_projects,
        "total_tasks": total_tasks,
        "total_bugs": total_bugs,
        "new_tasks_this_week": new_tasks_week,
        "active_projects": active_projects,
        "recent_users_last_30_days": recent_users,
    }


@router.get("/employee-holiday-selections", response_model=List[dict])
async def get_all_employee_holiday_selections(
    year: Optional[int] = None,
    admin_user: dict = Depends(require_admin),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    target_year = year or datetime.now(timezone.utc).year
    year_key = str(target_year)

    cursor = db.users.find(
        {"emergency_holiday_selections": {"$exists": True}},
        {
            "name": 1,
            "email": 1,
            "role": 1,
            "emergency_holiday_selections": 1,
        },
    ).sort("name", 1)

    selections = []
    async for user in cursor:
        holiday_names = user.get("emergency_holiday_selections", {}).get(year_key, [])
        selections.append(
            {
                "user_id": str(user["_id"]),
                "name": user.get("name", "Unknown"),
                "email": user.get("email", ""),
                "role": user.get("role", ""),
                "year": target_year,
                "holiday_names": holiday_names,
                "count": len(holiday_names),
            }
        )

    return selections


@router.post("/tasks", response_model=dict)
async def admin_create_task(
    task_data: TaskCreate,
    admin_user: dict = Depends(require_admin),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    try:
        project_oid = ObjectId(task_data.project_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    project = await db.projects.find_one({"_id": project_oid})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    task_doc = {
        "title": task_data.title,
        "description": task_data.description or "",
        "role": task_data.role.value if hasattr(task_data.role, "value") else task_data.role,
        "priority": task_data.priority.value if hasattr(task_data.priority, "value") else task_data.priority,
        "status": task_data.status.value if hasattr(task_data.status, "value") else task_data.status,
        "complexity": task_data.complexity.value if hasattr(task_data.complexity, "value") else task_data.complexity,
        "tags": task_data.tags or [],
        "estimated_hours": task_data.estimated_hours,
        "story_points": task_data.story_points,
        "time_spent": task_data.time_spent or 0,
        "remaining_hours": task_data.remaining_hours,
        "ai_generated": task_data.ai_generated or False,
        "source_markdown": task_data.source_markdown,
        "order": task_data.order or 0,
        "images": [img.model_dump() for img in task_data.images] if task_data.images else [],
        "project_id": task_data.project_id,
        "assignee": task_data.assignee,
        "assignee_id": task_data.assignee_id,
        "dependencies": task_data.dependencies or [],
        "start_date": task_data.start_date,
        "due_date": task_data.due_date,
        "sprint": task_data.sprint,
        "parent_id": task_data.parent_id,
        "epic_id": task_data.epic_id,
        "custom_field_values": task_data.custom_field_values or {},
        "created_by": admin_user["id"],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = await db.tasks.insert_one(task_doc)
    return {"id": str(result.inserted_id), "message": "Task created successfully"}


# change-role endpoint removed; role can be updated directly via PUT /admin/users/{user_id}


@router.get("/bugs", response_model=List[dict])
async def list_admin_bugs(
    skip: int = 0,
    limit: int = 100,
    admin_user: dict = Depends(require_admin),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    cursor = db.bugs.find().skip(skip).limit(limit).sort("created_at", -1)
    bugs = []
    project_ids = set()
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if "reporter" in doc:
            doc["reporter"] = str(doc["reporter"])
        if doc.get("project_id"):
            project_ids.add(doc["project_id"])
        bugs.append(doc)

    # Batch fetch project names
    project_name_map = {}
    if project_ids:
        valid_oids = []
        for pid in project_ids:
            try:
                valid_oids.append(ObjectId(pid))
            except Exception:
                pass
        if valid_oids:
            async for p in db.projects.find({"_id": {"$in": valid_oids}}, {"name": 1}):
                project_name_map[str(p["_id"])] = p.get("name", "Unknown")

    for doc in bugs:
        doc["project_name"] = project_name_map.get(doc.get("project_id", ""), "Unknown")

    return bugs


@router.get("/activity-logs", response_model=List[dict])
async def get_activity_logs(
    skip: int = 0,
    limit: int = 50,
    admin_user: dict = Depends(require_admin),
    _rate_limit=Depends(rate_limiter(max_requests=30, window_seconds=60)),
):
    db = _get_db()
    cursor = db.activity_logs.find().skip(skip).limit(limit).sort("created_at", -1)
    logs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        user_id_obj = doc.get("user_id")
        doc["user_email"] = ""
        if user_id_obj:
            try:
                user_doc = await db.users.find_one(
                    {"_id": ObjectId(user_id_obj)},
                    {"email": 1}
                )
                if user_doc:
                    doc["user_email"] = user_doc.get("email", "")
            except Exception:
                doc["user_email"] = ""
        if "details" not in doc:
            metadata = doc.get("metadata", {})
            title = metadata.get("title", "")
            action = doc.get("action", "")
            entity = doc.get("entity_type", "")
            action_label = action.replace("_", " ").title()
            if title:
                doc["details"] = f"{action_label} {entity}: {title}"
            else:
                doc["details"] = f"{action_label} {entity}"
        logs.append(doc)
    return logs


@router.get("/tasks", response_model=List[dict])
async def admin_list_tasks(
    admin_user: dict = Depends(require_admin),
):
    db = _get_db()
    cursor = db.tasks.find({}).sort("created_at", -1)
    tasks = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if "reporter" in doc:
            doc["reporter"] = str(doc["reporter"])
        doc["subtask_count"] = len(doc.get("subtask_ids", []))
        tasks.append(doc)
    return tasks


@router.get("/tasks/{task_id}", response_model=dict)
async def admin_get_task(
    task_id: str,
    admin_user: dict = Depends(require_admin),
):
    db = _get_db()
    try:
        task_oid = ObjectId(task_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task ID")
    task = await db.tasks.find_one({"_id": task_oid})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task["_id"] = str(task["_id"])
    if "reporter" in task:
        task["reporter"] = str(task["reporter"])
    task["subtask_count"] = len(task.get("subtask_ids", []))
    return task


@router.put("/tasks/{task_id}", response_model=dict)
async def admin_update_task(
    task_id: str,
    task_data: dict,
    admin_user: dict = Depends(require_admin),
):
    db = _get_db()
    try:
        task_oid = ObjectId(task_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task ID")
    
    # Remove immutable fields if present
    task_data.pop("_id", None)
    task_data.pop("id", None)
    task_data["updated_at"] = datetime.now(timezone.utc)
    
    result = await db.tasks.update_one(
        {"_id": task_oid},
        {"$set": task_data}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    
    updated_task = await db.tasks.find_one({"_id": task_oid})
    updated_task["_id"] = str(updated_task["_id"])
    if "reporter" in updated_task:
        updated_task["reporter"] = str(updated_task["reporter"])
    return updated_task


@router.delete("/tasks/{task_id}", response_model=dict)
async def admin_delete_task(
    task_id: str,
    admin_user: dict = Depends(require_admin),
):
    db = _get_db()
    try:
        task_oid = ObjectId(task_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task ID")
    
    result = await db.tasks.delete_one({"_id": task_oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"message": "Task deleted successfully"}


@router.get("/projects", response_model=List[dict])
async def admin_list_projects(
    admin_user: dict = Depends(require_admin),
):
    db = _get_db()
    cursor = db.projects.find().sort("created_at", -1)
    projects = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        projects.append(doc)
    return projects


@router.put("/projects/{project_id}", response_model=dict)
async def admin_update_project(
    project_id: str,
    project_data: dict,
    admin_user: dict = Depends(require_admin),
):
    db = _get_db()
    try:
        project_oid = ObjectId(project_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid project ID")

    project_data.pop("_id", None)
    project_data.pop("id", None)
    project_data["updated_at"] = datetime.now(timezone.utc)

    result = await db.projects.update_one(
        {"_id": project_oid},
        {"$set": project_data}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Project not found")

    updated = await db.projects.find_one({"_id": project_oid})
    updated["_id"] = str(updated["_id"])
    return updated


@router.delete("/projects/{project_id}", response_model=dict)
async def admin_delete_project(
    project_id: str,
    admin_user: dict = Depends(require_admin),
):
    from helpers.backblaze import delete_from_drive
    db = _get_db()
    try:
        project_oid = ObjectId(project_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid project ID")

    existing = await db.projects.find_one({"_id": project_oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")

    # Perform a hard delete of the project and all its associated data
    tasks = await db.tasks.find({"project_id": project_id}, {"_id": 1, "attachments": 1}).to_list(length=None)
    bugs = await db.bugs.find({"project_id": project_id}, {"_id": 1, "attachments": 1}).to_list(length=None)

    drive_file_ids = set()

    for attachment in existing.get("attachments") or []:
        file_id = attachment.get("file_id")
        if file_id:
            drive_file_ids.add(file_id)

    for task in tasks:
        for attachment in task.get("attachments") or []:
            drive_file_id = attachment.get("drive_file_id")
            if drive_file_id:
                drive_file_ids.add(drive_file_id)
    for bug in bugs:
        for attachment in bug.get("attachments") or []:
            drive_file_id = attachment.get("drive_file_id")
            if drive_file_id:
                drive_file_ids.add(drive_file_id)

    for drive_file_id in drive_file_ids:
        try:
            delete_from_drive(drive_file_id)
        except Exception as exc:
            logger.warning(f"Failed to delete Drive file {drive_file_id}: {exc}")

    await db.projects.delete_one({"_id": project_oid})
    await db.tasks.delete_many({"project_id": project_id})
    await db.bugs.delete_many({"project_id": project_id})
    await db.documents.delete_many({"project_id": project_id})
    await db.sprints.delete_many({"project_id": project_id})
    await db.ams_tickets.delete_many({"project_id": project_id})
    await db.whiteboards.delete_many({"project_id": project_id})

    return {"message": "Project and all associated data permanently deleted"}

