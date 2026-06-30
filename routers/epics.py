from fastapi import APIRouter, HTTPException, Depends
from typing import List
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from models.epic import EpicCreate, EpicUpdate
from dependencies import get_current_active_user

router = APIRouter(prefix="/epics", tags=["Epics"])


def _get_db():
    return get_database()


EPIC_COLORS = {
    "blue": ("bg-blue-100 text-blue-700", "bg-blue-500"),
    "green": ("bg-green-100 text-green-700", "bg-green-500"),
    "orange": ("bg-orange-100 text-orange-700", "bg-orange-500"),
    "red": ("bg-red-100 text-red-700", "bg-red-500"),
    "purple": ("bg-purple-100 text-purple-700", "bg-purple-500"),
    "teal": ("bg-teal-100 text-teal-700", "bg-teal-500"),
    "yellow": ("bg-yellow-100 text-yellow-700", "bg-yellow-500"),
    "pink": ("bg-pink-100 text-pink-700", "bg-pink-500"),
}


@router.post("", response_model=dict)
async def create_epic(
    epic: EpicCreate, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(epic.project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() != "admin" and current_user[
        "id"
    ] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Not a member of this project")

    doc = {
        "name": epic.name,
        "description": epic.description,
        "project_id": epic.project_id,
        "status": epic.status.value,
        "color": epic.color.value,
        "created_by": current_user["id"],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = await db.epics.insert_one(doc)

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "epic_created",
            "entity_type": "epic",
            "entity_id": str(result.inserted_id),
            "metadata": {"name": epic.name},
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"id": str(result.inserted_id), "message": "Epic created successfully"}


@router.get("")
async def list_epics(
    project_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Permission: admin/team_lead or project team member
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    cursor = db.epics.find({"project_id": project_id}).sort("created_at", -1)
    epics = []
    epic_ids = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        epics.append(doc)
        epic_ids.append(doc["_id"])

    # Batch fetch task counts for all epics in one query
    if epic_ids:
        epic_id_query_list = []
        for eid in epic_ids:
            epic_id_query_list.append(eid)
            if ObjectId.is_valid(eid):
                epic_id_query_list.append(ObjectId(eid))
        tasks_cursor = db.tasks.find({
            "epic_id": {"$in": epic_id_query_list},
            "parent_id": {"$in": [None, ""]}
        })
        task_counts = {}  # epic_id -> {"total": int, "completed": int}
        async for task in tasks_cursor:
            raw_eid = task.get("epic_id")
            eid = str(raw_eid) if raw_eid else None
            if eid:
                if eid not in task_counts:
                    task_counts[eid] = {"total": 0, "completed": 0}
                task_counts[eid]["total"] += 1
                if task.get("status") == "done":
                    task_counts[eid]["completed"] += 1
        # Assign counts to each epic
        for epic in epics:
            counts = task_counts.get(epic["_id"], {"total": 0, "completed": 0})
            epic["task_count"] = counts["total"]
            epic["completed_task_count"] = counts["completed"]
    else:
        for epic in epics:
            epic["task_count"] = 0
            epic["completed_task_count"] = 0

    return epics


@router.get("/{epic_id}")
async def get_epic(epic_id: str, current_user: dict = Depends(get_current_active_user)):
    db = _get_db()
    epic = await db.epics.find_one({"_id": ObjectId(epic_id)})
    if not epic:
        raise HTTPException(status_code=404, detail="Epic not found")
    
    # Permission: admin/team_lead or project team member
    project = await db.projects.find_one({"_id": ObjectId(epic["project_id"])})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")
    
    epic["_id"] = str(epic["_id"])

    epic_id_query = {"$in": [epic_id]}
    if ObjectId.is_valid(epic_id):
        epic_id_query["$in"].append(ObjectId(epic_id))
    tasks_cursor = db.tasks.find({
        "epic_id": epic_id_query,
        "parent_id": {"$in": [None, ""]}
    })
    task_count = 0
    completed_count = 0
    tasks_list = []
    async for task in tasks_cursor:
        task_count += 1
        task["_id"] = str(task["_id"])
        if task.get("status") == "done":
            completed_count += 1
        tasks_list.append(task)

    epic["task_count"] = task_count
    epic["completed_task_count"] = completed_count
    epic["tasks"] = tasks_list
    return epic


@router.put("/{epic_id}")
async def update_epic(
    epic_id: str,
    epic_update: EpicUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    existing = await db.epics.find_one({"_id": ObjectId(epic_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Epic not found")
    
    # Permission check: admin/team_lead or project team member
    project = await db.projects.find_one({"_id": ObjectId(existing["project_id"])})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Only project team members can update epics")
    
    update_data = {}
    if epic_update.name is not None:
        update_data["name"] = epic_update.name
    if epic_update.description is not None:
        update_data["description"] = epic_update.description
    if epic_update.status is not None:
        update_data["status"] = epic_update.status.value
    if epic_update.color is not None:
        update_data["color"] = epic_update.color.value
    update_data["updated_at"] = datetime.now(timezone.utc)

    await db.epics.update_one({"_id": ObjectId(epic_id)}, {"$set": update_data})
    return {"message": "Epic updated successfully"}


@router.delete("/{epic_id}")
async def delete_epic(
    epic_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    epic = await db.epics.find_one({"_id": ObjectId(epic_id)})
    if not epic:
        raise HTTPException(status_code=404, detail="Epic not found")

    project = await db.projects.find_one({"_id": ObjectId(epic["project_id"])})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Only project team members can delete epics")

    epic_id_query = {"$in": [epic_id]}
    if ObjectId.is_valid(epic_id):
        epic_id_query["$in"].append(ObjectId(epic_id))
    await db.tasks.update_many({"epic_id": epic_id_query}, {"$unset": {"epic_id": ""}})
    await db.epics.delete_one({"_id": ObjectId(epic_id)})
    return {"message": "Epic deleted successfully"}


@router.get("/{epic_id}/tasks")
async def get_epic_tasks(
    epic_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    epic = await db.epics.find_one({"_id": ObjectId(epic_id)})
    if not epic:
        raise HTTPException(status_code=404, detail="Epic not found")
    
    # Permission: admin/team_lead or project team member
    project = await db.projects.find_one({"_id": ObjectId(epic["project_id"])})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    epic_id_query = {"$in": [epic_id]}
    if ObjectId.is_valid(epic_id):
        epic_id_query["$in"].append(ObjectId(epic_id))
    cursor = db.tasks.find({
        "epic_id": epic_id_query,
        "parent_id": {"$in": [None, ""]}
    }).sort("order", 1)
    tasks = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        tasks.append(doc)
    return tasks
