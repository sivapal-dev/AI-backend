import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from bson import ObjectId

from dependencies import get_current_active_user
from database import get_database
from models.ai_task_monitor import TaskCompletionConfirmation, ConfirmationStatus
from helpers.notification_sender import send_notification
from helpers.ai_task_selector import pick_next_task
from helpers.ai_reason_validator import validate_completion_reason
from scheduler import schedule_force_assign

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ai-task-monitor"])


# ── 1. Trigger AI confirmation when task marked done ─────────────────────────
@router.post("/ai/task-completion/confirm/{task_id}")
async def trigger_task_completion_confirmation(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Trigger the AI confirmation flow when a task is marked done.
    Creates a confirmation record and sends a notification to the developer.
    """
    db = get_database()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project_id = task.get("project_id")
    developer_id = task.get("assignee_id")
    if not developer_id:
        raise HTTPException(status_code=400, detail="Task has no assignee")
        
    # Project team membership access check (W212)
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    team = [str(uid) for uid in project.get("team", [])]
    
    # Verify developer is a team member
    if str(developer_id) not in team:
        raise HTTPException(
            status_code=403,
            detail="Developer is not a team member of the project"
        )

    # Verify current user is admin or a team member
    current_user_role = current_user.get("role", "").lower()
    current_user_id_str = str(current_user.get("_id") or current_user.get("id"))
    if current_user_role != "admin" and current_user_id_str not in team:
        raise HTTPException(
            status_code=403,
            detail="Access denied: You are not a member of this project"
        )

    # Create confirmation record
    confirmation = TaskCompletionConfirmation(
        task_id=task_id,
        project_id=project_id,
        developer_id=developer_id,
        status=ConfirmationStatus.PENDING,
    )
    doc = confirmation.model_dump(by_alias=True, exclude={"id"})
    result = await db.task_completion_confirmations.insert_one(doc)
    confirmation_id = str(result.inserted_id)

    # Send notification to developer
    await send_notification(
        user_id=developer_id,
        type_="ai_task_confirmation",
        title="Task Completion Check",
        message=f"Have you completed your task '{task.get('title')}'? Do you want to move to the next task?",
        entity_type="task_completion_confirmation",
        entity_id=confirmation_id,
        link="/dashboard/ai/task-monitor",
    )

    return {"confirmation_id": confirmation_id, "status": "pending"}


# ── 2. Developer responds Yes / No / Reject ───────────────────────────────────
@router.post("/ai/task-completion/respond")
async def respond_to_task_completion(
    payload: dict,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Developer responds to a task completion confirmation.
    response: "yes" | "no" | "reject"
    remark: optional reason text
    """
    db = get_database()
    confirmation_id = payload.get("confirmation_id")
    response = (payload.get("response") or "").lower().strip()
    remark = payload.get("remark")

    if response not in ("yes", "no", "reject"):
        raise HTTPException(status_code=400, detail="response must be yes, no, or reject")

    confirmation = await db.task_completion_confirmations.find_one(
        {"_id": ObjectId(confirmation_id)}
    )
    if not confirmation:
        raise HTTPException(status_code=404, detail="Confirmation not found")

    task = await db.tasks.find_one({"_id": ObjectId(confirmation["task_id"])})
    project_id = confirmation["project_id"]
    developer_id = confirmation["developer_id"]

    # Verify that the developer is still the assignee of the task (H47)
    if not task or task.get("assignee_id") != developer_id:
        raise HTTPException(
            status_code=400,
            detail="Stale confirmation: developer is no longer the assignee of this task."
        )

    # Ensure only the assigned developer OR admin/team_lead can respond (HR restricted - W213)
    role = current_user.get("role", "").lower()
    if developer_id != current_user["id"] and role not in ("admin", "team_lead"):
        raise HTTPException(
            status_code=403, 
            detail="Only the assigned developer, admin, or team lead can respond to task completions."
        )

    now = datetime.now(timezone.utc)

    if response == "yes":
        # Pick the next task first (W215)
        next_task = await pick_next_task(project_id, developer_id, exclude_task_id=confirmation["task_id"])
        
        # Verify next_task still exists in the database (W214)
        verified_next_task = None
        if next_task:
            verified_next_task = await db.tasks.find_one({"_id": next_task["_id"]})
            if not verified_next_task:
                logger.warning(f"Next task {next_task['_id']} was selected by AI but does not exist in the database. Finding another task.")
                next_task = None

        # Update the task first if valid
        if next_task and verified_next_task:
            await db.tasks.update_one(
                {"_id": next_task["_id"]},
                {
                    "$set": {
                        "assignee_id": developer_id,
                        "status": "todo",
                        "updated_at": now,
                    }
                },
            )

        # Finally, update the confirmation status in the database (W215)
        update_doc = {"status": "confirmed", "confirmed_at": now}
        if next_task and verified_next_task:
            update_doc["new_task_id"] = str(next_task["_id"])

        await db.task_completion_confirmations.update_one(
            {"_id": ObjectId(confirmation_id)},
            {"$set": update_doc},
        )

        if next_task and verified_next_task:
            # Notify developer
            await send_notification(
                user_id=developer_id,
                type_="ai_task_assigned",
                title="New Task Assigned",
                message=f"Your next task is: {next_task.get('title')}",
                entity_type="task",
                entity_id=str(next_task["_id"]),
                link=f"/dashboard/projects/{project_id}/kanban",
            )
            return {
                "status": "confirmed",
                "new_task_id": str(next_task["_id"]),
                "new_task_title": next_task.get("title"),
            }
        else:
            await send_notification(
                user_id=developer_id,
                type_="ai_task_assigned",
                title="No More Tasks",
                message="You have completed all available tasks in this project. Great work!",
                entity_type="task",
                entity_id=confirmation["task_id"],
                link=f"/dashboard/projects/{project_id}/kanban",
            )
            return {"status": "confirmed", "new_task_id": None}

    elif response == "no":
        if not remark or not remark.strip():
            raise HTTPException(status_code=400, detail="Please provide a reason")

        # Validate reason with AI
        is_valid, evaluation = await validate_completion_reason(task, remark)

        await db.task_completion_confirmations.update_one(
            {"_id": ObjectId(confirmation_id)},
            {
                "$set": {
                    "status": "denied",
                    "developer_remark": remark,
                    "ai_evaluation": evaluation,
                    "confirmed_at": now,
                }
            },
        )

        if is_valid:
            # Valid reason — notify developer, give more time
            await send_notification(
                user_id=developer_id,
                type_="ai_task_confirmation",
                title="Reason Accepted",
                message=f"Your reason has been accepted. Take the time you need. Evaluation: {evaluation}",
                entity_type="task_completion_confirmation",
                entity_id=confirmation_id,
                link="/dashboard/ai/task-monitor",
            )
            return {"status": "denied", "valid": True, "evaluation": evaluation}
        else:
            # Invalid reason — alert admin + team lead, schedule force assign after 15 min
            # Pass confirmation_id to prevent NameError
            await _alert_admin_team_lead(confirmation_id, project_id, developer_id, task, remark, evaluation)
            # Schedule force assign
            schedule_force_assign(
                None,  # scheduler will be resolved internally
                confirmation_id,
                project_id,
                developer_id,
                confirmation["task_id"],
            )
            await send_notification(
                user_id=developer_id,
                type_="ai_task_confirmation",
                title="Reason Under Review",
                message=f"Your reason was not accepted. The admin and team lead have been notified. The task will be reassigned shortly. Evaluation: {evaluation}",
                entity_type="task_completion_confirmation",
                entity_id=confirmation_id,
                link="/dashboard/ai/task-monitor",
            )
            return {"status": "denied", "valid": False, "evaluation": evaluation}

    elif response == "reject":
        if not remark or not remark.strip():
            raise HTTPException(status_code=400, detail="Please provide a remark for rejecting the task")

        await db.task_completion_confirmations.update_one(
            {"_id": ObjectId(confirmation_id)},
            {
                "$set": {
                    "status": "rejected",
                    "developer_remark": remark,
                    "confirmed_at": now,
                }
            },
        )

        # Alert admin + team lead (pass confirmation_id)
        await _alert_admin_team_lead(
            confirmation_id, project_id, developer_id, task, remark, "Developer rejected the task."
        )

        # Schedule force assign after 15 minutes
        schedule_force_assign(
            None,
            confirmation_id,
            project_id,
            developer_id,
            confirmation["task_id"],
        )

        await send_notification(
            user_id=developer_id,
            type_="ai_task_confirmation",
            title="Task Rejected",
            message="Your rejection has been recorded. The admin and team lead have been notified. The task will be forcefully assigned to you after 15 minutes.",
            entity_type="task_completion_confirmation",
            entity_id=confirmation_id,
            link="/dashboard/ai/task-monitor",
        )
        return {"status": "rejected", "evaluation": "Task rejection recorded. Admin notified."}

    raise HTTPException(status_code=400, detail="Invalid response")


def _convert_ids(item: dict) -> dict:
    """Convert ObjectId fields to strings in a single document."""
    for key in ("_id", "developer_id", "project_id", "task_id", "new_task_id"):
        if key in item and not isinstance(item[key], str):
            item[key] = str(item[key])
    return item


# ── 3. List pending confirmations (BEFORE catch-all route) ────────────────────
@router.get("/ai/task-completion/pending")
async def list_pending_confirmations(
    current_user: dict = Depends(get_current_active_user),
):
    db = get_database()
    cursor = db.task_completion_confirmations.find(
        {"developer_id": current_user["id"], "status": "pending"}
    ).sort("created_at", -1)
    items = await cursor.to_list(length=50)
    return [_convert_ids(item) for item in items]


# ── 4. List confirmation history (BEFORE catch-all route) ─────────────────────
@router.get("/ai/task-completion/history")
async def list_confirmation_history(
    current_user: dict = Depends(get_current_active_user),
    limit: int = 50,
):
    db = get_database()
    cursor = db.task_completion_confirmations.find(
        {"developer_id": current_user["id"]}
    ).sort("created_at", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    return [_convert_ids(item) for item in items]


# ── 5. Admin: list ALL pending confirmations ──────────────────────────────────
@router.get("/ai/task-completion/admin/pending")
async def admin_list_pending_confirmations(
    current_user: dict = Depends(get_current_active_user),
):
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead", "hr"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    db = get_database()
    cursor = db.task_completion_confirmations.find(
        {"status": "pending"}
    ).sort("created_at", -1)
    items = await cursor.to_list(length=100)
    enriched = []
    for item in items:
        dev = await db.users.find_one({"_id": ObjectId(item["developer_id"])})
        item["developer_name"] = dev.get("name", "Unknown") if dev else "Unknown"
        enriched.append(_convert_ids(item))
    return enriched


# ── 6. Admin: list ALL confirmation history with pagination ──────────────────
@router.get("/ai/task-completion/admin/history")
async def admin_list_confirmation_history(
    current_user: dict = Depends(get_current_active_user),
    skip: int = 0,
    limit: int = 50,
):
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead", "hr"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    db = get_database()
    total = await db.task_completion_confirmations.count_documents({})
    cursor = db.task_completion_confirmations.find({}).sort("created_at", -1).skip(skip).limit(limit)
    items = await cursor.to_list(length=limit)
    enriched = []
    for item in items:
        dev = await db.users.find_one({"_id": ObjectId(item["developer_id"])})
        item["developer_name"] = dev.get("name", "Unknown") if dev else "Unknown"
        enriched.append(_convert_ids(item))
    return {"items": enriched, "total": total}


# ── 7. Get single confirmation (LAST — catch-all) ─────────────────────────────
@router.get("/ai/task-completion/{confirmation_id}")
async def get_confirmation(
    confirmation_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = get_database()
    confirmation = await db.task_completion_confirmations.find_one(
        {"_id": ObjectId(confirmation_id)}
    )
    if not confirmation:
        raise HTTPException(status_code=404, detail="Confirmation not found")
    return _convert_ids(confirmation)


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _alert_admin_team_lead(
    confirmation_id: str, project_id: str, developer_id: str, task: dict, remark: str, evaluation: str
):
    """Send AI_ADMIN_ALERT notifications to admin and team lead."""
    db = get_database()
    developer = await db.users.find_one({"_id": ObjectId(developer_id)})
    dev_name = developer.get("name") or developer.get("email", "Unknown")
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    project_name = project.get("name", "Unknown Project") if project else "Unknown Project"

    title = "AI Alert: Developer Task Issue"
    message = (
        f"Developer {dev_name} on project '{project_name}' "
        f"task '{task.get('title')}' reported: '{remark}'. "
        f"AI Evaluation: {evaluation}"
    )

    # Find admin and team lead
    admin_users = await db.users.find({"role": {"$in": ["admin", "team_lead"]}}).to_list(length=10)
    for admin in admin_users:
        admin_id = str(admin["_id"])
        await send_notification(
            user_id=admin_id,
            type_="ai_admin_alert",
            title=title,
            message=message,
            entity_type="task_completion_confirmation",
            entity_id=confirmation_id,
            link="/dashboard/ai/admin-alerts",
        )


async def trigger_ai_task_confirmation(task_id: str):
    """
    Fire-and-forget helper called from tasks.py when a task is marked done.
    This creates the confirmation record and sends the notification.
    """
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
        
    # Check if developer belongs to the project team (W212)
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project or developer_id not in project.get("team", []):
        logger.warning("Assignee %s is not in project %s team, skipping AI confirmation", developer_id, project_id)
        return

    # Check if a confirmation already exists for this task
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
