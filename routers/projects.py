import logging
from fastapi import APIRouter, HTTPException, status, Depends, UploadFile, File, Query
from typing import List, Optional
from bson import ObjectId
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
from database import get_database
from models.project import (
    ProjectCreate,
    ProjectUpdate,
    ProjectInDB,
    ProjectResponse,
    ProjectAttachment,
)
from dependencies import get_current_active_user, validate_object_id
from helpers.notification_sender import send_notification
from helpers.backblaze import upload_to_drive, delete_from_drive, build_direct_image_url

router = APIRouter(prefix="/projects", tags=["Projects"])


def _get_db():
    return get_database()


def _normalize_project_attachment(raw_attachment: dict) -> dict:
    mime_type = raw_attachment.get("mime_type") or ""
    file_id = raw_attachment.get("file_id", "")
    fallback_direct_url = build_direct_image_url(file_id) if file_id and mime_type.startswith("image/") else ""
    view_url = raw_attachment.get("view_url") or raw_attachment.get("url", "")
    direct_url = raw_attachment.get("direct_url") or fallback_direct_url
    normalized = {
        **raw_attachment,
        "url": direct_url or raw_attachment.get("url", ""),
        "view_url": view_url,
        "direct_url": direct_url,
    }
    if mime_type.startswith("image/"):
        logger.debug(f"Normalized project attachment: filename={normalized.get('filename')}, file_id={file_id}")
    return normalized


