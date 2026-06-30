from fastapi import APIRouter, HTTPException, status, Depends
from typing import List, Optional, Dict
from bson import ObjectId
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from database import get_database
from models.task import TaskCreate, TaskUpdate, TaskStatus
from dependencies import get_current_active_user
from helpers.backblaze import delete_from_drive
from helpers.notification_sender import send_notification, notify_role_watchers
from utils.office_hours import calculate_office_elapsed_seconds
import asyncio
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["Tasks"])


def _get_db():
    return get_database()


ALLOWED_TRANSITIONS = {
    "backlog": ["todo"],
    "todo": ["in_progress"],
    "in_progress": ["code_review"],
    "code_review": ["testing"],
    "testing": ["done"],
    "done": ["backlog"],
}

STATUS_CHANGE_ROLES = {
    "todo": ["developer", "admin", "team_lead", "hr"],
    "in_progress": ["developer", "admin", "team_lead", "hr"],
    "code_review": ["developer", "admin", "team_lead", "hr"],
    "testing": ["developer", "qa", "admin", "team_lead", "hr"],
    "done": ["developer", "qa", "admin", "team_lead", "hr"],
}

# Role families: any role containing these keywords maps to the family
ROLE_FAMILIES = {
    "developer": ["developer", "fullstack", "frontend", "backend", "engineer", "full_stack", "full stack"],
    "qa": ["qa", "quality", "tester", "testing"],
    "admin": ["admin"],
    "team_lead": ["team_lead", "lead", "manager"],
    "hr": ["hr", "human_resource", "human resource"],
}


def _get_role_family(user_role: str) -> list[str]:
    """Return all role family names that match the given user role string."""
    role_lower = user_role.lower().replace("-", "_").replace(" ", "_")
    families = []
    for family, keywords in ROLE_FAMILIES.items():
        for kw in keywords:
            kw_norm = kw.lower().replace("-", "_").replace(" ", "_")
            if kw_norm in role_lower:
                families.append(family)
                break
    return families if families else [role_lower]  # fallback to raw role


async def _check_task_access(task: dict, current_user: dict, db) -> None:
    """Helper to check if the current user has access to the task's project."""
    project_id = task.get("project_id")
    if not project_id:
        return
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    user_role = current_user.get("role", "").lower()
    if user_role not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied to this project's tasks")


async def validate_custom_field_values(project_id: str, values: dict, db) -> None:
    if not values:
        return
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    custom_fields = project.get("custom_fields", [])
    fields_map = {f["id"]: f for f in custom_fields}
    
    for fid, val in values.items():
        if fid not in fields_map:
            raise HTTPException(status_code=400, detail=f"Custom field {fid} is not defined in this project")
        
        field_def = fields_map[fid]
        ftype = field_def.get("field_type")
        options = field_def.get("options", [])
        
        if ftype == "text":
            if not isinstance(val, str):
                raise HTTPException(status_code=400, detail=f"Custom field {fid} must be a string")
        elif ftype == "number":
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise HTTPException(status_code=400, detail=f"Custom field {fid} must be a number")
        elif ftype == "checkbox":
            if not isinstance(val, bool):
                raise HTTPException(status_code=400, detail=f"Custom field {fid} must be a boolean")
        elif ftype == "select":
            if not isinstance(val, str):
                raise HTTPException(status_code=400, detail=f"Custom field {fid} must be a string selection")
            if val not in options:
                raise HTTPException(status_code=400, detail=f"Custom field {fid} value '{val}' is not one of the allowed options: {options}")
        elif ftype == "multi_select":
            if not isinstance(val, list):
                raise HTTPException(status_code=400, detail=f"Custom field {fid} must be a list of selections")
            for item in val:
                if not isinstance(item, str):
                    raise HTTPException(status_code=400, detail=f"Custom field {fid} multi-select items must be strings")
                if item not in options:
                    raise HTTPException(status_code=400, detail=f"Custom field {fid} value '{item}' is not one of the allowed options: {options}")
        elif ftype == "date":
            if not isinstance(val, str):
                raise HTTPException(status_code=400, detail=f"Custom field {fid} must be a date string")
            try:
                from datetime import date
                date.fromisoformat(val[:10])
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Custom field {fid} must be a valid ISO date string (YYYY-MM-DD)")
        elif ftype == "url":
            if not isinstance(val, str):
                raise HTTPException(status_code=400, detail=f"Custom field {fid} must be a URL string")
            if not val.startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail=f"Custom field {fid} must be a valid HTTP/HTTPS URL")



ROLLBACK_TRANSITIONS = {
    "todo": ["backlog"],
    "in_progress": ["todo", "backlog"],
    "code_review": ["in_progress", "backlog"],
    "testing": ["code_review", "in_progress", "backlog"],
    "done": ["testing", "code_review", "in_progress", "backlog"],
}

STAGE_ORDER = ["backlog", "todo", "in_progress", "code_review", "testing", "done"]


