from fastapi import APIRouter, HTTPException, Depends, Query
from datetime import datetime
from typing import List, Optional
from bson import ObjectId
from database import get_database
from dependencies import get_current_active_user

router = APIRouter(prefix="/activity", tags=["Activity"])


def _get_db():
    return get_database()


@router.get("")
async def get_activity_logs(
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100, description="Maximum number of logs to return"),
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    query = {}

    # Permission: non-admin users can only see their own activity
    role = current_user.get("role", "").lower()
    if role != "admin":
        if user_id and user_id != current_user["id"]:
            raise HTTPException(status_code=403, detail="Can only view own activity")
        query["user_id"] = current_user["id"]
    else:
        if user_id:
            query["user_id"] = user_id

    if entity_type:
        query["entity_type"] = entity_type
    if entity_id:
        query["entity_id"] = entity_id

    cursor = db.activity_logs.find(query).sort("created_at", -1).limit(limit)
    logs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        logs.append(doc)
    return logs


@router.get("/project/{project_id}")
async def get_project_activity(
    project_id: str,
    limit: int = Query(100, ge=1, le=200, description="Maximum number of logs to return"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Get all activity logs related to a project (tasks and bugs within the project).
    Aggregates activity from all tasks and bugs belonging to the project.
    """
    db = _get_db()

    # Fetch all task and bug IDs for this project
    task_cursor = db.tasks.find({"project_id": project_id}, {"_id": 1})
    task_ids = [str(t["_id"]) async for t in task_cursor]
    bug_cursor = db.bugs.find({"project_id": project_id}, {"_id": 1})
    bug_ids = [str(b["_id"]) async for b in bug_cursor]

    if not task_ids and not bug_ids:
        return []

    # Fetch activity logs for these entity IDs
    query = {
        "entity_type": {"$in": ["task", "bug"]},
        "entity_id": {"$in": task_ids + bug_ids},
    }

    # Non-admin users can only see their own activity unless they are admin
    role = current_user.get("role", "").lower()
    if role != "admin":
        query["user_id"] = current_user["id"]

    cursor = db.activity_logs.find(query).sort("created_at", -1).limit(limit)
    raw_logs = []
    user_ids = set()
    task_ids = set()
    bug_ids = set()
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        raw_logs.append(doc)
        if doc.get("user_id"):
            user_ids.add(doc["user_id"])
        if doc.get("entity_id"):
            if doc.get("entity_type") == "task":
                task_ids.add(doc["entity_id"])
            elif doc.get("entity_type") == "bug":
                bug_ids.add(doc["entity_id"])

    # Batch fetch user names
    user_name_map = {}
    if user_ids:
        valid_oids = []
        for uid in user_ids:
            try:
                valid_oids.append(ObjectId(uid))
            except Exception:
                pass
        if valid_oids:
            async for u in db.users.find({"_id": {"$in": valid_oids}}, {"name": 1}):
                user_name_map[str(u["_id"])] = u.get("name", "Unknown")

    # Batch fetch task titles
    task_title_map = {}
    if task_ids:
        valid_oids = []
        for tid in task_ids:
            try:
                valid_oids.append(ObjectId(tid))
            except Exception:
                pass
        if valid_oids:
            async for t in db.tasks.find({"_id": {"$in": valid_oids}}, {"title": 1}):
                task_title_map[str(t["_id"])] = t.get("title", "Unknown Task")

    # Batch fetch bug titles
    bug_title_map = {}
    if bug_ids:
        valid_oids = []
        for bid in bug_ids:
            try:
                valid_oids.append(ObjectId(bid))
            except Exception:
                pass
        if valid_oids:
            async for b in db.bugs.find({"_id": {"$in": valid_oids}}, {"title": 1}):
                bug_title_map[str(b["_id"])] = b.get("title", "Unknown Bug")

    logs = []
    for doc in raw_logs:
        doc["user_name"] = user_name_map.get(doc.get("user_id", ""), "Unknown")
        eid = doc.get("entity_id", "")
        if doc.get("entity_type") == "task":
            doc["entity_title"] = task_title_map.get(eid, "Unknown Task")
        elif doc.get("entity_type") == "bug":
            doc["entity_title"] = bug_title_map.get(eid, "Unknown Bug")
        logs.append(doc)

    return logs