@router.post("", response_model=dict)
async def create_project(
    project: ProjectCreate, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    
    # Only admin or team_lead can create projects
    if current_user.get("role", "").lower() not in ["admin", "team_lead"]:
        raise HTTPException(
            status_code=403,
            detail="Only admin or team lead can create projects"
        )
    
    # Deduplicate team list while preserving order
    team_list = list(dict.fromkeys([current_user["id"]] + project.team))
    
    project_doc = {
        "name": project.name,
        "description": project.description,
        "markdown_content": project.markdown_content,
        "status": project.status.value,
        "creation_source": (project.creation_source or "manual").lower(),
        "tags": project.tags,
        "start_date": project.start_date.isoformat() if project.start_date else None,
        "due_date": project.due_date.isoformat() if project.due_date else None,
         "github_repo": project.github_repo,
         "attachments": [img.model_dump() for img in project.attachments]
         if project.attachments
         else None,
         "created_by": current_user["id"],
        "created_by_name": current_user.get("name", "Unknown"),
        "team": team_list,
        "is_favorite": False,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = await db.projects.insert_one(project_doc)

    # Send notification to all team members
    for member_id in team_list:
        if member_id != current_user["id"]:  # Skip creator
            await send_notification(
                user_id=member_id,
                type_="project_invite",
                title="Added to Project",
                message=f"You have been added to project: {project.name}",
                entity_type="project",
                entity_id=str(result.inserted_id),
                link=f"/dashboard/projects/{result.inserted_id}",
            )

    # Project deadline notification (if due_date set)
    if project.due_date:
        for member_id in team_list:
            if member_id != current_user["id"]:
                await send_notification(
                    user_id=member_id,
                    type_="project_deadline",
                    title="Project Deadline Set",
                    message=f"Project '{project.name}' has a deadline of {project.due_date.strftime('%b %d, %Y')}",
                    entity_type="project",
                    entity_id=str(result.inserted_id),
                    link=f"/dashboard/projects/{result.inserted_id}",
                )

    return {"id": str(result.inserted_id), "message": "Project created successfully"}


@router.get("", response_model=List[ProjectResponse])
async def list_projects(current_user: dict = Depends(get_current_active_user)):
    db = _get_db()
    # Admin and team_lead see all projects; regular users see only team projects
    role = current_user.get("role", "").lower()
    if role in ("admin", "team_lead"):
        cursor = db.projects.find({"status": {"$ne": "archived"}}).sort("created_at", -1)
    else:
        cursor = db.projects.find({"team": current_user["id"], "status": {"$ne": "archived"}}).sort("created_at", -1)
    projects = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        projects.append(doc)
    return projects


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    project = await db.projects.find_one({"_id": validate_object_id(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # Admin and team_lead can access any project; regular users must be team members
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Enrich created_by_name if missing (backward compatibility for old projects)
    if "created_by_name" not in project and project.get("created_by"):
        try:
            creator = await db.users.find_one({"_id": ObjectId(project["created_by"])})
            if creator:
                project["created_by_name"] = creator.get("name", "Unknown")
            else:
                project["created_by_name"] = project["created_by"]
        except Exception:
            project["created_by_name"] = project.get("created_by", "Unknown")
    
    project["_id"] = str(project["_id"])
    return project


@router.get("/{project_id}/team-members")
async def get_project_team_members(
    project_id: str, current_user: dict = Depends(get_current_active_user)
):
    """Get team member details for a project (admin or team member only)"""
    db = _get_db()
    project = await db.projects.find_one({"_id": validate_object_id(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # Access check: admin, team_lead, or team member
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")
    team_ids = project.get("team", [])
    if not team_ids:
        return []
    # Validate ObjectIds
    valid_oids = []
    for uid in team_ids:
        try:
            valid_oids.append(ObjectId(uid))
        except Exception:
            continue
    # Fetch user documents
    users = []
    async for user in db.users.find({"_id": {"$in": valid_oids}}):
        user["_id"] = str(user["_id"])
        # Remove sensitive fields
        user.pop("verification_token", None)
        user.pop("verification_token_expires", None)
        user.pop("github_access_token", None)
        user.pop("email_credentials", None)
        user.pop("refresh_token_hashes", None)
        users.append(user)
    return users


from pydantic import BaseModel

class TeamMemberAdd(BaseModel):
    user_id: str

@router.post("/{project_id}/team-members")
async def add_project_team_member(
    project_id: str,
    data: TeamMemberAdd,
    current_user: dict = Depends(get_current_active_user)
):
    """Add a team member to a project (admin, team_lead, or creator only)"""
    db = _get_db()
    project = await db.projects.find_one({"_id": validate_object_id(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
        
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead") and project.get("created_by") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only project creator or admin/team_lead can add team members")
        
    # Check if user is already in the project's team list to prevent duplicate operations/notifications (W216)
    if data.user_id in project.get("team", []):
        return {"message": "Team member added successfully"}

    # Check if user exists
    try:
        user_oid = ObjectId(data.user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user ID")
        
    user = await db.users.find_one({"_id": user_oid})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    # Add to team
    await db.projects.update_one(
        {"_id": validate_object_id(project_id)},
        {"$addToSet": {"team": data.user_id}, "$set": {"updated_at": datetime.now(timezone.utc)}}
    )
    
    # Sync to non-cancelled meetings for this project
    await db.meetings.update_many(
        {"project_id": project_id, "status": {"$ne": "cancelled"}},
        {"$addToSet": {"attendees": data.user_id}}
    )
    
    # Notify user
    if data.user_id != current_user["id"]:
        await send_notification(
            user_id=data.user_id,
            type_="project_invite",
            title="Added to Project",
            message=f"You have been added to project: {project['name']}",
            entity_type="project",
            entity_id=project_id,
            link=f"/dashboard/projects/{project_id}",
        )
        
    return {"message": "Team member added successfully"}


@router.delete("/{project_id}/team-members/{user_id}")
async def remove_project_team_member(
    project_id: str,
    user_id: str,
    current_user: dict = Depends(get_current_active_user)
):
    """Remove a team member from a project (admin, team_lead, or creator only)"""
    db = _get_db()
    project = await db.projects.find_one({"_id": validate_object_id(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
        
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead") and project.get("created_by") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only project creator or admin/team_lead can remove team members")
        
    # Cannot remove creator
    if user_id == project.get("created_by"):
        raise HTTPException(status_code=400, detail="Cannot remove project creator from team")
        
    await db.projects.update_one(
        {"_id": validate_object_id(project_id)},
        {"$pull": {"team": user_id}, "$set": {"updated_at": datetime.now(timezone.utc)}}
    )
    
    # Sync to non-cancelled meetings for this project
    await db.meetings.update_many(
        {"project_id": project_id, "status": {"$ne": "cancelled"}},
        {"$pull": {"attendees": user_id}}
    )
    
    return {"message": "Team member removed successfully"}


@router.put("/{project_id}", response_model=dict)
async def update_project(
    project_id: str,
    project: ProjectUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    existing = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    # Admin and team_lead can update any project; only creator can update otherwise
    if (
        existing["created_by"] != current_user["id"]
        and current_user.get("role", "").lower() not in ("admin", "team_lead")
    ):
        raise HTTPException(
            status_code=403, detail="Only project creator, admin, or team_lead can update"
        )

    update_data = {k: v for k, v in project.model_dump().items() if v is not None}

    # Convert datetime fields to ISO strings
    if "start_date" in update_data and update_data["start_date"]:
        update_data["start_date"] = update_data["start_date"].isoformat()
    if "due_date" in update_data and update_data["due_date"]:
        update_data["due_date"] = update_data["due_date"].isoformat()

    update_data["updated_at"] = datetime.now(timezone.utc)

    # Check if due_date is being set/changed
    def _normalize_date_str(d):
        if not d:
            return None
        if isinstance(d, datetime):
            return d.isoformat()
        if isinstance(d, str):
            try:
                return datetime.fromisoformat(d.replace("Z", "+00:00")).isoformat()
            except Exception:
                return d
        return str(d)

    old_due_date = existing.get("due_date")
    new_due_date = update_data.get("due_date")
    
    await db.projects.update_one({"_id": validate_object_id(project_id)}, {"$set": update_data})

    # Send deadline notification if due_date is newly set or changed
    if new_due_date and _normalize_date_str(new_due_date) != _normalize_date_str(old_due_date):
        team_member_ids = existing.get("team", [])
        # Format due_date for message
        try:
            due_date_formatted = datetime.fromisoformat(new_due_date.replace("Z", "+00:00")).strftime("%b %d, %Y")
        except Exception:
            due_date_formatted = new_due_date  # fallback to raw string
        
        for member_id in team_member_ids:
            if member_id != current_user["id"]:
                await send_notification(
                    user_id=member_id,
                    type_="project_deadline",
                    title="Project Deadline Updated",
                    message=f"Project '{existing['name']}' deadline is now {due_date_formatted}",
                    entity_type="project",
                    entity_id=project_id,
                    link=f"/dashboard/projects/{project_id}",
                )

    return {"message": "Project updated successfully"}


@router.delete("/{project_id}", response_model=dict)
async def delete_project(
    project_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    existing = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    # Admin and team_lead can delete any project; only creator can delete otherwise
    if (
        existing["created_by"] != current_user["id"]
        and current_user.get("role", "").lower() not in ("admin", "team_lead")
    ):
        raise HTTPException(
            status_code=403, detail="Only project creator, admin, or team_lead can delete"
        )

    # Perform a hard delete of the project and all its associated data
    tasks = await db.tasks.find({"project_id": project_id}, {"_id": 1, "attachments": 1}).to_list(length=None)
    bugs = await db.bugs.find({"project_id": project_id}, {"_id": 1, "attachments": 1}).to_list(length=None)
    task_ids = [str(task["_id"]) for task in tasks]

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

    await db.projects.delete_one({"_id": ObjectId(project_id)})
    await db.tasks.delete_many({"project_id": project_id})
    await db.bugs.delete_many({"project_id": project_id})
    await db.documents.delete_many({"project_id": project_id})
    await db.sprints.delete_many({"project_id": project_id})
    await db.ams_tickets.delete_many({"project_id": project_id})
    await db.whiteboards.delete_many({"project_id": project_id})

    return {"message": "Project and all associated data permanently deleted"}


@router.post("/{project_id}/favorite", response_model=dict)
async def toggle_favorite(
    project_id: str, current_user: dict = Depends(get_current_active_user)
):
    """Toggle favorite status for a project"""
    db = _get_db()
    try:
        project = await db.projects.find_one({"_id": ObjectId(project_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid project ID")

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Access check: user must be in team or admin/team_lead
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    new_favorite = not project.get("is_favorite", False)
    await db.projects.update_one(
        {"_id": validate_object_id(project_id)}, {"$set": {"is_favorite": new_favorite}}
    )
    return {"is_favorite": new_favorite, "message": "Favorite status updated"}


@router.get("/{project_id}/tasks")
async def get_project_tasks(
    project_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # Admin and team_lead can access any project's tasks; regular users must be team members
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    cursor = db.tasks.find({
        "project_id": project_id,
        "parent_id": {"$in": [None, ""]}
    }).sort("order", 1)
    tasks = []
    assignee_ids = []
    reporter_ids = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if "reporter" in doc:
            doc["reporter"] = str(doc["reporter"])
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


@router.get("/{project_id}/bugs")
async def get_project_bugs(
    project_id: str,
    status: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=0),
    skip: Optional[int] = Query(None, ge=0),
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # Admin and team_lead can access any project's bugs; regular users must be team members
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    query = {"project_id": project_id}
    if status:
        query["status"] = status

    cursor = db.bugs.find(query).sort("created_at", -1)
    if skip is not None:
        cursor = cursor.skip(skip)
    if limit is not None:
        cursor = cursor.limit(limit)
    bugs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if "reporter" in doc:
            doc["reporter"] = str(doc["reporter"])
        bugs.append(doc)
    return bugs


# ─── Project Images ────────────────────────────────────────────────────────────

@router.get("/{project_id}/attachments")
async def list_project_attachments(
    project_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Get all project attachments."""
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Access control: project team member or admin/team_lead
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    attachments = [_normalize_project_attachment(att) for att in (project.get("attachments") or [])]
    return attachments


@router.post("/{project_id}/attachments")
async def upload_project_attachment(
    project_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_active_user),
):
    """Upload an attachment to a project."""
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Allow admin, team_lead, HR, or any project team member to upload project attachments
    role = current_user.get("role", "").lower()
    is_privileged = role in ("admin", "team_lead", "hr")
    is_member = current_user["id"] in project.get("team", []) or project.get("created_by") == current_user["id"]
    if not is_privileged and not is_member:
        raise HTTPException(
            status_code=403,
            detail="Only project members, team leads, HR, or admins can upload project attachments"
        )

    # Upload to Google Drive
    contents = await file.read()
    try:
        drive_result = upload_to_drive(
            file_content=contents,
            filename=file.filename,
            mime_type=file.content_type,
        )
    except Exception as e:
        logger.error(f"Drive upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload to Google Drive: {str(e)}")

    logger.debug(f"Drive upload success, keys: {list(drive_result.keys())}")

    # Build ProjectAttachment entry
    # Dynamically map response keys based on active provider configuration (W219)
    now_iso = datetime.now(timezone.utc).isoformat()
    file_id_val = drive_result.get("file_id") or drive_result.get("id") or ""
    direct_url = drive_result.get("directUrl") or drive_result.get("webViewLink") or drive_result.get("webContentLink") or ""
    thumbnail_url = drive_result.get("thumbnailLink") or drive_result.get("thumbnail_url") or direct_url

    logger.debug(f"Attachment URLs: direct_url={direct_url[:60]}..., file_id={file_id_val}")

    attachment_entry = ProjectAttachment(
        url=direct_url,
        filename=file.filename,
        uploaded_at=now_iso,
        project_id=project_id,
        uploaded_by=current_user["id"],
        uploaded_by_name=current_user.get("name", "Unknown"),
        mime_type=file.content_type or "",
        thumbnail_url=thumbnail_url,
        file_id=file_id_val,
        size=len(contents),
        created_at=now_iso,
    ).model_dump()

    logger.debug(f"Attachment entry built")

    # Use aggregation pipeline to handle null -> array conversion
    # If attachments is null or missing, $ifNull gives [], then $concatArrays appends the new attachment
    result = await db.projects.update_one(
      {"_id": ObjectId(project_id)},
      [
          {
              "$set": {
                  "attachments": {
                      "$concatArrays": [{"$ifNull": ["$attachments", []]}, [attachment_entry]]
                  },
                  "updated_at": datetime.now(timezone.utc),
              }
          }
      ],
    )
    logger.debug(f"MongoDB update result: modified_count={result.modified_count}")

    return attachment_entry


@router.delete("/{project_id}/attachments/{file_id}")
async def delete_project_attachment(
    project_id: str,
    file_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Delete an attachment from project."""
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    attachment = next(
        (item for item in (project.get("attachments") or []) if item.get("file_id") == file_id),
        None,
    )
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Only admin, team_lead, or the uploader can delete project attachments
    role = current_user.get("role", "").lower()
    is_privileged = role in ("admin", "team_lead")
    is_uploader = attachment.get("uploaded_by") == current_user["id"]
    if not is_privileged and not is_uploader:
        raise HTTPException(
            status_code=403,
            detail="Only admin, team lead, or the uploader can delete project attachments"
        )

    if attachment.get("source_type") == "task" and attachment.get("source_task_id") and attachment.get("source_attachment_id"):
        await db.tasks.update_one(
            {"_id": ObjectId(attachment["source_task_id"])},
            {
                "$pull": {"attachments": {"_id": attachment["source_attachment_id"]}},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )

    result = await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$pull": {"attachments": {"file_id": file_id}}},
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Attachment not found")

    drive_file_id = attachment.get("file_id")
    if drive_file_id:
        try:
            delete_from_drive(drive_file_id)
        except Exception as exc:
            logger.warning(f"Failed to delete Drive file {drive_file_id}: {exc}")

    return {"message": "Attachment deleted"}