@router.post("", response_model=dict)
async def create_task(
    task: TaskCreate, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    # Verify project exists and user has access (admin or team member)
    project = await db.projects.find_one({"_id": ObjectId(task.project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin"] and current_user[
        "id"
    ] not in project.get("team", []):
        raise HTTPException(
            status_code=403, detail="You are not a member of this project"
        )

    # Verify parent task and dependencies exist
    if task.parent_id:
        parent_exists = await db.tasks.find_one({"_id": ObjectId(task.parent_id)})
        if not parent_exists:
            raise HTTPException(status_code=404, detail="Parent task not found")
            
    if task.dependencies:
        for dep in task.dependencies:
            dep_exists = await db.tasks.find_one({"_id": ObjectId(dep)})
            if not dep_exists:
                raise HTTPException(status_code=404, detail=f"Dependency task {dep} not found")

    # Validate custom field values
    if task.custom_field_values:
        await validate_custom_field_values(task.project_id, task.custom_field_values, db)

    # Resolve assignee name if assignee_id provided
    assignee_name = None
    assignee_id_val = task.assignee_id
    if assignee_id_val:
        assignee_user = await db.users.find_one({"_id": ObjectId(assignee_id_val)})
        if assignee_user:
            assignee_name = assignee_user.get("name", "Unknown")
        else:
            assignee_name = task.assignee
    else:
        assignee_name = task.assignee
        # If only name provided, resolve to user ID so we can auto-add to project team
        if assignee_name:
            assignee_user_by_name = await db.users.find_one({"name": assignee_name})
            if assignee_user_by_name:
                assignee_id_val = str(assignee_user_by_name["_id"])

    if assignee_id_val:
        team_members = [str(uid) for uid in project.get("team", [])]
        if assignee_id_val not in team_members:
            raise HTTPException(
                status_code=400,
                detail="Assignee is not a member of this project"
            )

    task_doc = {
         "project_id": task.project_id,
         "title": task.title,
         "description": task.description,
         "role": task.role.value,
         "priority": task.priority.value,
         "status": task.status.value,
         "complexity": task.complexity.value,
         "tags": task.tags,
         "estimated_hours": task.estimated_hours,
         "story_points": task.story_points,
         "time_spent": 0,
         "remaining_hours": task.estimated_hours,
         "ai_generated": task.ai_generated,
         "source_markdown": task.source_markdown,
         "order": task.order,
          "assignee": assignee_name,
          "assignee_id": assignee_id_val,
          "reporter": current_user["id"],
          "reporter_name": current_user.get("name", "Unknown"),
          "dependencies": task.dependencies,
         "attachments": [],
         "start_date": task.start_date.isoformat() if task.start_date else None,
         "due_date": task.due_date.isoformat() if task.due_date else None,
         "sprint": task.sprint,
         "parent_id": task.parent_id,
         "epic_id": task.epic_id,
         "subtask_ids": [],
         "custom_field_values": task.custom_field_values or {},
         "images": [img.model_dump() for img in task.images] if task.images else None,
         "started_at": None,
         "paused_at": None,
         "total_paused_ms": 0,
         "is_timer_running": False,
         "created_at": datetime.now(timezone.utc),
         "updated_at": datetime.now(timezone.utc),
    }
    result = await db.tasks.insert_one(task_doc)
    new_task_id = str(result.inserted_id)

    # Notify assignee if task is assigned (unless notifications are batched/skipped)
    if assignee_id_val and not task.skip_notification:
        await send_notification(
            user_id=assignee_id_val,
            type_="task_assigned",
            title="Task Assigned",
            message=f"You have been assigned to task: {task.title}",
            entity_type="task",
            entity_id=new_task_id,
            link=f"/dashboard/projects/{task.project_id}/tasks",
        )



    if task.parent_id:
        await db.tasks.update_one(
            {"_id": ObjectId(task.parent_id)},
            {
                "$push": {"subtask_ids": new_task_id},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "task_created",
            "entity_type": "task",
            "entity_id": str(result.inserted_id),
            "metadata": {"title": task.title, "parent_id": task.parent_id},
            "created_at": datetime.now(timezone.utc),
        }
    )


    return {"id": str(result.inserted_id), "message": "Task created successfully"}


@router.get("")
async def list_tasks(
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    query = {}

    if project_id:
        project = await db.projects.find_one({"_id": ObjectId(project_id)})
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if current_user.get("role", "").lower() != "admin" and current_user[
            "id"
        ] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")
        query["project_id"] = project_id
    else:
        if current_user.get("role", "").lower() != "admin":
            user_projects = await db.projects.find(
                {"team": current_user["id"], "status": {"$ne": "archived"}}, {"_id": 1}
            ).to_list(length=None)
            project_ids = [str(p["_id"]) for p in user_projects]
            if not project_ids:
                return []
            query["project_id"] = {"$in": project_ids}

    query["parent_id"] = None  # Only top-level tasks (null or missing parent_id)

    if status:
        query["status"] = status
    if assignee:
        query["assignee"] = assignee

    cursor = db.tasks.find(query).sort("order", 1)
    tasks = []
    assignee_ids = []
    reporter_ids = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if "reporter" in doc:
            doc["reporter"] = str(doc["reporter"])
        doc["subtask_count"] = len(doc.get("subtask_ids", []))
        # Collect assignee_id and reporter_id for batch enrichment
        if doc.get("assignee_id"):
            assignee_ids.append(doc["assignee_id"])
        if doc.get("reporter"):
            reporter_ids.append(doc["reporter"])
        tasks.append(doc)

    # Batch fetch assignee roles and reporter names in one query
    all_user_ids = list(set(assignee_ids + reporter_ids))
    user_map = {}
    if all_user_ids:
        # Validate ObjectIds to avoid crash on malformed IDs
        valid_object_ids = []
        for uid in all_user_ids:
            try:
                valid_object_ids.append(ObjectId(uid))
            except Exception:
                continue  # skip invalid IDs
        if valid_object_ids:
            users_cursor = db.users.find({"_id": {"$in": valid_object_ids}})
            async for user_doc in users_cursor:
                user_map[str(user_doc["_id"])] = {
                    "role": user_doc.get("role", "developer"),
                    "name": user_doc.get("name", "Unknown"),
                }
    
    for task in tasks:
        aid = task.get("assignee_id")
        task["assignee_role"] = user_map.get(aid, {}).get("role", "developer") if aid else None
        task["assignee"] = user_map.get(aid, {}).get("name", task.get("assignee")) if aid else task.get("assignee")
        rid = task.get("reporter")
        # Always set reporter_name from user_map if available, otherwise use existing or fallback to ID
        if rid and rid in user_map:
            task["reporter_name"] = user_map[rid].get("name", rid)
        elif "reporter_name" not in task or not task["reporter_name"]:
            task["reporter_name"] = rid if rid else "Unknown"

    return tasks


@router.put("/{task_id}")
async def update_task(
    task_id: str,
    task_update: TaskUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    existing = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Task not found")

    await _check_task_access(existing, current_user, db)

    # W133: Circular reference and existence checks
    if task_update.parent_id is not None:
        if task_update.parent_id == task_id:
            raise HTTPException(status_code=400, detail="A task cannot be its own parent")
        if task_update.parent_id:
            parent_exists = await db.tasks.find_one({"_id": ObjectId(task_update.parent_id)})
            if not parent_exists:
                raise HTTPException(status_code=404, detail="Parent task not found")
            # Check circular parent-child reference upwards
            curr_parent_id = task_update.parent_id
            visited = {task_id}
            while curr_parent_id:
                if curr_parent_id in visited:
                    raise HTTPException(status_code=400, detail="Circular parent-child reference detected")
                visited.add(curr_parent_id)
                parent_task = await db.tasks.find_one({"_id": ObjectId(curr_parent_id)}, {"parent_id": 1})
                if not parent_task:
                    break
                curr_parent_id = parent_task.get("parent_id")

    if task_update.dependencies is not None and task_update.dependencies:
        if task_id in task_update.dependencies:
            raise HTTPException(status_code=400, detail="A task cannot depend on itself")
        
        # Verify all dependencies exist
        for dep in task_update.dependencies:
            dep_exists = await db.tasks.find_one({"_id": ObjectId(dep)})
            if not dep_exists:
                raise HTTPException(status_code=404, detail=f"Dependency task {dep} not found")

        # Cycle detection DFS
        async def has_path(start_id: str, target_id: str, visited_set: set) -> bool:
            if start_id == target_id:
                return True
            if start_id in visited_set:
                return False
            visited_set.add(start_id)
            t = await db.tasks.find_one({"_id": ObjectId(start_id)}, {"dependencies": 1})
            if not t:
                return False
            for dep_id in t.get("dependencies", []):
                if await has_path(dep_id, target_id, visited_set):
                    return True
            return False

        for dep in task_update.dependencies:
            if await has_path(dep, task_id, set()):
                raise HTTPException(
                    status_code=400,
                    detail=f"Circular dependency detected: task depends on {dep} which depends back on {task_id}"
                )

    # Check permission: reporter, admin, or team_lead can update
    if existing.get("reporter") != current_user["id"] and current_user.get("role", "").lower() not in ["admin", "team_lead"]:
        raise HTTPException(
            status_code=403, detail="Only task reporter or admin/team_lead can update"
        )

    from enum import Enum
    update_data = {}
    for k, v in task_update.model_dump().items():
        if v is not None:
            if isinstance(v, Enum):
                update_data[k] = v.value
            else:
                update_data[k] = v

    # Check if assignee is being changed
    old_assignee_id = existing.get("assignee_id")
    new_assignee_id = None
    if "assignee_id" in update_data and update_data["assignee_id"]:
        new_assignee_id = update_data["assignee_id"]
        assignee_user = await db.users.find_one({"_id": ObjectId(update_data["assignee_id"])})
        if not assignee_user:
            raise HTTPException(status_code=404, detail="Assignee not found")
        update_data["assignee"] = assignee_user.get("name", "Unknown")
    elif "assignee" in update_data and "assignee_id" not in update_data:
        # Legacy: only assignee name provided, resolve to ID
        assignee_user = await db.users.find_one({"name": update_data["assignee"]})
        if assignee_user:
            new_assignee_id = str(assignee_user["_id"])
            update_data["assignee_id"] = new_assignee_id

    if new_assignee_id:
        project_id = existing.get("project_id")
        if project_id:
            project = await db.projects.find_one({"_id": ObjectId(project_id)})
            if project:
                team_members = [str(uid) for uid in project.get("team", [])]
                if new_assignee_id not in team_members:
                    raise HTTPException(
                        status_code=400,
                        detail="Assignee is not a member of this project"
                    )

    if "start_date" in update_data and update_data["start_date"]:
        update_data["start_date"] = update_data["start_date"].isoformat()
    if "due_date" in update_data and update_data["due_date"]:
        update_data["due_date"] = update_data["due_date"].isoformat()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    update_data["updated_at"] = now

    # Validate custom field values
    if "custom_field_values" in update_data and update_data["custom_field_values"]:
        await validate_custom_field_values(existing.get("project_id"), update_data["custom_field_values"], db)

    # If status is being changed to 'done', stop timer and trigger AI confirmation
    new_status = task_update.status if task_update.status is not None else existing.get("status")
    if hasattr(new_status, "value"):
        new_status = new_status.value
    elif new_status is not None:
        new_status = str(new_status)
    current_status = existing.get("status")

    if new_status != current_status:
        # Validate transitions and role capabilities (H46)
        allowed = ALLOWED_TRANSITIONS.get(current_status, [])
        if new_status in allowed:
            pass
        elif new_status in ROLLBACK_TRANSITIONS.get(current_status, []):
            raise HTTPException(
                status_code=400,
                detail="Rollback transitions are not permitted via PUT task update. Please use the task status update endpoint /api/tasks/{task_id}/status instead to provide a reason.",
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot transition from {current_status} to {new_status}",
            )

        user_role = current_user.get("role", "").lower()
        allowed_roles = STATUS_CHANGE_ROLES.get(new_status, [])
        user_role_families = _get_role_family(user_role)
        if not any(fam in allowed_roles for fam in user_role_families):
            raise HTTPException(
                status_code=403,
                detail=f"Role '{user_role}' is not allowed to change task status to '{new_status}'. Allowed roles: {', '.join(allowed_roles)}",
            )

        # Timer integrations for PUT status updates
        if current_status == "backlog" and new_status == "todo":
            if existing.get("paused_at"):
                paused_at_raw = existing["paused_at"]
                if isinstance(paused_at_raw, str):
                    paused_dt = datetime.fromisoformat(paused_at_raw.replace("Z", "+00:00"))
                else:
                    paused_dt = paused_at_raw
                pause_duration_ms = int((now - paused_dt).total_seconds() * 1000)
                update_data["is_timer_running"] = True
                update_data["paused_at"] = None
                update_data["total_paused_ms"] = existing.get("total_paused_ms", 0) + pause_duration_ms
            else:
                update_data["is_timer_running"] = True
                if not existing.get("started_at"):
                    update_data["started_at"] = now_iso
        elif current_status == "todo" and new_status == "backlog":
            if existing.get("is_timer_running"):
                update_data["is_timer_running"] = False
                update_data["paused_at"] = now_iso

    if new_status == "done" and current_status != "done":
        # Compute elapsed time
        started_at_raw = existing.get("started_at")
        paused_at_raw = existing.get("paused_at")
        total_paused_ms = existing.get("total_paused_ms", 0)

        started_at = None
        paused_at = None
        if started_at_raw:
            if isinstance(started_at_raw, str):
                started_at = datetime.fromisoformat(started_at_raw.replace("Z", "+00:00"))
            else:
                started_at = started_at_raw
        if paused_at_raw:
            if isinstance(paused_at_raw, str):
                paused_at = datetime.fromisoformat(paused_at_raw.replace("Z", "+00:00"))
            else:
                paused_at = paused_at_raw

        elapsed_sec = 0
        if started_at:
            end_at = paused_at if paused_at else now
            elapsed_sec = await calculate_office_elapsed_seconds(started_at, end_at, total_paused_ms)

        elapsed_hours = max(0, int(elapsed_sec / 3600))
        new_spent = existing.get("time_spent", 0) + elapsed_hours

        original_estimate = existing.get("estimated_hours", 0) or existing.get("remaining_hours", 0) or 0
        new_remaining = max(0, original_estimate - new_spent)

        update_data["is_timer_running"] = False
        update_data["started_at"] = None
        update_data["paused_at"] = None
        update_data["time_spent"] = new_spent
        update_data["remaining_hours"] = new_remaining

    await db.tasks.update_one({"_id": ObjectId(task_id)}, {"$set": update_data})

    # Send notification if assignee changed
    if new_assignee_id and new_assignee_id != old_assignee_id:
        await send_notification(
            user_id=new_assignee_id,
            type_="task_assigned",
            title="Task Assigned",
            message=f"You have been assigned to task: {existing['title']}",
            entity_type="task",
            entity_id=task_id,
            link=f"/dashboard/projects/{existing['project_id']}/tasks",
        )

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "task_updated",
            "entity_type": "task",
            "entity_id": task_id,
            "changes": {
                k: {"old": existing.get(k), "new": v}
                for k, v in update_data.items()
                if k != "updated_at"
            },
            "created_at": datetime.now(timezone.utc),
        }
    )

    # Trigger AI task completion confirmation if status changed to done
    if new_status == "done" and current_status != "done":
        await _trigger_ai_confirmation(task_id)

    return {"message": "Task updated successfully"}


class ChangeTaskStatusRequest(BaseModel):
    status: str
    comment: str = ""

    class Config:
        extra = "forbid"


@router.post("/{task_id}/status")
async def change_task_status(
    task_id: str,
    status_data: ChangeTaskStatusRequest,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    new_status = status_data.status
    comment = status_data.comment

    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    current_status = task["status"]
    user_role = current_user.get("role", "").lower()

    # Validate transition
    allowed = ALLOWED_TRANSITIONS.get(current_status, [])
    if new_status in allowed:
        pass  # forward transition, allowed
    elif new_status in ROLLBACK_TRANSITIONS.get(current_status, []):
        if not comment or not comment.strip():
            raise HTTPException(
                status_code=400,
                detail="Rollback requires a reason — please provide a comment",
            )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from {current_status} to {new_status}",
        )

    # Validate role — use role family matching so junior/senior/mid variants are accepted
    allowed_roles = STATUS_CHANGE_ROLES.get(new_status, [])
    user_role_families = _get_role_family(user_role)
    if not any(fam in allowed_roles for fam in user_role_families):
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user_role}' is not allowed to change task status to '{new_status}'. "
                   f"Allowed roles: {', '.join(allowed_roles)}",
        )

    # --- Timer integration ---
    now = datetime.now(timezone.utc)
    timer_action = None

    # backlog -> todo: start timer (fresh or resume)
    if current_status == "backlog" and new_status == "todo":
        if task.get("paused_at"):
            # resume from pause
            paused_at_raw = task["paused_at"]
            if isinstance(paused_at_raw, str):
                paused_dt = datetime.fromisoformat(paused_at_raw)
            else:
                paused_dt = paused_at_raw
            pause_duration_ms = int((now - paused_dt).total_seconds() * 1000)
            await db.tasks.update_one(
                {"_id": ObjectId(task_id)},
                {
                    "$set": {"is_timer_running": True, "paused_at": None, "updated_at": now},
                    "$inc": {"total_paused_ms": pause_duration_ms},
                },
            )
            timer_action = "resumed"
        else:
            # fresh start
            set_ops = {"is_timer_running": True, "updated_at": now}
            if not task.get("started_at"):
                set_ops["started_at"] = now.isoformat()
            await db.tasks.update_one(
                {"_id": ObjectId(task_id)},
                {"$set": set_ops},
            )
            timer_action = "started"

    # todo -> backlog: pause timer
    elif current_status == "todo" and new_status == "backlog":
        if task.get("is_timer_running"):
            await db.tasks.update_one(
                {"_id": ObjectId(task_id)},
                {
                    "$set": {
                        "is_timer_running": False,
                        "paused_at": now.isoformat(),
                        "updated_at": now,
                    }
                },
            )
            timer_action = "paused"
        else:
            # if not running, treat as no timer action
            timer_action = None

    # any status -> done: stop timer and finalize
    elif new_status == "done":
        # Compute elapsed if timer was running or started
        started_at_raw = task.get("started_at")
        if started_at_raw:
            if isinstance(started_at_raw, str):
                started_dt = datetime.fromisoformat(started_at_raw)
            else:
                started_dt = started_at_raw
        else:
            started_dt = None

        total_paused_ms = task.get("total_paused_ms", 0)
        elapsed_sec = 0
        if started_dt:
            elapsed_sec = await calculate_office_elapsed_seconds(started_dt, now, total_paused_ms)
        elapsed_hours = max(0, int(elapsed_sec / 3600))

        current_spent = task.get("time_spent", 0)
        current_remaining = task.get("remaining_hours") or task.get("estimated_hours") or 0
        new_spent = current_spent + elapsed_hours
        new_remaining = max(0, current_remaining - elapsed_hours)

        await db.tasks.update_one(
            {"_id": ObjectId(task_id)},
            {
                "$set": {
                    "is_timer_running": False,
                    "started_at": None,
                    "paused_at": None,
                    "time_spent": new_spent,
                    "remaining_hours": new_remaining,
                    "updated_at": now,
                }
            },
        )
        timer_action = "stopped"
    else:
        timer_action = None

    # Update status (always)
    await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {"status": new_status, "updated_at": now}},
    )

    # Log status change
    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "status_changed",
            "entity_type": "task",
            "entity_id": task_id,
            "changes": {"status": {"old": current_status, "new": new_status}},
            "metadata": {"comment": comment, "timer_action": timer_action},
            "created_at": now,
        }
    )

    # Admin Alert Notification on Rollback or Moving Another User's Task
    is_rollback = new_status in ROLLBACK_TRANSITIONS.get(current_status, [])
    is_others_task = task.get("assignee_id") is not None and str(task.get("assignee_id")) != str(current_user["id"])

    if is_rollback or is_others_task:
        # Fetch project details to get project name
        project = None
        project_id_str = task.get("project_id")
        if project_id_str:
            try:
                project = await db.projects.find_one({"_id": ObjectId(project_id_str)})
            except Exception:
                pass
        project_name = project.get("name", "Unknown") if project else "Unknown"

        assignee_name = task.get("assignee", "Unassigned")
        user_name = current_user.get("name") or current_user.get("email", "Unknown")

        status_display = {
            "backlog": "Backlog",
            "todo": "Todo",
            "in_progress": "In Progress",
            "code_review": "Code Review",
            "testing": "Testing",
            "done": "Done",
        }
        current_status_display = status_display.get(current_status, current_status)
        new_status_display = status_display.get(new_status, new_status)
        reason_text = comment.strip() if comment else "No reason provided"

        metadata_payload = {
            "task_name": task.get("title", "Untitled"),
            "project_name": project_name,
            "assignee": assignee_name,
            "moved_by": user_name,
            "previous_status": current_status_display,
            "new_status": new_status_display,
            "reason": reason_text,
            "date_time": now.isoformat(),
        }

        if is_rollback:
            alert_message = f'Task "{task.get("title", "Untitled")}" was moved from "{current_status_display}" to "{new_status_display}" by {user_name}. Reason: "{reason_text}". Please review the change.'
        else:
            alert_message = f'Task "{task.get("title", "Untitled")}" assigned to {assignee_name} was moved by {user_name}. Reason: "{reason_text}". Please review the change.'

        await notify_role_watchers(
            notify_roles=["admin"],
            type_="ai_admin_alert",
            title="Task Movement Requires Review",
            message=alert_message,
            entity_type="task",
            entity_id=task_id,
            link=f"/dashboard/projects/{task.get('project_id')}/kanban?task={task_id}",
            metadata=metadata_payload,
        )

    # Notify admin/team_lead/hr of status change (in-app only)
    if current_status != new_status:
        await notify_role_watchers(
            notify_roles=["admin", "team_lead", "hr"],
            type_="task_status_changed",
            title=f"Task Status: {task.get('title', 'Untitled')}",
            message=f"Task '{task.get('title', 'Untitled')}' moved from {current_status} → {new_status}",
            entity_type="task",
            entity_id=task_id,
            link=f"/dashboard/projects/{task.get('project_id')}/kanban",
            exclude_user_id=current_user["id"],
        )

    # Trigger AI task completion confirmation if task is marked done
    if new_status == TaskStatus.DONE.value and current_status != TaskStatus.DONE.value:
        await _trigger_ai_confirmation(task_id)

    return {"message": f"Status changed to {new_status}"}




