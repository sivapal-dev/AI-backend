import os
import uuid
import logging
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Request

logger = logging.getLogger(__name__)
from fastapi.responses import HTMLResponse
from typing import List, Optional
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from dependencies import get_current_active_user
from models.project import ProjectAttachment
from helpers.backblaze import upload_to_drive, delete_from_drive, build_direct_image_url

router = APIRouter(prefix="/upload", tags=["Upload"])


def _get_db():
    return get_database()


def _validate_magic_bytes(data: bytes, ext: str) -> bool:
    magic_map = {
        ".pdf": [b"%PDF"],
        ".png": [b"\x89PNG\r\n\x1a\n"],
        ".jpg": [b"\xff\xd8\xff"],
        ".jpeg": [b"\xff\xd8\xff"],
        ".gif": [b"GIF87a", b"GIF89a"],
        ".webp": [b"RIFF"],
    }
    if ext in (".txt", ".md", ".doc", ".docx", ".xlsx", ".xls", ".zip"):
        return True
    expected = magic_map.get(ext)
    if not expected:
        return True
    for sig in expected:
        if data.startswith(sig):
            if ext == ".webp":
                return data[8:12] == b"WEBP"
            return True
    return False


def _build_attachment(file: UploadFile, size: int, drive_result: dict, current_user: dict) -> dict:
    mime_type = file.content_type or ""
    is_image = mime_type.startswith("image/")
    view_url = drive_result["webViewLink"]
    direct_url = build_direct_image_url(drive_result["file_id"]) if is_image else ""
    return {
        "_id": uuid.uuid4().hex,
        "name": file.filename,
        "filename": file.filename,
        "url": direct_url or view_url,
        "view_url": view_url,
        "direct_url": direct_url,
        "mime_type": mime_type,
        "size": size,
        "uploaded_by": current_user["id"],
        "uploaded_by_name": current_user.get("name", "Unknown"),
        "drive_file_id": drive_result["file_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalize_attachment(raw_attachment: dict) -> dict:
    mime_type = raw_attachment.get("mime_type") or ""
    drive_file_id = raw_attachment.get("drive_file_id")
    fallback_direct_url = build_direct_image_url(drive_file_id) if drive_file_id and mime_type.startswith("image/") else ""
    view_url = raw_attachment.get("view_url") or raw_attachment.get("url", "")
    direct_url = raw_attachment.get("direct_url") or fallback_direct_url
    normalized = {
        "_id": raw_attachment.get("_id") or uuid.uuid4().hex,
        "filename": raw_attachment.get("filename") or raw_attachment.get("name", "Attachment"),
        "name": raw_attachment.get("name") or raw_attachment.get("filename", "Attachment"),
        "url": direct_url or raw_attachment.get("url", ""),
        "view_url": view_url,
        "direct_url": direct_url,
        "mime_type": mime_type,
        "size": raw_attachment.get("size"),
        "uploaded_by": raw_attachment.get("uploaded_by"),
        "uploaded_by_name": raw_attachment.get("uploaded_by_name", "Unknown"),
        "drive_file_id": drive_file_id,
        "created_at": raw_attachment.get("created_at"),
    }
    if mime_type.startswith("image/"):
        logger.debug(f"Normalized attachment: filename={normalized['filename']}, drive_file_id={drive_file_id}")
    return normalized


def _attachment_sort_key(item: dict) -> str:
    created_at = item.get("created_at")
    if isinstance(created_at, datetime):
        return created_at.isoformat()
    return created_at or ""


def _get_collection_name(entity_type: str) -> str:
    if entity_type == "task":
        return "tasks"
    if entity_type == "bug":
        return "bugs"
    if entity_type == "sprint":
        return "sprints"
    raise HTTPException(status_code=400, detail=f"Unsupported attachment entity_type: {entity_type}")


def _build_project_mirror_attachment(task: dict, attachment: dict) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    return ProjectAttachment(
        url=attachment.get("direct_url") or attachment.get("url", ""),
        view_url=attachment.get("view_url", ""),
        direct_url=attachment.get("direct_url", ""),
        filename=attachment.get("filename") or attachment.get("name", "Attachment"),
        uploaded_at=now_iso,
        project_id=task.get("project_id", ""),
        uploaded_by=attachment.get("uploaded_by", ""),
        uploaded_by_name=attachment.get("uploaded_by_name", "Unknown"),
        mime_type=attachment.get("mime_type") or "",
        file_id=attachment.get("drive_file_id", ""),
        size=attachment.get("size") or 0,
        created_at=attachment.get("created_at") or now_iso,
        source_type="task",
        source_task_id=str(task.get("_id", "")),
        source_task_title=task.get("title", ""),
        source_attachment_id=attachment.get("_id", ""),
    ).model_dump()


@router.post("", response_model=dict)
async def upload_file(
    file: UploadFile = File(...),
    entity_type: str = Form("general"),
    entity_id: str = Form(""),
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()

    if entity_type not in {"task", "bug", "sprint"}:
        raise HTTPException(status_code=400, detail="Only task, bug, and sprint attachments are supported here")
    if not entity_id:
        raise HTTPException(status_code=400, detail="entity_id is required for task, bug, and sprint attachments")
    if not ObjectId.is_valid(entity_id):
        raise HTTPException(status_code=400, detail="Invalid entity_id")

    # Allowed image extensions (expanded to include common image types)
    allowed_extensions = {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
        ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".md",
        ".xlsx", ".xls", ".csv", ".json", ".zip",
    }
    file_ext = os.path.splitext(file.filename)[1].lower()
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="File type not allowed")

    max_size = 5 * 1024 * 1024  # 5MB
    contents = await file.read()
    if len(contents) > max_size:
        raise HTTPException(status_code=400, detail="File too large")

    # Validate magic bytes (signatures) for security
    if not _validate_magic_bytes(contents, file_ext):
        raise HTTPException(status_code=400, detail="File content does not match its extension")

    # Upload to Google Drive
    try:
        drive_result = upload_to_drive(
            file_content=contents,
            filename=file.filename,
            mime_type=file.content_type
        )
    except Exception as e:
        logging.getLogger(__name__).error("Drive upload failed: %s", str(e), exc_info=True)
        raise HTTPException(status_code=503, detail="File upload is temporarily unavailable. Please try again later.")

    collection_name = _get_collection_name(entity_type)
    collection = getattr(db, collection_name)
    existing = await collection.find_one({"_id": ObjectId(entity_id)})
    if not existing:
        raise HTTPException(status_code=404, detail=f"{entity_type.capitalize()} not found")

    attachment = _build_attachment(file, len(contents), drive_result, current_user)
    await collection.update_one(
        {"_id": ObjectId(entity_id)},
        {
            "$push": {"attachments": attachment},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    # Activity Log
    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "file_uploaded",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "metadata": {"filename": attachment["filename"], "size": len(contents)},
            "created_at": datetime.now(timezone.utc),
        }
    )

    if entity_type == "task" and existing.get("project_id"):
        mirrored_attachment = _build_project_mirror_attachment(existing, attachment)
        await db.projects.update_one(
            {"_id": ObjectId(existing["project_id"])},
            [
                {
                    "$set": {
                        "attachments": {
                            "$concatArrays": [{"$ifNull": ["$attachments", []]}, [mirrored_attachment]]
                        },
                        "updated_at": datetime.now(timezone.utc),
                    }
                }
            ],
        )

    return {
        "_id": attachment["_id"],
        "id": attachment["_id"],
        "filename": attachment["filename"],
        "url": attachment["url"],
        "view_url": attachment["view_url"],
        "direct_url": attachment["direct_url"],
        "mime_type": attachment["mime_type"],
        "drive_file_id": attachment["drive_file_id"],
        "size": attachment["size"],
        "message": "File uploaded successfully",
    }


@router.get("/files/{filename}")
async def get_file(
    filename: str,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Legacy endpoint retained only for backward compatibility.
    """
    raise HTTPException(
        status_code=410,
        detail=f"Legacy filename lookup is no longer supported for {filename}. Use the stored attachment URL directly.",
    )


@router.get("/entity/{entity_type}/{entity_id}")
async def list_entity_files(
    entity_type: str,
    entity_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    if entity_type not in {"task", "bug", "sprint"}:
        raise HTTPException(status_code=400, detail="Unsupported attachment entity_type")
    if not ObjectId.is_valid(entity_id):
        raise HTTPException(status_code=400, detail="Invalid entity_id")

    collection = getattr(db, _get_collection_name(entity_type))
    doc = await collection.find_one({"_id": ObjectId(entity_id)}, {"attachments": 1})
    if not doc:
        raise HTTPException(status_code=404, detail=f"{entity_type.capitalize()} not found")

    files = [_normalize_attachment(att) for att in doc.get("attachments") or []]
    files.sort(key=_attachment_sort_key, reverse=True)
    return files


@router.get("/project/{project_id}")
async def list_project_files(
    project_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Get all files (attachments) for a project across all tasks and bugs.
    Supports optional date filtering via start_date/end_date (ISO strings).
    Enriches files with task/bug titles and uploader names.
     """
    db = _get_db()

    # Fetch all tasks for the project
    tasks_cursor = db.tasks.find({"project_id": project_id}, {"_id": 1, "title": 1, "attachments": 1})
    tasks_list = await tasks_cursor.to_list(length=None)
    task_ids = [str(t["_id"]) for t in tasks_list]
    task_map = {str(t["_id"]): t["title"] for t in tasks_list}

    # Fetch all bugs for the project
    bugs_cursor = db.bugs.find({"project_id": project_id}, {"_id": 1, "title": 1, "attachments": 1})
    bugs_list = await bugs_cursor.to_list(length=None)
    bug_ids = [str(b["_id"]) for b in bugs_list]
    bug_map = {str(b["_id"]): b["title"] for b in bugs_list}

    logger.debug(f"list_project_files: project_id={project_id}, tasks={len(tasks_list)}, bugs={len(bugs_list)}")

    filter_start = None
    filter_end = None
    if start_date:
        try:
            filter_start = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        except ValueError:
            filter_start = None
    if end_date:
        try:
            filter_end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except ValueError:
            filter_end = None

    files = []

    for task in tasks_list:
        task_id = str(task["_id"])
        for raw_attachment in task.get("attachments") or []:
            attachment = _normalize_attachment(raw_attachment)
            created_at = attachment.get("created_at")
            created_dt = None
            if created_at:
                try:
                    created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except ValueError:
                    created_dt = None
            if filter_start and created_dt and created_dt < filter_start:
                continue
            if filter_end and created_dt and created_dt > filter_end:
                continue

            attachment["entity_type"] = "task"
            attachment["entity_id"] = task_id
            attachment["linked_title"] = task_map.get(task_id, "Unknown Task")
            files.append(attachment)

    for bug in bugs_list:
        bug_id = str(bug["_id"])
        for raw_attachment in bug.get("attachments") or []:
            attachment = _normalize_attachment(raw_attachment)
            created_at = attachment.get("created_at")
            created_dt = None
            if created_at:
                try:
                    created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except ValueError:
                    created_dt = None
            if filter_start and created_dt and created_dt < filter_start:
                continue
            if filter_end and created_dt and created_dt > filter_end:
                continue

            attachment["entity_type"] = "bug"
            attachment["entity_id"] = bug_id
            attachment["linked_title"] = bug_map.get(bug_id, "Unknown Bug")
            files.append(attachment)

    files.sort(key=_attachment_sort_key, reverse=True)

    logger.debug(f"Returning {len(files)} files")

    return {
        "files": files,
        "summary": {
            "total": len(files),
            "images": sum(1 for f in files if f.get("mime_type", "").startswith("image/")),
            "documents": sum(1 for f in files if not f.get("mime_type", "").startswith("image/")),
            "by_task": len([f for f in files if f["entity_type"] == "task"]),
            "by_bug": len([f for f in files if f["entity_type"] == "bug"]),
        },
    }


@router.delete("/{file_id}")
async def delete_file(
    file_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    for entity_type, collection_name in (("task", "tasks"), ("bug", "bugs"), ("sprint", "sprints")):
        owner = await getattr(db, collection_name).find_one({"attachments._id": file_id}, {"attachments": 1, "project_id": 1})
        if not owner:
            continue

        attachment = next(
            (item for item in owner.get("attachments", []) if item.get("_id") == file_id),
            None,
        )
        if not attachment:
            continue

        role = current_user.get("role", "").lower()
        if (
            attachment.get("uploaded_by") != current_user["id"]
            and role not in ("admin", "team_lead")
        ):
            raise HTTPException(status_code=403, detail="Access denied")

        drive_file_id = attachment.get("drive_file_id")
        if drive_file_id:
            try:
                delete_from_drive(drive_file_id)
            except Exception as e:
                logger.warning(f"Failed to delete {entity_type} attachment {drive_file_id} from Drive: {e}")

        await getattr(db, collection_name).update_one(
            {"_id": owner["_id"]},
            {
                "$pull": {"attachments": {"_id": file_id}},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )

        # Activity Log
        await db.activity_logs.insert_one(
            {
                "user_id": current_user["id"],
                "action": "file_deleted",
                "entity_type": entity_type,
                "entity_id": str(owner["_id"]),
                "metadata": {"filename": attachment.get("filename")},
                "created_at": datetime.now(timezone.utc),
            }
        )

        if entity_type == "task" and owner.get("project_id"):
            await db.projects.update_one(
                {"_id": ObjectId(owner["project_id"])},
                {
                    "$pull": {"attachments": {"source_attachment_id": file_id}},
                    "$set": {"updated_at": datetime.now(timezone.utc)},
                },
            )
        return {"message": "File deleted successfully"}

    raise HTTPException(status_code=404, detail="File not found")
