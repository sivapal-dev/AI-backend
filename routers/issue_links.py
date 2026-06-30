from fastapi import APIRouter, HTTPException, Depends
from typing import List
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from models.issue_link import IssueLinkCreate, LinkType
from dependencies import get_current_active_user

router = APIRouter(prefix="/issue-links", tags=["Issue Links"])


def _get_db():
    return get_database()


LINK_LABELS = {
    "blocks": "Blocks",
    "is_blocked_by": "Is blocked by",
    "relates_to": "Relates to",
    "duplicates": "Duplicates",
    "is_duplicated_by": "Is duplicated by",
    "clones": "Clones",
    "is_cloned_by": "Is cloned by",
}


@router.post("", response_model=dict)
async def create_issue_link(
    link: IssueLinkCreate, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()

    if link.source_id == link.target_id:
        raise HTTPException(status_code=400, detail="Cannot link issue to itself")

    existing = await db.issue_links.find_one({
        "source_id": ObjectId(link.source_id),
        "target_id": ObjectId(link.target_id),
        "link_type": link.link_type.value,
    })
    if existing:
        raise HTTPException(status_code=400, detail="Link already exists")

    source = await db.tasks.find_one({"_id": ObjectId(link.source_id)})
    if not source:
        raise HTTPException(status_code=404, detail="Source task not found")
    target = await db.tasks.find_one({"_id": ObjectId(link.target_id)})
    if not target:
        raise HTTPException(status_code=404, detail="Target task not found")
    source_project = await db.projects.find_one({"_id": ObjectId(source["project_id"])})
    if not source_project or (current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in source_project.get("team", [])):
        raise HTTPException(status_code=403, detail="Not authorized for source task's project")

    doc = {
        "source_id": ObjectId(link.source_id),
        "target_id": ObjectId(link.target_id),
        "link_type": link.link_type.value,
        "created_by": current_user["id"],
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.issue_links.insert_one(doc)

    await db.activity_logs.insert_one({
        "user_id": current_user["id"],
        "action": "issue_linked",
        "entity_type": "task",
        "entity_id": link.source_id,
        "metadata": {
            "link_type": link.link_type.value,
            "target_id": link.target_id,
            "target_title": target.get("title"),
        },
        "created_at": datetime.now(timezone.utc),
    })

    return {"id": str(result.inserted_id), "message": "Link created successfully"}


@router.get("/task/{task_id}")
async def get_links_for_task(
    task_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    task = await db.tasks.find_one({"_id": ObjectId(task_id)})
    if task:
        project = await db.projects.find_one({"_id": ObjectId(task["project_id"])})
        if not project or (current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", [])):
            raise HTTPException(status_code=403, detail="Access denied")

    cursor = db.issue_links.find({
        "$or": [{"source_id": ObjectId(task_id)}, {"target_id": ObjectId(task_id)}]
    })

    links = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        links.append(doc)

    # Batch fetch source and target task titles
    all_task_ids = list(set(
        str(doc["source_id"]) for doc in links
    ) | set(
        str(doc["target_id"]) for doc in links
    ))
    task_title_map = {}
    if all_task_ids:
        valid_oids = []
        for tid in all_task_ids:
            try:
                valid_oids.append(ObjectId(tid))
            except Exception:
                pass
        if valid_oids:
            async for t in db.tasks.find({"_id": {"$in": valid_oids}}, {"title": 1}):
                task_title_map[str(t["_id"])] = t.get("title")

    for doc in links:
        source = task_title_map.get(str(doc["source_id"]))
        target = task_title_map.get(str(doc["target_id"]))
        doc["source_title"] = source
        doc["target_title"] = target
        doc["link_label"] = LINK_LABELS.get(doc["link_type"], doc["link_type"])

        if str(doc["source_id"]) == task_id:
            doc["direction"] = "outgoing"
            doc["linked_title"] = doc["target_title"]
            doc["linked_id"] = doc["target_id"]
        else:
            doc["direction"] = "incoming"
            doc["linked_title"] = doc["source_title"]
            doc["linked_id"] = doc["source_id"]

    return links


@router.delete("/{link_id}")
async def delete_issue_link(
    link_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    link = await db.issue_links.find_one({"_id": ObjectId(link_id)})
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    source = await db.tasks.find_one({"_id": ObjectId(link["source_id"])})
    if source:
        project = await db.projects.find_one({"_id": ObjectId(source["project_id"])})
        if not project or (current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", [])):
            raise HTTPException(status_code=403, detail="Access denied")

    await db.issue_links.delete_one({"_id": ObjectId(link_id)})

    await db.activity_logs.insert_one({
        "user_id": current_user["id"],
        "action": "issue_unlinked",
        "entity_type": "task",
        "entity_id": link["source_id"],
        "metadata": {
            "link_type": link["link_type"],
            "target_id": link["target_id"],
        },
        "created_at": datetime.now(timezone.utc),
    })

    return {"message": "Link deleted successfully"}


@router.get("/project/{project_id}")
async def get_links_for_project(
    project_id: str, current_user: dict = Depends(get_current_active_user)
):
    """Get all issue links for tasks within a project"""
    db = _get_db()

    # Verify project access
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    # Get all task IDs in this project
    task_cursor = db.tasks.find({"project_id": project_id}, {"_id": 1})
    task_ids = [str(task["_id"]) async for task in task_cursor]

    if not task_ids:
        return []

    # Find all links where either source or target is in this project
    # Convert string IDs to ObjectId for query
    object_ids = [ObjectId(tid) for tid in task_ids]
    cursor = db.issue_links.find({
        "$or": [
            {"source_id": {"$in": object_ids}},
            {"target_id": {"$in": object_ids}}
        ]
    })

    links = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        links.append(doc)

    # Batch fetch all linked task titles
    all_task_ids = list(set(
        str(doc["source_id"]) for doc in links
    ) | set(
        str(doc["target_id"]) for doc in links
    ))
    task_title_map = {}
    if all_task_ids:
        valid_oids = []
        for tid in all_task_ids:
            try:
                valid_oids.append(ObjectId(tid))
            except Exception:
                pass
        if valid_oids:
            async for t in db.tasks.find({"_id": {"$in": valid_oids}}, {"title": 1}):
                task_title_map[str(t["_id"])] = t.get("title")

    for doc in links:
        doc["source_title"] = task_title_map.get(str(doc["source_id"]))
        doc["target_title"] = task_title_map.get(str(doc["target_id"]))
        doc["link_label"] = LINK_LABELS.get(doc["link_type"], doc["link_type"])
        if doc["source_id"] in object_ids and doc["target_id"] in object_ids:
            doc["direction"] = "internal"
        elif doc["source_id"] in object_ids:
            doc["direction"] = "outgoing"
        else:
            doc["direction"] = "incoming"
        links.append(doc)

    return links
