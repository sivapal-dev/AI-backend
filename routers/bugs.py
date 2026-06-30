from fastapi import APIRouter, HTTPException, status, Depends
import asyncio
import logging
from typing import List, Optional
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from models.bug import BugCreate, BugUpdate, BugStatus
from pydantic import BaseModel
from dependencies import get_current_active_user, validate_object_id
from helpers.backblaze import delete_from_drive
from helpers.notification_sender import send_notification, notify_role_watchers
from services.webhook_service import webhook_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bugs", tags=["Bugs"])


def _get_db():
    return get_database()


@router.post("", response_model=dict)
async def create_bug(
    bug: BugCreate, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    # Verify project exists and user has access (admin or team member)
    project = await db.projects.find_one({"_id": ObjectId(bug.project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead", "hr"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(
            status_code=403, detail="You are not a member of this project"
        )

    now = datetime.now(timezone.utc)
    assignee_name = None
    assignee_id = bug.assignee_id
    if assignee_id:
        assignee_user = await db.users.find_one({"_id": ObjectId(assignee_id)})
        if assignee_user:
            assignee_name = assignee_user.get("name")

    bug_doc = {
        "project_id": bug.project_id,
        "title": bug.title,
        "description": bug.description,
        "severity": bug.severity.value,
        "status": bug.status.value,
        "steps_to_reproduce": bug.steps_to_reproduce,
        "expected_result": bug.expected_result,
        "actual_result": bug.actual_result,
        "environment": bug.environment.model_dump(),
        "is_regression": bug.is_regression,
        "attachments": [],
        "reporter": current_user["id"],
        "assignee": assignee_name,
        "assignee_id": assignee_id,
        "related_task": bug.related_task,
        "custom_field_values": bug.custom_field_values or {},
        "created_at": now,
        "updated_at": now,
    }
    # Set started_at if initial status indicates work has started
    if bug.status.value in {'in_progress', 'fix_in_progress'}:
        bug_doc['started_at'] = now
    # Set resolved_at if initial status is a resolved/closed status
    if bug.status.value in {'resolved', 'verified', 'wont_fix', 'duplicate'}:
        bug_doc['resolved_at'] = now
    result = await db.bugs.insert_one(bug_doc)

    # Notify all project team members about the new bug (except reporter)
    team_members = project.get("team", [])
    for member_id in team_members:
        if member_id != current_user["id"]:
            await send_notification(
                user_id=member_id,
                type_="bug_reported",
                title="New Bug Reported",
                message=f"A new bug '{bug.title}' was reported in project '{project['name']}'",
                entity_type="bug",
                entity_id=str(result.inserted_id),
                link=f"/dashboard/bugs/{str(result.inserted_id)}",
            )

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "bug_created",
            "entity_type": "bug",
            "entity_id": str(result.inserted_id),
            "metadata": {"title": bug.title, "severity": bug.severity.value},
            "created_at": datetime.now(timezone.utc),
        }
    )


    return {"id": str(result.inserted_id), "message": "Bug reported successfully"}


@router.get("")
async def list_bugs(
    project_id: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
    all: bool = False,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    query = {}
    has_bypass = current_user.get("role", "").lower() in ["admin", "team_lead", "hr"]

    if project_id:
        project = await db.projects.find_one({"_id": validate_object_id(project_id)})
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if not has_bypass and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")
        query["project_id"] = project_id
    elif not has_bypass or not all:
        # If no project_id is specified and user is not admin requesting all,
        # only show bugs from projects they are part of.
        user_projects = await db.projects.find({"team": current_user["id"]}, {"_id": 1}).to_list(length=None)
        project_ids = [str(p["_id"]) for p in user_projects]
        if not project_ids:
            return []
        query["project_id"] = {"$in": project_ids}

    if severity:
        query["severity"] = severity
    if status:
        query["status"] = status

    cursor = db.bugs.find(query).sort("created_at", -1)
    bugs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if "reporter" in doc:
            doc["reporter"] = str(doc["reporter"])
        bugs.append(doc)
    return bugs


@router.get("/{bug_id}")
async def get_bug(bug_id: str, current_user: dict = Depends(get_current_active_user)):
    db = _get_db()
    bug = await db.bugs.find_one({"_id": validate_object_id(bug_id)})
    if not bug:
        raise HTTPException(status_code=404, detail="Bug not found")
    
    # Permission check: user must be admin/team_lead/hr or project team member
    project = await db.projects.find_one({"_id": ObjectId(bug["project_id"])})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead", "hr"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Not authorized to view this bug")
    
    bug["_id"] = str(bug["_id"])
    if "reporter" in bug:
        bug["reporter"] = str(bug["reporter"])
    return bug


async def handle_bug_status_change(
    db,
    bug_id: str,
    existing: dict,
    new_status: str,
    rollback_reason: Optional[str],
    current_user: dict,
    now: datetime
) -> dict:
    old_status = existing.get("status")
    if old_status == new_status:
        return {}

    status_order = {"open": 1, "in_progress": 2, "resolved": 3}
    is_rollback_transition = (
        old_status in status_order 
        and new_status in status_order 
        and status_order[new_status] < status_order[old_status]
    )

    status_history = existing.get("status_history") or []

    if is_rollback_transition:
        if not rollback_reason or not rollback_reason.strip():
            raise HTTPException(
                status_code=400,
                detail="Rollback reason is required."
            )
        
        reason_str = rollback_reason.strip()
        history_entry = {
            "status_from": old_status,
            "status_to": new_status,
            "reason": reason_str,
            "changed_by": current_user["id"],
            "changed_by_name": current_user.get("name", "Unknown"),
            "changed_at": now.isoformat()
        }
        status_history.append(history_entry)
        
        await db.activity_logs.insert_one({
            "user_id": current_user["id"],
            "action": "bug_status_changed",
            "entity_type": "bug",
            "entity_id": bug_id,
            "metadata": {
                "title": existing["title"],
                "old_status": old_status,
                "new_status": new_status,
                "rollback_reason": reason_str
            },
            "created_at": now
        })
        
        # Notify assigned developer and reporter
        assignee_id = existing.get("assignee_id")
        if not assignee_id:
            # Fallback: look up by name if ID not stored
            assignee_name = existing.get("assignee")
            if assignee_name:
                assignee_user = await db.users.find_one({"name": assignee_name})
                if assignee_user:
                    assignee_id = str(assignee_user["_id"])
        
        notif_recipients = {str(existing.get("reporter")), assignee_id}
        for uid in notif_recipients:
            if uid and uid != current_user["id"]:
                await send_notification(
                    user_id=uid,
                    type_="bug_status_changed",
                    title="Bug Status Rolled Back",
                    message=(
                        f"Bug '{existing['title']}' status rolled back from {old_status.replace('_', ' ').title()} "
                        f"to {new_status.replace('_', ' ').title()}.\n\n"
                        f"Reason:\n{reason_str}\n\n"
                        f"Changed By:\n{current_user.get('name', 'Unknown')}"
                    ),
                    entity_type="bug",
                    entity_id=bug_id,
                    link=f"/dashboard/bugs",
                )
    else:
        history_entry = {
            "status_from": old_status,
            "status_to": new_status,
            "reason": None,
            "changed_by": current_user["id"],
            "changed_by_name": current_user.get("name", "Unknown"),
            "changed_at": now.isoformat()
        }
        status_history.append(history_entry)
        
        await db.activity_logs.insert_one({
            "user_id": current_user["id"],
            "action": "bug_status_changed",
            "entity_type": "bug",
            "entity_id": bug_id,
            "metadata": {
                "title": existing["title"],
                "old_status": old_status,
                "new_status": new_status
            },
            "created_at": now
        })

    return {"status_history": status_history}


@router.put("/{bug_id}")
async def update_bug(
    bug_id: str,
    bug_update: BugUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    existing = await db.bugs.find_one({"_id": ObjectId(bug_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Bug not found")

    # Check permission: reporter, admin, or team_lead can update
    if existing.get("reporter") != current_user["id"] and current_user.get("role", "").lower() not in ["admin", "team_lead", "hr"]:
        raise HTTPException(
            status_code=403, detail="Only bug reporter or admin/team_lead can update"
        )

    update_data = {k: v for k, v in bug_update.model_dump().items() if v is not None}
    if "environment" in update_data and update_data["environment"]:
        update_data["environment"] = update_data["environment"].model_dump()
    now = datetime.now(timezone.utc)
    update_data["updated_at"] = now
    if "status" in update_data and update_data["status"] != existing.get("status"):
        new_status = update_data["status"]
        working_statuses = {"in_progress", "fix_in_progress"}
        resolved_statuses = {"resolved", "verified", "wont_fix", "duplicate"}
        if new_status in working_statuses and not existing.get("started_at"):
            update_data["started_at"] = now
        if new_status in resolved_statuses and not existing.get("resolved_at"):
            update_data["resolved_at"] = now
        elif new_status not in resolved_statuses and existing.get("resolved_at"):
            update_data["resolved_at"] = None

        # Handle status rollback check and history logging
        status_updates = await handle_bug_status_change(
            db=db,
            bug_id=bug_id,
            existing=existing,
            new_status=new_status,
            rollback_reason=update_data.get("rollback_reason"),
            current_user=current_user,
            now=now
        )
        update_data.update(status_updates)

    # Remove rollback_reason from update_data so it doesn't get saved as a top-level field in MongoDB
    update_data.pop("rollback_reason", None)

    # Check if assignee is being set/changed
    old_assignee = existing.get("assignee")
    old_assignee_id = existing.get("assignee_id")

    if "assignee_id" in update_data:
        new_assignee_id = update_data["assignee_id"]
        if new_assignee_id:
            assignee_user = await db.users.find_one({"_id": ObjectId(new_assignee_id)})
            if assignee_user:
                update_data["assignee"] = assignee_user.get("name")
            else:
                update_data["assignee"] = None
        else:
            update_data["assignee"] = None
    elif "assignee" in update_data:
        new_assignee = update_data["assignee"]
        if new_assignee:
            assignee_user = await db.users.find_one({"name": new_assignee})
            if assignee_user:
                update_data["assignee_id"] = str(assignee_user["_id"])
            else:
                update_data["assignee_id"] = None
        else:
            update_data["assignee_id"] = None

    await db.bugs.update_one({"_id": ObjectId(bug_id)}, {"$set": update_data})

    # Fire webhook if status changed
    if "status" in update_data and update_data["status"] != existing.get("status"):
        webhook_payload = {
            "bug_id": bug_id,
            "title": existing["title"],
            "old_status": existing.get("status"),
            "new_status": update_data["status"],
            "updated_at": now.isoformat()
        }
        await webhook_service.trigger_webhooks("bug_status_changed", webhook_payload)

    # Send notification if assignee changed
    new_assignee = update_data.get("assignee")
    new_assignee_id = update_data.get("assignee_id")

    if (new_assignee_id and new_assignee_id != old_assignee_id) or (new_assignee and new_assignee != old_assignee):
        target_id = new_assignee_id
        if not target_id and new_assignee:
            assignee_user = await db.users.find_one({"name": new_assignee})
            if assignee_user:
                target_id = str(assignee_user["_id"])
        
        if target_id:
            await send_notification(
                user_id=target_id,
                type_="bug_assigned",
                title="Bug Assigned",
                message=f"You have been assigned to bug: {existing['title']}",
                entity_type="bug",
                entity_id=bug_id,
                link=f"/dashboard/bugs/{bug_id}",
            )

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "bug_updated",
            "entity_type": "bug",
            "entity_id": bug_id,
            "changes": {
                k: {"old": existing.get(k), "new": v}
                for k, v in update_data.items()
                if k != "updated_at" and k != "status_history"
            },
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"message": "Bug updated successfully"}


@router.delete("/{bug_id}")
async def delete_bug(
    bug_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    bug = await db.bugs.find_one({"_id": ObjectId(bug_id)})
    if not bug:
        raise HTTPException(status_code=404, detail="Bug not found")
    role = current_user.get("role", "").lower()
    is_privileged = role in ("admin", "team_lead")
    is_reporter = bug["reporter"] == current_user["id"]

    # Reporters can only delete their own bugs if still in "open" status
    if is_reporter and bug.get("status") != "open" and not is_privileged:
        raise HTTPException(
            status_code=403,
            detail="Bug has been assigned/acknowledged — only admin or team lead can delete it",
        )

    if not is_reporter and not is_privileged:
        raise HTTPException(status_code=403, detail="Access denied")

    for attachment in bug.get("attachments", []):
        drive_file_id = attachment.get("drive_file_id")
        if not drive_file_id:
            continue
        try:
            delete_from_drive(drive_file_id)
        except Exception as exc:
            logger.warning(f"Failed to delete bug attachment {drive_file_id}: {exc}")

    await db.bugs.delete_one({"_id": ObjectId(bug_id)})
    return {"message": "Bug deleted successfully"}


class ChangeBugStatusRequest(BaseModel):
    status: BugStatus
    rollback_reason: Optional[str] = None


@router.post("/{bug_id}/status")
async def change_bug_status(
    bug_id: str,
    status_data: ChangeBugStatusRequest,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    bug = await db.bugs.find_one({"_id": ObjectId(bug_id)})
    if not bug:
        raise HTTPException(status_code=404, detail="Bug not found")

    new_status = status_data.status.value

    if bug["reporter"] != current_user["id"] and current_user.get("role", "").lower() not in ["admin", "team_lead", "hr"]:
        raise HTTPException(status_code=403, detail="Access denied")

    now = datetime.now(timezone.utc)
    status_updates = await handle_bug_status_change(
        db=db,
        bug_id=bug_id,
        existing=bug,
        new_status=new_status,
        rollback_reason=status_data.rollback_reason,
        current_user=current_user,
        now=now
    )

    update_doc = {
        "status": new_status,
        "updated_at": now
    }
    update_doc.update(status_updates)

    working_statuses = {"in_progress", "fix_in_progress"}
    resolved_statuses = {"resolved", "verified", "wont_fix", "duplicate"}
    if new_status in working_statuses and not bug.get("started_at"):
        update_doc["started_at"] = now
    if new_status in resolved_statuses and not bug.get("resolved_at"):
        update_doc["resolved_at"] = now
    elif new_status not in resolved_statuses and bug.get("resolved_at"):
        update_doc["resolved_at"] = None

    await db.bugs.update_one(
        {"_id": ObjectId(bug_id)},
        {"$set": update_doc},
    )

    # Notify admin/team_lead/hr of status change (in-app only)
    if bug["status"] != new_status:
        await notify_role_watchers(
            notify_roles=["admin", "team_lead", "hr"],
            type_="bug_status_changed",
            title=f"Bug Status: {bug.get('title', 'Untitled')}",
            message=f"Bug '{bug.get('title', 'Untitled')}' moved from {bug['status']} → {new_status}",
            entity_type="bug",
            entity_id=bug_id,
            link=f"/dashboard/bugs/{bug_id}",
            exclude_user_id=current_user["id"],
        )

    return {"message": f"Bug status changed to {new_status}"}