@router.delete("/{task_id}")
async def delete_task(
    task_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await _check_task_access(task, current_user, db)
    if task["reporter"] != current_user["id"] and current_user.get("role", "").lower() not in [
        "admin",
        "team_lead",
    ]:
        raise HTTPException(status_code=403, detail="Access denied")

    subtask_ids = task.get("subtask_ids", [])
    if subtask_ids:
        valid_oids = [
            ObjectId(sid) for sid in subtask_ids
            if isinstance(sid, ObjectId) or (isinstance(sid, str) and ObjectId.is_valid(sid))
        ]
        if valid_oids:
            await db.tasks.delete_many({"_id": {"$in": valid_oids}})

    if task.get("parent_id"):
        await db.tasks.update_one(
            {"_id": ObjectId(task["parent_id"])}, {"$pull": {"subtask_ids": task_id}}
        )

    drive_file_ids = [
        attachment.get("drive_file_id")
        for attachment in task.get("attachments", [])
        if attachment.get("drive_file_id")
    ]
    for drive_file_id in drive_file_ids:
        try:
            delete_from_drive(drive_file_id)
        except Exception as exc:
            logger.warning("Failed to delete Drive file %s for task %s: %s", drive_file_id, task_id, exc)

    if task.get("project_id"):
        source_attachment_ids = [
            attachment.get("_id")
            for attachment in task.get("attachments", [])
            if attachment.get("_id")
        ]
        if source_attachment_ids:
            await db.projects.update_one(
                {"_id": ObjectId(task["project_id"]), "attachments": {"$type": "array"}},
                {
                    "$pull": {"attachments": {"source_attachment_id": {"$in": source_attachment_ids}}},
                    "$set": {"updated_at": datetime.now(timezone.utc)},
                },
            )

    await db.tasks.delete_one({"_id": ObjectId(task_id)})

    await db.projects.update_many(
        {"attachments": {"$type": "array"}},
        {"$pull": {"attachments": {"source_type": "task", "source_task_id": task_id}}},
    )

    return {"message": "Task deleted successfully"}


@router.get("/{task_id}/history")
async def get_task_history(
    task_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await _check_task_access(task, current_user, db)

    cursor = db.activity_logs.find({"entity_id": task_id, "entity_type": "task"}).sort(
        "created_at", -1
    )
    logs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        logs.append(doc)
    return logs


@router.get("/{task_id}/subtasks")
async def list_subtasks(
    task_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    parent = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not parent:
        raise HTTPException(status_code=404, detail="Task not found")
    await _check_task_access(parent, current_user, db)

    subtask_ids = parent.get("subtask_ids", [])
    valid_oids = [
        ObjectId(sid) for sid in subtask_ids
        if isinstance(sid, ObjectId) or (isinstance(sid, str) and ObjectId.is_valid(sid))
    ]
    if not valid_oids:
        return []

    cursor = db.tasks.find({"_id": {"$in": valid_oids}})
    subtasks = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if "reporter" in doc:
            doc["reporter"] = str(doc["reporter"])
        subtasks.append(doc)
    return subtasks


@router.post("/{task_id}/subtasks")
async def create_subtask(
    task_id: str,
    task: TaskCreate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    parent = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not parent:
        raise HTTPException(status_code=404, detail="Parent task not found")

    await _check_task_access(parent, current_user, db)
    # Permission check: only admin/team_lead or the task's assignee/reporter can add subtasks
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and \
       current_user["id"] not in [parent.get("assignee_id"), parent.get("reporter")]:
        raise HTTPException(status_code=403, detail="Not authorized to add subtasks to this task")

    # Subtasks always inherit the parent task's assignee — never override
    task.assignee_id = parent.get("assignee_id")
    task.assignee = parent.get("assignee")

    # Resolve assignee name if assignee_id provided
    assignee_name = None
    assignee_id_val = task.assignee_id
    if assignee_id_val:
        assignee_user = await db.users.find_one({"_id": ObjectId(assignee_id_val)})
        if assignee_user:
            assignee_name = assignee_user.get("name", "Unknown")
        else:
            assignee_name = task.assignee
    else:
        assignee_name = task.assignee
        # If only name provided, resolve to user ID so we can auto-add to project team
        if assignee_name:
            assignee_user_by_name = await db.users.find_one({"name": assignee_name})
            if assignee_user_by_name:
                assignee_id_val = str(assignee_user_by_name["_id"])

    task_doc = {
         "project_id": task.project_id,
         "title": task.title,
         "description": task.description,
         "role": task.role.value,
         "priority": task.priority.value,
         "status": task.status.value,
         "complexity": task.complexity.value,
         "tags": task.tags,
         "estimated_hours": task.estimated_hours,
         "story_points": task.story_points,
         "time_spent": 0,
         "remaining_hours": task.estimated_hours,
         "ai_generated": task.ai_generated,
         "source_markdown": task.source_markdown,
         "order": task.order,
          "assignee": assignee_name,
          "assignee_id": assignee_id_val,
          "reporter": current_user["id"],
          "reporter_name": current_user.get("name", "Unknown"),
          "dependencies": task.dependencies,
         "attachments": [],
         "start_date": task.start_date.isoformat() if task.start_date else None,
         "due_date": task.due_date.isoformat() if task.due_date else None,
         "sprint": task.sprint,
         "parent_id": task.parent_id,
         "epic_id": task.epic_id,
         "subtask_ids": [],
         "custom_field_values": task.custom_field_values or {},
         "images": [img.model_dump() for img in task.images] if task.images else None,
         "started_at": None,
         "paused_at": None,
         "total_paused_ms": 0,
         "is_timer_running": False,
         "created_at": datetime.now(timezone.utc),
         "updated_at": datetime.now(timezone.utc),
    }

    result = await db.tasks.insert_one(task_doc)
    new_id = str(result.inserted_id)

    await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {
            "$push": {"subtask_ids": new_id},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "subtask_created",
            "entity_type": "task",
            "entity_id": new_id,
            "metadata": {"title": task.title, "parent_id": task_id},
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"id": new_id, "message": "Subtask created successfully"}


@router.put("/{task_id}/subtasks/{subtask_id}")
async def update_subtask(
    task_id: str,
    subtask_id: str,
    task_update: TaskUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    subtask = await db.tasks.find_one({"_id": ObjectId(subtask_id)})
    if not subtask or subtask.get("parent_id") != task_id:
        raise HTTPException(
            status_code=404, detail="Subtask not found under this parent"
        )

    await _check_task_access(subtask, current_user, db)
    # Permission: only subtask reporter or admin/team_lead can update
    if subtask.get("reporter") != current_user["id"] and current_user.get("role", "").lower() not in [
        "admin",
        "team_lead",
    ]:
        raise HTTPException(
            status_code=403, detail="Only subtask reporter or admin/team_lead can update"
        )

    update_data = {k: v for k, v in task_update.model_dump().items() if v is not None}
    if "due_date" in update_data and update_data["due_date"]:
        update_data["due_date"] = update_data["due_date"].isoformat()
    update_data["updated_at"] = datetime.now(timezone.utc)

    await db.tasks.update_one({"_id": ObjectId(subtask_id)}, {"$set": update_data})

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "task_updated",
            "entity_type": "task",
            "entity_id": subtask_id,
            "metadata": {
                "parent_id": task_id,
                "fields_updated": list(update_data.keys()),
            },
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"message": "Subtask updated successfully"}


@router.delete("/{task_id}/subtasks/{subtask_id}")
async def delete_subtask(
    task_id: str,
    subtask_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    subtask = await db.tasks.find_one({"_id": ObjectId(subtask_id)})
    if not subtask or subtask.get("parent_id") != task_id:
        raise HTTPException(
            status_code=404, detail="Subtask not found under this parent"
        )

    await _check_task_access(subtask, current_user, db)
    # Permission: only subtask reporter or admin/team_lead can delete
    if subtask.get("reporter") != current_user["id"] and current_user.get("role", "").lower() not in [
        "admin",
        "team_lead",
    ]:
        raise HTTPException(
            status_code=403, detail="Only subtask reporter or admin/team_lead can delete"
        )

    await db.tasks.delete_one({"_id": ObjectId(subtask_id)})

    await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {
            "$pull": {"subtask_ids": subtask_id},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "subtask_deleted",
            "entity_type": "task",
            "entity_id": subtask_id,
            "metadata": {"parent_id": task_id},
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"message": "Subtask deleted successfully"}


class LogTimeRequest(BaseModel):
    hours: float = 0

    class Config:
        extra = "forbid"


@router.post("/{task_id}/log-time")
async def log_time(
    task_id: str,
    time_data: LogTimeRequest,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    hours = time_data.hours
    if hours <= 0:
        raise HTTPException(status_code=400, detail="Hours must be positive")

    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await _check_task_access(task, current_user, db)

    await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {
            "$inc": {"time_spent": hours},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )
    task_after = await db.tasks.find_one({"_id": ObjectId(task_id)}, {"time_spent": 1, "estimated_hours": 1})
    new_spent = (task_after or task).get("time_spent", 0)
    new_remaining = max(0, ((task_after or task).get("estimated_hours") or 0) - new_spent)
    await db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {"remaining_hours": new_remaining}},
    )

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "time_logged",
            "entity_type": "task",
            "entity_id": task_id,
            "metadata": {
                "hours_added": hours,
                "total_spent": new_spent,
                "remaining": max(0, (task.get("estimated_hours") or 0) - new_spent),
            },
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {
        "message": f"Logged {hours}h",
        "time_spent": new_spent,
        "remaining_hours": new_remaining,
    }


# ---------- Timer Endpoints ----------
@router.post("/{task_id}/timer/start")
async def start_timer(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Start the timer for a task. Sets started_at if not already set."""
    db = _get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await _check_task_access(task, current_user, db)

    now = datetime.now(timezone.utc)
    # Build update using simpler approach
    set_ops = {"is_timer_running": True, "updated_at": now}
    inc_ops = {}
    if not task.get("started_at"):
        set_ops["started_at"] = now.isoformat()
    if task.get("paused_at"):
        # compute paused duration
        paused_at_raw = task["paused_at"]
        if isinstance(paused_at_raw, str):
            paused_dt = datetime.fromisoformat(paused_at_raw)
        else:
            paused_dt = paused_at_raw
        paused_duration = (now - paused_dt).total_seconds() * 1000
        inc_ops["total_paused_ms"] = int(paused_duration)
        set_ops["paused_at"] = None
    update_doc = {"$set": set_ops}
    if inc_ops:
        update_doc["$inc"] = inc_ops

    # Conditional update: only if timer is not already running
    result = await db.tasks.update_one(
        {"_id": ObjectId(task_id), "is_timer_running": False},
        update_doc
    )
    if result.matched_count == 0:
        # Timer already running or state changed; fetch current and return
        updated = await db.tasks.find_one({"_id": ObjectId(task_id)})
        return {"message": "Timer already running", "timer": await _build_timer_state(updated)}

    # Log activity
    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "timer_started",
            "entity_type": "task",
            "entity_id": task_id,
            "metadata": {"previous_state": {"is_timer_running": task.get("is_timer_running"), "paused_at": task.get("paused_at")}},
            "created_at": now,
        }
    )

    # Fetch updated task to return timer state
    updated = await db.tasks.find_one({"_id": ObjectId(task_id)})
    return {"message": "Timer started", "timer": await _build_timer_state(updated)}


@router.post("/{task_id}/timer/pause")
async def pause_timer(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Pause the timer for a task."""
    db = _get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await _check_task_access(task, current_user, db)
    if not task.get("is_timer_running"):
        raise HTTPException(status_code=400, detail="Timer is not running")

    now = datetime.now(timezone.utc)
    update_doc = {
        "$set": {
            "is_timer_running": False,
            "paused_at": now.isoformat(),
            "updated_at": now,
        }
    }
    result = await db.tasks.update_one(
        {"_id": ObjectId(task_id), "is_timer_running": True},
        update_doc
    )
    if result.matched_count == 0:
        # Timer not running; fetch current state
        updated = await db.tasks.find_one({"_id": ObjectId(task_id)})
        raise HTTPException(status_code=400, detail="Timer is not running")

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "timer_paused",
            "entity_type": "task",
            "entity_id": task_id,
            "metadata": {},
            "created_at": now,
        }
    )

    updated = await db.tasks.find_one({"_id": ObjectId(task_id)})
    return {"message": "Timer paused", "timer": await _build_timer_state(updated)}


@router.post("/{task_id}/timer/resume")
async def resume_timer(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Resume a paused timer."""
    db = _get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await _check_task_access(task, current_user, db)
    if task.get("is_timer_running"):
        raise HTTPException(status_code=400, detail="Timer is already running")
    if not task.get("paused_at"):
        raise HTTPException(status_code=400, detail="Timer is not paused")

    now = datetime.now(timezone.utc)
    paused_at_raw = task["paused_at"]
    if isinstance(paused_at_raw, str):
        paused_dt = datetime.fromisoformat(paused_at_raw)
    else:
        paused_dt = paused_at_raw
    paused_duration = (now - paused_dt).total_seconds() * 1000  # ms

    update_doc = {
        "$set": {
            "is_timer_running": True,
            "paused_at": None,
            "updated_at": now,
        },
        "$inc": {"total_paused_ms": int(paused_duration)},
    }
    # Conditional update: only if timer is paused and not running
    result = await db.tasks.update_one(
        {"_id": ObjectId(task_id), "is_timer_running": False, "paused_at": {"$ne": None}},
        update_doc
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=400, detail="Timer is already running or not paused")

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "timer_resumed",
            "entity_type": "task",
            "entity_id": task_id,
            "metadata": {"paused_for_ms": int(paused_duration)},
            "created_at": now,
        }
    )

    updated = await db.tasks.find_one({"_id": ObjectId(task_id)})
    return {"message": "Timer resumed", "timer": await _build_timer_state(updated)}


@router.post("/{task_id}/timer/stop")
async def stop_timer(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Stop the timer and finalize time_spent. Typically called when task is Done."""
    db = _get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await _check_task_access(task, current_user, db)
    if not task.get("is_timer_running") and not task.get("started_at"):
        raise HTTPException(status_code=400, detail="Timer is not running")

    now = datetime.now(timezone.utc)
    started_at_raw = task.get("started_at")
    if started_at_raw:
        if isinstance(started_at_raw, str):
            started_dt = datetime.fromisoformat(started_at_raw)
        else:
            started_dt = started_at_raw
    else:
        started_dt = None

    total_paused_ms = task.get("total_paused_ms", 0)
    if started_dt:
        elapsed_sec = await calculate_office_elapsed_seconds(started_dt, now, total_paused_ms)
    else:
        elapsed_sec = 0

    elapsed_hours = max(0, int(elapsed_sec / 3600))

    # Update task: stop timer, add elapsed to time_spent, remaining = max(0, remaining - elapsed_hours)
    current_spent = task.get("time_spent", 0)
    current_remaining = task.get("remaining_hours")
    if current_remaining is None:
        current_remaining = task.get("estimated_hours") or 0
    new_spent = current_spent + elapsed_hours
    new_remaining = max(0, current_remaining - elapsed_hours)

    set_ops = {
        "is_timer_running": False,
        "started_at": None,
        "paused_at": None,
        "time_spent": new_spent,
        "remaining_hours": new_remaining,
        "updated_at": now,
    }
    # Conditional update: only if started_at is not None (timer not already stopped)
    result = await db.tasks.update_one(
        {"_id": ObjectId(task_id), "started_at": {"$ne": None}},
        {"$set": set_ops}
    )
    if result.matched_count == 0:
        # Timer already stopped; fetch current state
        updated = await db.tasks.find_one({"_id": ObjectId(task_id)})
        return {"message": "Timer already stopped", "time_spent": updated.get("time_spent", 0), "remaining_hours": updated.get("remaining_hours", 0), "timer": await _build_timer_state(updated)}

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "timer_stopped",
            "entity_type": "task",
            "entity_id": task_id,
            "metadata": {"elapsed_hours": elapsed_hours, "time_spent": new_spent, "remaining_hours": new_remaining},
            "created_at": now,
        }
    )

    updated = await db.tasks.find_one({"_id": ObjectId(task_id)})
    return {"message": "Timer stopped", "time_spent": new_spent, "remaining_hours": new_remaining, "timer": await _build_timer_state(updated)}


@router.get("/{task_id}/timer")
async def get_timer_state(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Get current timer state and calculated time_spent."""
    db = _get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await _check_task_access(task, current_user, db)
    return {"timer": await _build_timer_state(task)}


async def _build_timer_state(task: dict) -> dict:
    """Helper to build timer state with calculated elapsed time (office hours only)."""
    now = datetime.now(timezone.utc)
    started_at_raw = task.get("started_at")
    paused_at_raw = task.get("paused_at")
    is_running = task.get("is_timer_running", False)
    total_paused_ms = task.get("total_paused_ms", 0)

    started_dt = None
    paused_dt = None
    if started_at_raw:
        if isinstance(started_at_raw, str):
            started_dt = datetime.fromisoformat(started_at_raw)
        else:
            started_dt = started_at_raw
    if paused_at_raw:
        if isinstance(paused_at_raw, str):
            paused_dt = datetime.fromisoformat(paused_at_raw)
        else:
            paused_dt = paused_at_raw

    elapsed_seconds = 0
    if started_dt:
        end_dt = paused_dt if paused_dt else now
        elapsed_seconds = await calculate_office_elapsed_seconds(started_dt, end_dt, total_paused_ms)

    state = {
        "is_timer_running": is_running,
        "started_at": started_at_raw,
        "paused_at": paused_at_raw,
        "total_paused_ms": total_paused_ms,
        "elapsed_seconds": elapsed_seconds,
        "time_spent": task.get("time_spent", 0),
        "remaining_hours": task.get("remaining_hours"),
    }
    return state


@router.get("/{task_id}")
async def get_task(task_id: str, current_user: dict = Depends(get_current_active_user)):
    db = _get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Project membership check
    project_id = task.get("project_id")
    if project_id:
        project = await db.projects.find_one({"_id": ObjectId(project_id)})
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        user_role = current_user.get("role", "").lower()
        if user_role not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")

    task["_id"] = str(task["_id"])
    if "reporter" in task:
        task["reporter"] = str(task["reporter"])

    subtask_ids = task.get("subtask_ids", [])
    subtasks = []
    if subtask_ids:
        valid_oids = [
            ObjectId(sid) for sid in subtask_ids
            if isinstance(sid, ObjectId) or (isinstance(sid, str) and ObjectId.is_valid(sid))
        ]
        if valid_oids:
            cursor = db.tasks.find({"_id": {"$in": valid_oids}})
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                if "reporter" in doc:
                    doc["reporter"] = str(doc["reporter"])
                subtasks.append(doc)
    task["subtasks"] = subtasks
    # Enrich with assignee role
    if task.get("assignee_id"):
        assignee = await db.users.find_one({"_id": ObjectId(task["assignee_id"])})
        if assignee:
            task["assignee_role"] = assignee.get("role", "developer")
    else:
        task["assignee_role"] = None
    # Enrich with reporter name
    if task.get("reporter"):
        try:
            reporter = await db.users.find_one({"_id": ObjectId(task["reporter"])})
            if reporter:
                task["reporter_name"] = reporter.get("name", task["reporter"])
            else:
                task["reporter_name"] = task["reporter"]
        except Exception:
            task["reporter_name"] = task["reporter"]
    else:
        task["reporter_name"] = "Unknown"
    return task


# ── AI Task Completion Confirmation Trigger ──────────────────────────────────

async def _trigger_ai_confirmation(task_id: str):
    """
    Create a confirmation record and send notification when a task is marked done.
    """
    try:
        from models.ai_task_monitor import TaskCompletionConfirmation, ConfirmationStatus

        db = get_database()
        task = await db.tasks.find_one({"_id": ObjectId(task_id)})
        if not task:
            logger.warning("Task %s not found for AI confirmation", task_id)
            return

        project_id = task.get("project_id")
        developer_id = task.get("assignee_id")
        if not developer_id:
            logger.info("Task %s has no assignee, skipping AI confirmation", task_id)
            return

        # Check if a pending confirmation already exists
        existing = await db.task_completion_confirmations.find_one(
            {"task_id": task_id, "status": "pending"}
        )
        if existing:
            logger.info("Pending confirmation already exists for task %s", task_id)
            return

        confirmation = TaskCompletionConfirmation(
            task_id=task_id,
            project_id=project_id,
            developer_id=developer_id,
            status=ConfirmationStatus.PENDING,
        )
        doc = confirmation.model_dump(by_alias=True, exclude={"id"})
        result = await db.task_completion_confirmations.insert_one(doc)
        confirmation_id = str(result.inserted_id)

        await send_notification(
            user_id=developer_id,
            type_="ai_task_confirmation",
            title="Task Completion Check",
            message=f"Have you completed your task '{task.get('title')}'? Do you want to move to the next task?",
            entity_type="task_completion_confirmation",
            entity_id=confirmation_id,
            link="/dashboard/ai/task-monitor",
        )
        logger.info("AI confirmation triggered for task %s", task_id)
    except Exception as e:
        logger.exception("Failed to trigger AI task completion confirmation for task %s", task_id)


# ── Batch Assignment Notifications ────────────────────────────────────────────

from typing import Dict, List

class BatchNotifyRequest(BaseModel):
    """Request body for batch assignment notifications."""
    project_id: str
    task_ids: Optional[List[str]] = None  # if empty, fetches all tasks for project with assignees

    class Config:
        extra = "forbid"


@router.post("/notify-assignments")
async def notify_assignments_batch(
    data: BatchNotifyRequest,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Send ONE consolidated notification per developer for a batch of assigned tasks.
    Used after creating multiple tasks (e.g., AI-generated) to avoid email spam.
    """
    db = _get_db()

    # Verify project exists and user has access
    project = await db.projects.find_one({"_id": ObjectId(data.project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Fetch tasks to notify about
    query: dict = {"project_id": data.project_id, "assignee_id": {"$exists": True, "$ne": None}}
    if data.task_ids:
        query["_id"] = {"$in": [ObjectId(tid) for tid in data.task_ids]}

    tasks_cursor = db.tasks.find(query)
    tasks = []
    async for t in tasks_cursor:
        tasks.append({
            "_id": str(t["_id"]),
            "title": t.get("title", "Untitled"),
            "assignee_id": t.get("assignee_id"),
            "assignee": t.get("assignee"),
        })

    # Group tasks by assignee_id
    assignee_groups: Dict[str, List[dict]] = {}
    for task in tasks:
        aid = task.get("assignee_id")
        if aid:
            assignee_groups.setdefault(aid, []).append(task)

    # Send one notification per developer
    sent_count = 0
    for assignee_id, assigned_tasks in assignee_groups.items():
        # Skip if assignee_id is the current user (they already know they created tasks)
        if assignee_id == current_user["id"]:
            continue

        task_titles = [t["title"] for t in assigned_tasks]
        count = len(assigned_tasks)

        if count == 1:
            message = f"You have been assigned to task: {task_titles[0]}"
        else:
            # Format: "You have been assigned to 3 tasks: Task 1, Task 2, Task 3"
            titles_str = ", ".join(task_titles)
            message = f"You have been assigned to {count} tasks: {titles_str}"

        await send_notification(
            user_id=assignee_id,
            type_="task_assigned",
            title=f"{count} Task{'s' if count > 1 else ''} Assigned",
            message=message,
            entity_type="project",
            entity_id=data.project_id,
            link=f"/dashboard/projects/{data.project_id}/tasks",
        )
        sent_count += 1

    return {"success": True, "notifications_sent": sent_count, "developers_notified": len(assignee_groups)}





