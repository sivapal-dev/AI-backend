from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from models.off_project_task import (
    OffProjectTaskCreate,
    OffProjectTaskUpdate,
    OffProjectTaskResponse,
    OffProjectTaskStatus,
    OffProjectTaskPriority,
)
from dependencies import get_current_active_user
from helpers.notification_sender import send_notification

router = APIRouter(prefix="/off-project-tasks", tags=["Off-Project Tasks"])


def _get_db():
    return get_database()


@router.get("", response_model=List[dict])
async def list_off_project_tasks(
    status: Optional[str] = Query(None, description="Filter by status"),
    assignee_id: Optional[str] = Query(None, description="Filter by assignee"),
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    query = {}

    if current_user.get("role", "").lower() not in ["admin", "team_lead", "hr"]:
        query["assignee_id"] = current_user["id"]

    if status:
        query["status"] = status
    if assignee_id and current_user.get("role", "").lower() in ["admin", "team_lead", "hr"]:
        query["assignee_id"] = assignee_id

    tasks = await db.off_project_tasks.find(query).sort("created_at", -1).to_list(500)
    # Batch fetch assignee roles
    assignee_ids = list(set(t.get("assignee_id") for t in tasks if t.get("assignee_id")))
    assignee_role_map = {}
    if assignee_ids:
        valid_oids = []
        for aid in assignee_ids:
            try:
                valid_oids.append(ObjectId(aid))
            except Exception:
                pass
        if valid_oids:
            async for u in db.users.find({"_id": {"$in": valid_oids}}, {"role": 1}):
                assignee_role_map[str(u["_id"])] = u.get("role", "developer")

    for t in tasks:
        t["_id"] = str(t["_id"])
        t["assignee_role"] = assignee_role_map.get(t.get("assignee_id", "")) if t.get("assignee_id") else None
    return tasks


@router.post("", response_model=dict)
async def create_off_project_task(
    task: OffProjectTaskCreate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()

    # Only admin/team_lead/hr can assign tasks to other users
    if task.assignee_id and task.assignee_id != current_user["id"]:
        role = current_user.get("role", "").lower()
        if role not in ["admin", "team_lead", "hr"]:
            raise HTTPException(status_code=403, detail="Only administrators, team leads, and HR can assign tasks to other users")

    assignee_id = task.assignee_id if task.assignee_id else current_user["id"]
    assignee_name = (
        task.assignee_name
        if task.assignee_name
        else current_user.get("name", current_user.get("email", "Unknown"))
    )

    task_doc = {
        "title": task.title,
        "description": task.description,
        "priority": task.priority.value,
        "status": task.status.value,
        "estimated_hours": task.estimated_hours,
        "actual_hours": task.actual_hours,
        "notes": task.notes,
        "assignee_id": assignee_id,
        "assignee_name": assignee_name,
        "created_by": current_user["id"],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = await db.off_project_tasks.insert_one(task_doc)
    task_doc["_id"] = str(result.inserted_id)
    task_doc["id"] = task_doc["_id"]

    # Notify assignee
    await send_notification(
        user_id=assignee_id,
        type_="task_assigned",
        title="Task Assigned",
        message=f"You have been assigned to off-project task: {task.title}",
        entity_type="off_project_task",
        entity_id=str(result.inserted_id),
        link="/dashboard/off-project",
    )

    # Enrich with assignee role
    if task_doc.get("assignee_id"):
        assignee = await db.users.find_one({"_id": ObjectId(task_doc["assignee_id"])})
        if assignee:
            task_doc["assignee_role"] = assignee.get("role", "developer")
    else:
        task_doc["assignee_role"] = None
    return task_doc


@router.get("/{task_id}", response_model=dict)
async def get_off_project_task(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    try:
        task = await db.off_project_tasks.find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if (
        current_user.get("role", "").lower() not in ["admin", "team_lead", "hr"]
        and task.get("assignee_id") != current_user["id"]
    ):
        raise HTTPException(status_code=403, detail="Not authorized to view this task")

    task["_id"] = str(task["_id"])
    task["id"] = task["_id"]
    # Enrich with assignee role
    if task.get("assignee_id"):
        assignee = await db.users.find_one({"_id": ObjectId(task["assignee_id"])})
        if assignee:
            task["assignee_role"] = assignee.get("role", "developer")
    else:
        task["assignee_role"] = None
    return task


@router.put("/{task_id}", response_model=dict)
async def update_off_project_task(
    task_id: str,
    update: OffProjectTaskUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()

    try:
        existing = await db.off_project_tasks.find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    if not existing:
        raise HTTPException(status_code=404, detail="Task not found")

    is_creator = existing["created_by"] == current_user["id"]
    is_assignee = existing.get("assignee_id") == current_user["id"]
    is_privileged = current_user.get("role", "").lower() in ["admin", "team_lead", "hr"]

    if not (is_creator or is_assignee or is_privileged):
        raise HTTPException(
            status_code=403, detail="Not authorized to update this task"
        )

    update_data = {k: v for k, v in update.model_dump().items() if v is not None}
    
    # Handle status rollback validation, history logging, activity logs, and notifications
    if "status" in update_data:
        new_status = update_data["status"].value
        update_data["status"] = new_status
        
        old_status = existing.get("status")
        if old_status != new_status:
            status_order = {"todo": 1, "in_progress": 2, "done": 3}
            is_rollback_transition = status_order.get(new_status, 0) < status_order.get(old_status, 0)
            
            if is_rollback_transition:
                rollback_reason = update_data.get("rollback_reason")
                if not rollback_reason or not rollback_reason.strip():
                    raise HTTPException(
                        status_code=400,
                        detail="Rollback reason is required."
                    )
                
                # 1. Save rollback reason/history in database (task document)
                status_history = existing.get("status_history") or []
                now_utc = datetime.now(timezone.utc)
                history_entry = {
                    "status_from": old_status,
                    "status_to": new_status,
                    "reason": rollback_reason.strip(),
                    "changed_by": current_user["id"],
                    "changed_by_name": current_user.get("name", "Unknown"),
                    "changed_at": now_utc.isoformat()
                }
                status_history.append(history_entry)
                update_data["status_history"] = status_history
                
                # 2. Create activity log
                await db.activity_logs.insert_one(
                    {
                        "user_id": current_user["id"],
                        "action": "status_changed",
                        "entity_type": "off_project_task",
                        "entity_id": task_id,
                        "metadata": {
                            "title": existing["title"],
                            "old_status": old_status,
                            "new_status": new_status,
                            "rollback_reason": rollback_reason.strip()
                        },
                        "created_at": now_utc
                    }
                )
                
                # 3. Notify assigned users
                notif_recipients = set([existing.get("assignee_id"), existing.get("created_by")])
                for uid in notif_recipients:
                    if uid and uid != current_user["id"]:
                        await send_notification(
                            user_id=uid,
                            type_="task_status_changed",
                            title="Off-Project Task Status Rolled Back",
                            message=(
                                f"Task status rolled back from {old_status.replace('_', ' ').title()} "
                                f"to {new_status.replace('_', ' ').title()}.\n\n"
                                f"Reason:\n{rollback_reason.strip()}\n\n"
                                f"Changed By:\n{current_user.get('name', 'Unknown')}"
                            ),
                            entity_type="off_project_task",
                            entity_id=task_id,
                            link="/dashboard/off-project",
                        )
            else:
                # Regular status change (forward or no change)
                status_history = existing.get("status_history") or []
                now_utc = datetime.now(timezone.utc)
                history_entry = {
                    "status_from": old_status,
                    "status_to": new_status,
                    "reason": None,
                    "changed_by": current_user["id"],
                    "changed_by_name": current_user.get("name", "Unknown"),
                    "changed_at": now_utc.isoformat()
                }
                status_history.append(history_entry)
                update_data["status_history"] = status_history
                
                await db.activity_logs.insert_one(
                    {
                        "user_id": current_user["id"],
                        "action": "status_changed",
                        "entity_type": "off_project_task",
                        "entity_id": task_id,
                        "metadata": {
                            "title": existing["title"],
                            "old_status": old_status,
                            "new_status": new_status
                        },
                        "created_at": now_utc
                    }
                )
                
    # Remove rollback_reason from update_data so it doesn't get saved as a top level field in MongoDB
    update_data.pop("rollback_reason", None)

    if "priority" in update_data:
        update_data["priority"] = update_data["priority"].value
    update_data["updated_at"] = datetime.now(timezone.utc)

    await db.off_project_tasks.update_one(
        {"_id": ObjectId(task_id)}, {"$set": update_data}
    )

    updated = await db.off_project_tasks.find_one({"_id": ObjectId(task_id)})
    updated["_id"] = str(updated["_id"])
    updated["id"] = updated["_id"]
    # Enrich with assignee role
    if updated.get("assignee_id"):
        assignee = await db.users.find_one({"_id": ObjectId(updated["assignee_id"])})
        if assignee:
            updated["assignee_role"] = assignee.get("role", "developer")
    else:
        updated["assignee_role"] = None
    return updated


@router.delete("/{task_id}")
async def delete_off_project_task(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()

    try:
        existing = await db.off_project_tasks.find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    if not existing:
        raise HTTPException(status_code=404, detail="Task not found")

    is_creator = existing["created_by"] == current_user["id"]
    is_assignee = existing.get("assignee_id") == current_user["id"]
    is_privileged = current_user.get("role", "").lower() in ["admin", "team_lead", "hr"]

    if not (is_creator or is_assignee or is_privileged):
        raise HTTPException(
            status_code=403, detail="Not authorized to delete this task"
        )

    await db.off_project_tasks.delete_one({"_id": ObjectId(task_id)})
    return {"message": "Task deleted"}
