from fastapi import APIRouter, HTTPException, Depends
from typing import List, Optional
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from models.sprint import SprintCreate, SprintUpdate
from dependencies import get_current_active_user, validate_object_id
from helpers.notification_sender import send_notification
import logging
import asyncio
from collections import defaultdict

logger = logging.getLogger(__name__)

# Lock dictionary per sprint_id to prevent concurrent update races (W220)
_sprint_locks = defaultdict(asyncio.Lock)


router = APIRouter(prefix="/sprints", tags=["Sprints"])


def _get_db():
    return get_database()


def _normalize_datetime(dt):
    if not dt:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _check_sprint_access(sprint: dict, current_user: dict, db) -> None:
    role = current_user.get("role", "").lower()
    project = await db.projects.find_one({"_id": validate_object_id(sprint["project_id"])})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if role not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")



@router.post("", response_model=dict)
async def create_sprint(
    sprint: SprintCreate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()

    start_dt = _normalize_datetime(sprint.start_date)
    end_dt = _normalize_datetime(sprint.end_date)
    if start_dt and end_dt and end_dt < start_dt:
        raise HTTPException(status_code=400, detail="Sprint end date cannot be before start date")

    project = await db.projects.find_one({"_id": validate_object_id(sprint.project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if current_user.get("role", "").lower() != "admin" and current_user[
        "id"
    ] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    now_utc = datetime.now(timezone.utc)
    sprint_doc = {
        "name": sprint.name,
        "goal": sprint.goal,
        "description": sprint.description or "",
        "priority": sprint.priority or "medium",
        "team_lead_id": sprint.team_lead_id,
        "team_member_ids": sprint.team_member_ids or [],
        "estimated_hours": sprint.estimated_hours or 0.0,
        "tags": sprint.tags or [],
        "attachments": sprint.attachments or [],
        "project_id": sprint.project_id,
        "start_date": sprint.start_date,
        "end_date": sprint.end_date,
        "status": sprint.status.value,
        "status_history": [
            {
                "status": sprint.status.value,
                "changed_by": current_user["id"],
                "changed_at": now_utc,
            }
        ],
        "created_by": current_user["id"],
        "created_at": now_utc,
        "updated_at": now_utc,
    }
    result = await db.sprints.insert_one(sprint_doc)
    sprint_id = str(result.inserted_id)

    # Activity Log
    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "sprint_created",
            "entity_type": "sprint",
            "entity_id": sprint_id,
            "metadata": {"name": sprint.name, "project_id": sprint.project_id},
            "created_at": now_utc,
        }
    )

    # Notifications
    # 1. Notify Team Lead
    if sprint.team_lead_id:
        lead_user = await db.users.find_one({"_id": validate_object_id(sprint.team_lead_id)}, {"name": 1})
        lead_name = lead_user.get("name", "Unknown") if lead_user else "Unknown"
        
        await send_notification(
            user_id=sprint.team_lead_id,
            type_="sprint_assigned",
            title="Assigned as Sprint Team Lead",
            message=(
                f"You have been assigned as Team Lead for Sprint {sprint.name}\n\n"
                f"Project:\n{project.get('name')}\n\n"
                f"Assigned By:\n{current_user.get('name', 'Admin')}"
            ),
            entity_type="sprint",
            entity_id=sprint_id,
            link=f"/dashboard/sprints/{sprint_id}",
        )

        # Log Team Lead assignment in activity logs
        await db.activity_logs.insert_one(
            {
                "user_id": current_user["id"],
                "action": "sprint_lead_assigned",
                "entity_type": "sprint",
                "entity_id": sprint_id,
                "metadata": {"name": sprint.name, "team_lead_name": lead_name, "project_id": sprint.project_id},
                "created_at": now_utc,
            }
        )

    # 2. Notify Team Members
    for member_id in (sprint.team_member_ids or []):
        if member_id != sprint.team_lead_id:  # Avoid duplicate notification if team lead is also in member list
            member_user = await db.users.find_one({"_id": validate_object_id(member_id)}, {"role": 1, "position": 1, "name": 1})
            member_role = "Developer"
            member_name = "Unknown"
            if member_user:
                member_name = member_user.get("name", "Unknown")
                member_role = member_user.get("position") or member_user.get("role", "Developer")
                member_role = member_role.replace("_", " ").title()
                
            await send_notification(
                user_id=member_id,
                type_="sprint_member_added",
                title="Added to Sprint",
                message=(
                    f"You have been assigned to Sprint {sprint.name}\n\n"
                    f"Role:\n{member_role}\n\n"
                    f"Project:\n{project.get('name')}"
                ),
                entity_type="sprint",
                entity_id=sprint_id,
                link=f"/dashboard/sprints/{sprint_id}",
            )

            # Log member assignment in activity logs
            await db.activity_logs.insert_one(
                {
                    "user_id": current_user["id"],
                    "action": "sprint_member_added",
                    "entity_type": "sprint",
                    "entity_id": sprint_id,
                    "metadata": {"name": sprint.name, "member_name": member_name, "project_id": sprint.project_id},
                    "created_at": now_utc,
                }
            )

    return {"id": sprint_id, "message": "Sprint created successfully"}



@router.get("")
async def list_sprints(
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    query = {}
    role = current_user.get("role", "").lower()

    if project_id:
        # Verify project exists and user has access
        project = await db.projects.find_one({"_id": ObjectId(project_id)})
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if role not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")
        query["project_id"] = project_id
    else:
        # No specific project: restrict to projects user can access
        if role in ["admin", "team_lead"]:
            # No project filter needed; can see all sprints
            pass
        else:
            user_projects = await db.projects.find({"team": current_user["id"]}, {"_id": 1}).to_list(length=None)
            project_ids = [str(p["_id"]) for p in user_projects]
            if not project_ids:
                return []
            query["project_id"] = {"$in": project_ids}

    if status:
        query["status"] = status

    cursor = db.sprints.find(query).sort("created_at", -1)
    sprints = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        
        # Calculate tasks stats and progress for the sprint
        sprint_id_str = doc["_id"]
        try:
            sprint_oid = ObjectId(sprint_id_str)
            task_query = {"$or": [{"sprint": sprint_id_str}, {"sprint": sprint_oid}], "parent_id": {"$in": [None, ""]}}
        except Exception:
            task_query = {"sprint": sprint_id_str}
            
        sprint_tasks = await db.tasks.find(task_query).to_list(length=None)
        total_tasks_count = len(sprint_tasks)
        completed_tasks_count = sum(1 for t in sprint_tasks if t.get("status") == "done")
        progress_pct = (completed_tasks_count / total_tasks_count * 100) if total_tasks_count > 0 else 0.0
        
        doc["total_tasks_count"] = total_tasks_count
        doc["completed_tasks_count"] = completed_tasks_count
        doc["progress_pct"] = round(progress_pct, 1)
        
        sprints.append(doc)
    return sprints


@router.get("/{sprint_id}")
async def get_sprint(
    sprint_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    sprint = await db.sprints.find_one({"_id": validate_object_id(sprint_id)})
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")

    await _check_sprint_access(sprint, current_user, db)
    project = await db.projects.find_one({"_id": validate_object_id(sprint["project_id"])})


    sprint["_id"] = str(sprint["_id"])
    
    # 1. Project Info Card enrichment
    manager_name = project.get("created_by_name", "Unknown")
    if not project.get("created_by_name") and project.get("created_by"):
        manager = await db.users.find_one({"_id": validate_object_id(project["created_by"])}, {"name": 1})
        if manager:
            manager_name = manager.get("name", "Unknown")

    # Fetch total sprints count for this project
    project_sprint_count = await db.sprints.count_documents({"project_id": sprint["project_id"]})

    # Fetch all tasks in the project to calculate project progress %
    proj_tasks = await db.tasks.find({"project_id": sprint["project_id"]}, {"status": 1}).to_list(length=None)
    proj_total_tasks = len(proj_tasks)
    proj_completed_tasks = sum(1 for t in proj_tasks if t.get("status") == "done")
    proj_progress_pct = (proj_completed_tasks / proj_total_tasks * 100) if proj_total_tasks > 0 else 0.0

    sprint["project_details"] = {
        "id": str(project["_id"]),
        "name": project.get("name"),
        "description": project.get("description", ""),
        "manager_id": project.get("created_by"),
        "manager_name": manager_name,
        "team_size": len(project.get("team", [])),
        "sprint_count": project_sprint_count,
        "progress_pct": round(proj_progress_pct, 1),
    }

    # 2. Tasks stats and progress for the sprint
    try:
        sprint_oid = ObjectId(sprint_id)
        task_query = {"$or": [{"sprint": sprint_id}, {"sprint": sprint_oid}], "parent_id": {"$in": [None, ""]}}
    except Exception:
        task_query = {"sprint": sprint_id, "parent_id": {"$in": [None, ""]}}
    sprint_tasks = await db.tasks.find(task_query).to_list(length=None)
    total_tasks_count = len(sprint_tasks)
    completed_tasks_count = sum(1 for t in sprint_tasks if t.get("status") == "done")
    pending_tasks_count = total_tasks_count - completed_tasks_count
    progress_pct = (completed_tasks_count / total_tasks_count * 100) if total_tasks_count > 0 else 0.0
    
    overdue_tasks_count = 0
    now_utc = datetime.now(timezone.utc)
    for t in sprint_tasks:
        if t.get("status") != "done" and t.get("due_date"):
            try:
                due_dt = t.get("due_date")
                if isinstance(due_dt, str):
                    due_dt = datetime.fromisoformat(due_dt.replace("Z", "+00:00"))
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=timezone.utc)
                if due_dt < now_utc:
                    overdue_tasks_count += 1
            except Exception:
                pass

    total_estimated_hours = sum(t.get("estimated_hours") or 0.0 for t in sprint_tasks)
    total_consumed_hours = sum(t.get("time_spent") or 0.0 for t in sprint_tasks)
    total_remaining_hours = 0.0
    for t in sprint_tasks:
        rem = t.get("remaining_hours")
        if rem is not None:
            total_remaining_hours += rem
        else:
            est = t.get("estimated_hours") or 0.0
            spent = t.get("time_spent") or 0.0
            total_remaining_hours += max(0.0, est - spent)

    sprint["total_tasks_count"] = total_tasks_count
    sprint["completed_tasks_count"] = completed_tasks_count
    sprint["pending_tasks_count"] = pending_tasks_count
    sprint["overdue_tasks_count"] = overdue_tasks_count
    sprint["progress_pct"] = round(progress_pct, 1)
    sprint["stats"] = {
        "total_tasks": total_tasks_count,
        "completed_tasks": completed_tasks_count,
        "pending_tasks": pending_tasks_count,
        "overdue_tasks": overdue_tasks_count,
        "completion_pct": round(progress_pct, 1),
        "estimated_hours": round(total_estimated_hours, 1),
        "consumed_hours": round(total_consumed_hours, 1),
        "remaining_hours": round(total_remaining_hours, 1)
    }

    # 3. Fetch Team Lead Profile
    team_lead = None
    if sprint.get("team_lead_id"):
        lead_user = await db.users.find_one(
            {"_id": validate_object_id(sprint["team_lead_id"])},
            {"name": 1, "role": 1, "position": 1, "avatar": 1}
        )
        if lead_user:
            lead_user["_id"] = str(lead_user["_id"])
            team_lead = lead_user
    sprint["team_lead"] = team_lead

    # 4. Fetch Team Members Profiles and calculate individual progress
    team_members = []
    member_ids = sprint.get("team_member_ids", [])
    if member_ids:
        # Convert list of string IDs to ObjectIds
        valid_oids = []
        for mid in member_ids:
            try:
                valid_oids.append(ObjectId(mid))
            except Exception:
                continue

        # Fetch users
        users_cursor = db.users.find(
            {"_id": {"$in": valid_oids}},
            {"name": 1, "role": 1, "position": 1, "avatar": 1, "last_seen": 1}
        )
        async for u in users_cursor:
            u["_id"] = str(u["_id"])
            
            # Count tasks for this user in this sprint
            user_sprint_tasks = [t for t in sprint_tasks if t.get("assignee_id") == u["_id"]]
            user_total_tasks = len(user_sprint_tasks)
            user_completed_tasks = sum(1 for t in user_sprint_tasks if t.get("status") == "done")
            user_progress_pct = (user_completed_tasks / user_total_tasks * 100) if user_total_tasks > 0 else 0.0
            
            # Determine online status based on last_seen (e.g. within 5 minutes)
            is_online = False
            last_seen = u.get("last_seen")
            if last_seen:
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                if (now_utc - last_seen).total_seconds() < 300:  # 5 minutes
                    is_online = True

            u["sprint_tasks_count"] = user_total_tasks
            u["sprint_completed_tasks"] = user_completed_tasks
            u["sprint_progress_pct"] = round(user_progress_pct, 1)
            u["is_online"] = is_online
            team_members.append(u)

    sprint["team_members"] = team_members
    return sprint



@router.put("/{sprint_id}")
async def update_sprint(
    sprint_id: str,
    sprint_update: SprintUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    # Acquire lock per sprint to prevent concurrent update races (W220)
    async with _sprint_locks[sprint_id]:
        return await _update_sprint_impl(sprint_id, sprint_update, current_user)

async def _update_sprint_impl(
    sprint_id: str,
    sprint_update: SprintUpdate,
    current_user: dict,
):
    db = _get_db()
    existing = await db.sprints.find_one({"_id": validate_object_id(sprint_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Sprint not found")

    role = current_user.get("role", "").lower()
    project = await db.projects.find_one({"_id": validate_object_id(existing["project_id"])})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found") # W222
    is_project_member = current_user["id"] in project.get("team", [])
    
    if (
        existing["created_by"] != current_user["id"]
        and role != "admin"
        and not (role == "team_lead" and is_project_member)
    ):
        raise HTTPException(
            status_code=403, detail="Only creator, admin, or project team leads can update"
        )

    update_data = {k: v for k, v in sprint_update.model_dump().items() if v is not None}

    # Validate dates if updated
    start_date = update_data.get("start_date") if "start_date" in update_data else existing.get("start_date")
    end_date = update_data.get("end_date") if "end_date" in update_data else existing.get("end_date")
    start_dt = _normalize_datetime(start_date)
    end_dt = _normalize_datetime(end_date)
    if start_dt and end_dt and end_dt < start_dt:
        raise HTTPException(status_code=400, detail="Sprint end date cannot be before start date")
    
    # Handle status history and changes
    now_utc = datetime.now(timezone.utc)
    status_changed = False
    new_status = None
    if "status" in update_data and update_data["status"]:
        new_status = update_data["status"].value if hasattr(update_data["status"], "value") else str(update_data["status"])
        update_data["status"] = new_status
        current_status = existing.get("status")
        if new_status != current_status:
            # Validate transition
            if current_status == "planning" and new_status not in ["active", "cancelled"]:
                raise HTTPException(status_code=400, detail=f"Sprint in planning status can only transition to active or cancelled, not {new_status}")
            elif current_status in ["active", "in_progress", "testing"] and new_status not in ["completed", "cancelled"]:
                raise HTTPException(status_code=400, detail=f"Active sprint can only transition to completed or cancelled, not {new_status}")
            elif current_status in ["completed", "cancelled"]:
                raise HTTPException(status_code=400, detail=f"Cannot transition sprint from a final status ({current_status})")
            status_changed = True


    if status_changed and new_status:
        hist = existing.get("status_history", [])
        hist.append({
            "status": new_status,
            "changed_by": current_user["id"],
            "changed_at": now_utc
        })
        update_data["status_history"] = hist

    update_data["updated_at"] = now_utc

    await db.sprints.update_one({"_id": validate_object_id(sprint_id)}, {"$set": update_data})

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "sprint_updated",
            "entity_type": "sprint",
            "entity_id": sprint_id,
            "metadata": {"name": existing.get("name")},
            "created_at": now_utc,
        }
    )

    sprint_name = update_data.get("name", existing.get("name"))
    
    # 1. Status change notifications
    if status_changed and new_status:
        recipients = set(update_data.get("team_member_ids", existing.get("team_member_ids", [])))
        if update_data.get("team_lead_id", existing.get("team_lead_id")):
            recipients.add(update_data.get("team_lead_id", existing.get("team_lead_id")))
        
        notif_type = "sprint_status_changed"
        title = "Sprint Status Changed"
        message = f"Sprint '{sprint_name}' has changed status to: {new_status.replace('_', ' ').title()}"
        
        if new_status == "completed":
            notif_type = "sprint_completed"
            title = "Sprint Completed"
            message = f"Sprint '{sprint_name}' has been marked as Completed!"
            
            # Log sprint completion in activity logs
            await db.activity_logs.insert_one(
                {
                    "user_id": current_user["id"],
                    "action": "sprint_completed",
                    "entity_type": "sprint",
                    "entity_id": sprint_id,
                    "metadata": {"name": sprint_name, "project_id": existing["project_id"]},
                    "created_at": now_utc,
                }
            )
        
        for r_id in recipients:
            if r_id != current_user["id"]:
                await send_notification(
                    user_id=r_id,
                    type_=notif_type,
                    title=title,
                    message=message,
                    entity_type="sprint",
                    entity_id=sprint_id,
                    link=f"/dashboard/sprints/{sprint_id}",
                )

    # 2. Team Lead Change Notification
    old_lead = existing.get("team_lead_id")
    new_lead = update_data.get("team_lead_id")
    if new_lead and new_lead != old_lead:
        lead_user = await db.users.find_one({"_id": validate_object_id(new_lead)}, {"name": 1})
        lead_name = lead_user.get("name", "Unknown") if lead_user else "Unknown"
        
        await send_notification(
            user_id=new_lead,
            type_="sprint_assigned",
            title="Assigned as Sprint Team Lead",
            message=(
                f"You have been assigned as Team Lead for Sprint {sprint_name}\n\n"
                f"Project:\n{project.get('name')}\n\n"
                f"Assigned By:\n{current_user.get('name', 'Admin')}"
            ),
            entity_type="sprint",
            entity_id=sprint_id,
            link=f"/dashboard/sprints/{sprint_id}",
        )

        # Log Team Lead assignment in activity logs
        await db.activity_logs.insert_one(
            {
                "user_id": current_user["id"],
                "action": "sprint_lead_assigned",
                "entity_type": "sprint",
                "entity_id": sprint_id,
                "metadata": {"name": sprint_name, "team_lead_name": lead_name, "project_id": existing["project_id"]},
                "created_at": now_utc,
            }
        )

    # 3. Team Member additions/removals
    old_members = set(existing.get("team_member_ids", []))
    new_members = set(update_data.get("team_member_ids", list(old_members)))
    
    added_members = new_members - old_members
    removed_members = old_members - new_members

    for a_id in added_members:
        if a_id != current_user["id"]:
            member_user = await db.users.find_one({"_id": validate_object_id(a_id)}, {"role": 1, "position": 1, "name": 1})
            member_role = "Developer"
            member_name = "Unknown"
            if member_user:
                member_name = member_user.get("name", "Unknown")
                member_role = member_user.get("position") or member_user.get("role", "Developer")
                member_role = member_role.replace("_", " ").title()

            await send_notification(
                user_id=a_id,
                type_="sprint_member_added",
                title="Added to Sprint",
                message=(
                    f"You have been assigned to Sprint {sprint_name}\n\n"
                    f"Role:\n{member_role}\n\n"
                    f"Project:\n{project.get('name')}"
                ),
                entity_type="sprint",
                entity_id=sprint_id,
                link=f"/dashboard/sprints/{sprint_id}",
            )

            # Log member assignment
            await db.activity_logs.insert_one(
                {
                    "user_id": current_user["id"],
                    "action": "sprint_member_added",
                    "entity_type": "sprint",
                    "entity_id": sprint_id,
                    "metadata": {"name": sprint_name, "member_name": member_name, "project_id": existing["project_id"]},
                    "created_at": now_utc,
                }
            )
            
    for r_id in removed_members:
        if r_id != current_user["id"]:
            member_user = await db.users.find_one({"_id": validate_object_id(r_id)}, {"name": 1})
            member_name = member_user.get("name", "Unknown") if member_user else "Unknown"

            await send_notification(
                user_id=r_id,
                type_="sprint_member_removed",
                title="Removed from Sprint",
                message=f"You have been removed from Sprint: {sprint_name}",
                entity_type="sprint",
                entity_id=sprint_id,
                link=f"/dashboard/sprints/{sprint_id}",
            )

            # Log member removal
            await db.activity_logs.insert_one(
                {
                    "user_id": current_user["id"],
                    "action": "sprint_member_removed",
                    "entity_type": "sprint",
                    "entity_id": sprint_id,
                    "metadata": {"name": sprint_name, "member_name": member_name, "project_id": existing["project_id"]},
                    "created_at": now_utc,
                }
            )

    return {"message": "Sprint updated successfully"}


@router.delete("/{sprint_id}")
async def delete_sprint(
    sprint_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    existing = await db.sprints.find_one({"_id": validate_object_id(sprint_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Sprint not found")

    if (
        existing["created_by"] != current_user["id"]
        and current_user.get("role", "").lower() != "admin"
    ):
        raise HTTPException(
            status_code=403, detail="Only sprint creator or admin can delete"
        )

    # 1. Update tasks assigned to the deleted sprint to have sprint = None
    try:
        sprint_oid = ObjectId(sprint_id)
        task_query = {"$or": [{"sprint": sprint_id}, {"sprint": sprint_oid}]}
    except Exception:
        task_query = {"sprint": sprint_id}
    await db.tasks.update_many(task_query, {"$set": {"sprint": None, "updated_at": datetime.now(timezone.utc)}})

    # 2. Delete all notifications related to the sprint
    await db.notifications.delete_many({"entity_type": "sprint", "entity_id": sprint_id})

    # 3. Delete all sprint comments
    await db.comments.delete_many({"entity_type": "sprint", "entity_id": sprint_id})

    # 4. Delete files from Google Drive and database
    from helpers.backblaze import delete_from_drive
    for att in existing.get("attachments", []):
        drive_file_id = att.get("drive_file_id")
        if drive_file_id:
            try:
                delete_from_drive(drive_file_id)
            except Exception as e:
                logger.warning(f"Failed to delete Drive file {drive_file_id} for sprint {sprint_id}: {e}")

    # 5. Delete activity logs for the sprint
    await db.activity_logs.delete_many({"entity_type": "sprint", "entity_id": sprint_id})

    # 6. Delete the sprint itself
    await db.sprints.delete_one({"_id": validate_object_id(sprint_id)})
    return {"message": "Sprint deleted successfully"}


@router.post("/{sprint_id}/start")
async def start_sprint(
    sprint_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    existing = await db.sprints.find_one({"_id": validate_object_id(sprint_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Sprint not found")

    if existing["status"] != "planning":
        raise HTTPException(
            status_code=400, detail="Sprint must be in planning status to start"
        )

    now_utc = datetime.now(timezone.utc)
    hist = existing.get("status_history", [])
    hist.append({
        "status": "active",
        "changed_by": current_user["id"],
        "changed_at": now_utc
    })

    await db.sprints.update_one(
        {"_id": validate_object_id(sprint_id)},
        {"$set": {"status": "active", "status_history": hist, "updated_at": now_utc}},
    )

    # Activity Log
    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "sprint_started",
            "entity_type": "sprint",
            "entity_id": sprint_id,
            "metadata": {"name": existing.get("name"), "project_id": existing["project_id"]},
            "created_at": now_utc,
        }
    )

    # Remove hardcoded "TBD" placeholders (W223)
    start_date_str = None
    if existing.get("start_date"):
        start_val = existing.get("start_date")
        if isinstance(start_val, str):
            start_date_str = start_val
        elif isinstance(start_val, datetime):
            start_date_str = start_val.isoformat()

    end_date_str = None
    if existing.get("end_date"):
        end_val = existing.get("end_date")
        if isinstance(end_val, str):
            end_date_str = end_val
        elif isinstance(end_val, datetime):
            end_date_str = end_val.isoformat()

    recipients = set(existing.get("team_member_ids", []))
    if existing.get("team_lead_id"):
        recipients.add(existing["team_lead_id"])

    for r_id in recipients:
        if r_id != current_user["id"]:
            await send_notification(
                user_id=r_id,
                type_="sprint_started",
                title="Sprint Started",
                message=(
                    f"Sprint {existing.get('name')} has started.\n\n"
                    f"Start Date:\n{start_date_str or 'Not specified'}\n\n"
                    f"End Date:\n{end_date_str or 'Not specified'}"
                ),
                entity_type="sprint",
                entity_id=sprint_id,
                link=f"/dashboard/sprints/{sprint_id}",
            )

    return {"message": "Sprint started"}


@router.post("/{sprint_id}/complete")
async def complete_sprint(
    sprint_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    existing = await db.sprints.find_one({"_id": validate_object_id(sprint_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Sprint not found")

    if existing["status"] not in ["active", "in_progress", "testing"]:
        raise HTTPException(status_code=400, detail="Sprint must be active to complete")

    now_utc = datetime.now(timezone.utc)
    hist = existing.get("status_history", [])
    hist.append({
        "status": "completed",
        "changed_by": current_user["id"],
        "changed_at": now_utc
    })

    await db.sprints.update_one(
        {"_id": validate_object_id(sprint_id)},
        {"$set": {"status": "completed", "status_history": hist, "updated_at": now_utc}},
    )

    # Activity Log
    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "sprint_completed",
            "entity_type": "sprint",
            "entity_id": sprint_id,
            "metadata": {"name": existing.get("name"), "project_id": existing["project_id"]},
            "created_at": now_utc,
        }
    )

    recipients = set(existing.get("team_member_ids", []))
    if existing.get("team_lead_id"):
        recipients.add(existing["team_lead_id"])

    for r_id in recipients:
        if r_id != current_user["id"]:
            await send_notification(
                user_id=r_id,
                type_="sprint_completed",
                title="Sprint Completed",
                message=f"Sprint '{existing.get('name')}' has been marked as Completed!",
                entity_type="sprint",
                entity_id=sprint_id,
                link=f"/dashboard/sprints/{sprint_id}",
            )

    return {"message": "Sprint completed"}


@router.get("/{sprint_id}/tasks")
async def get_sprint_tasks(
    sprint_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    sprint = await db.sprints.find_one({"_id": validate_object_id(sprint_id)})
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
    
    # Permission check: verify user can access the sprint's project
    await _check_sprint_access(sprint, current_user, db)

    try:
        sprint_oid = ObjectId(sprint_id)
        task_query = {"$or": [{"sprint": sprint_id}, {"sprint": sprint_oid}]}
    except Exception:
        task_query = {"sprint": sprint_id}
    cursor = db.tasks.find(task_query).sort("order", 1)
    tasks = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        tasks.append(doc)
    return tasks


@router.get("/{sprint_id}/burndown")
async def get_sprint_burndown(
    sprint_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    sprint = await db.sprints.find_one({"_id": validate_object_id(sprint_id)})
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
    
    # Permission check: verify user can access the sprint's project
    await _check_sprint_access(sprint, current_user, db)

    try:
        sprint_oid = ObjectId(sprint_id)
        task_query = {"$or": [{"sprint": sprint_id}, {"sprint": sprint_oid}]}
    except Exception:
        task_query = {"sprint": sprint_id}
    tasks_cursor = db.tasks.find(task_query)
    tasks = []
    async for doc in tasks_cursor:
        doc["_id"] = str(doc["_id"])
        tasks.append(doc)

    def task_metric(t):
        return (
            t.get("story_points")
            if t.get("story_points") is not None
            else (t.get("estimated_hours") or 0)
        )

    total_estimated = sum(task_metric(t) for t in tasks)
    total_spent = sum(t.get("time_spent") or 0 for t in tasks)
    total_remaining = sum(t.get("remaining_hours") or task_metric(t) for t in tasks)

    completed_estimated = sum(
        task_metric(t) for t in tasks if t.get("status") == "done"
    )
    in_progress_estimated = sum(
        task_metric(t)
        for t in tasks
        if t.get("status") in ("in_progress", "code_review", "testing")
    )

    activity_cursor = db.activity_logs.find(
        {
            "entity_type": "task",
            "entity_id": {"$in": [t["_id"] for t in tasks]},
            "action": {"$in": ["status_changed", "time_logged"]},
        }
    ).sort("created_at", 1)

    daily_completed = {}
    async for log in activity_cursor:
        date_key = log["created_at"].strftime("%Y-%m-%d")
        if log["action"] == "status_changed":
            changes = log.get("changes", {})
            if "status" in changes and changes["status"].get("new") == "done":
                task = next((t for t in tasks if t["_id"] == log["entity_id"]), None)
                if task:
                    daily_completed[date_key] = daily_completed.get(
                        date_key, 0
                    ) + task_metric(task)
        elif log["action"] == "time_logged":
            hours_logged = log.get("metadata", {}).get("hours_added", 0)
            daily_completed[date_key] = daily_completed.get(date_key, 0) + hours_logged

    start_date = sprint.get("start_date")
    end_date = sprint.get("end_date")

    sprint_days = []
    if start_date and end_date:
        from datetime import timedelta

        current = (
            start_date
            if isinstance(start_date, datetime)
            else datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        )
        end = (
            end_date
            if isinstance(end_date, datetime)
            else datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        )
        while current <= end:
            date_key = current.strftime("%Y-%m-%d")
            start = (
                start_date
                if isinstance(start_date, datetime)
                else datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            )
            total_days = max(1, (end - start).days)
            elapsed = (current - start).days
            ideal_remaining = total_estimated - (total_estimated * elapsed / total_days)
            sprint_days.append(
                {
                    "date": date_key,
                    "ideal_remaining": round(ideal_remaining, 1),
                    "actual_completed": daily_completed.get(date_key, 0),
                }
            )
            current += timedelta(days=1)

    completed_count = sum(1 for t in tasks if t.get("status") == "done")
    total_count = len(tasks)
    uses_story_points = any(t.get("story_points") is not None for t in tasks)

    return {
        "sprint_id": sprint_id,
        "sprint_name": sprint.get("name"),
        "total_estimated_hours": total_estimated,
        "total_spent_hours": total_spent,
        "total_remaining_hours": total_remaining,
        "completed_hours": completed_estimated,
        "in_progress_hours": in_progress_estimated,
        "completed_tasks": completed_count,
        "total_tasks": total_count,
        "velocity": completed_estimated,
        "uses_story_points": uses_story_points,
        "burndown_data": sprint_days,
    }


@router.get("/{sprint_id}/activity")
async def get_sprint_activity(
    sprint_id: str,
    limit: int = 50,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    sprint = await db.sprints.find_one({"_id": validate_object_id(sprint_id)})
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
        
    await _check_sprint_access(sprint, current_user, db)

    cursor = db.activity_logs.find(
        {"entity_type": "sprint", "entity_id": sprint_id}
    ).sort("created_at", -1).limit(limit)
    
    raw_logs = []
    user_ids = set()
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        raw_logs.append(doc)
        if doc.get("user_id"):
            user_ids.add(doc["user_id"])
            
    # Enrich user names
    user_name_map = {}
    if user_ids:
        valid_oids = [ObjectId(uid) for uid in user_ids if ObjectId.is_valid(uid)]
        if valid_oids:
            async for u in db.users.find({"_id": {"$in": valid_oids}}, {"name": 1}):
                user_name_map[str(u["_id"])] = u.get("name", "Unknown")
                
    logs = []
    for doc in raw_logs:
        doc["user_name"] = user_name_map.get(doc.get("user_id", ""), "Unknown")
        logs.append(doc)
        
    return logs
