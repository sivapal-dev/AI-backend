from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from bson import ObjectId

from database import get_database
from dependencies import get_current_active_user
from models.workflow import WorkflowCreate, WorkflowUpdate, WorkflowInDB, WorkflowResponse

router = APIRouter(prefix="/workflows", tags=["Workflows"])


def _get_db():
    return get_database()


DEFAULT_WORKFLOW = {
    "name": "Default",
    "states": [
        {"id": "backlog", "label": "Backlog", "color": "bg-zinc-100 dark:bg-zinc-800", "order": 0},
        {"id": "todo", "label": "Todo", "color": "bg-blue-50 dark:bg-blue-900/20", "order": 1},
        {"id": "in_progress", "label": "In Progress", "color": "bg-orange-50 dark:bg-orange-900/20", "order": 2},
        {"id": "code_review", "label": "Code Review", "color": "bg-teal-50 dark:bg-teal-900/20", "order": 3},
        {"id": "testing", "label": "Testing", "color": "bg-orange-50 dark:bg-orange-900/20", "order": 4},
        {"id": "done", "label": "Done", "color": "bg-teal-50 dark:bg-teal-900/20", "order": 5},
    ],
    "transitions": [
        {"from_status": "backlog", "to_status": "todo"},
        {"from_status": "todo", "to_status": "in_progress"},
        {"from_status": "in_progress", "to_status": "code_review"},
        {"from_status": "code_review", "to_status": "testing"},
        {"from_status": "testing", "to_status": "done"},
        {"from_status": "done", "to_status": "backlog"},
    ],
    "default_state": "backlog",
}


@router.post("", response_model=WorkflowResponse)
async def create_workflow(
    data: WorkflowCreate,
    project_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    user_id = str(current_user["id"])
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")
    doc = {
        "project_id": project_id,
        "created_by": user_id,
        "name": data.name,
        "states": [s.model_dump() for s in data.states],
        "transitions": [t.model_dump() for t in data.transitions],
        "default_state": data.default_state,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db["workflows"].insert_one(doc)
    doc["_id"] = result.inserted_id
    return WorkflowResponse(**WorkflowInDB(**doc).model_dump(by_alias=True))


@router.get("", response_model=Optional[WorkflowResponse])
async def get_workflow(project_id: str, current_user: dict = Depends(get_current_active_user)):
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")
    doc = await db["workflows"].find_one({"project_id": project_id})
    if not doc:
        return WorkflowResponse(**DEFAULT_WORKFLOW, id="default", project_id=project_id, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
    doc["_id"] = str(doc["_id"])
    return WorkflowResponse(**doc)


@router.put("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    data: WorkflowUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    user_id = str(current_user["id"])
    doc = await db["workflows"].find_one({"_id": ObjectId(workflow_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if doc.get("created_by") != user_id and current_user.get("role", "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Only workflow creator or admin can update")
    update = {}
    if data.name is not None:
        update["name"] = data.name
    if data.states is not None:
        update["states"] = [s.model_dump() for s in data.states]
    if data.transitions is not None:
        update["transitions"] = [t.model_dump() for t in data.transitions]
    if data.default_state is not None:
        update["default_state"] = data.default_state
    if not update:
        raise HTTPException(status_code=400, detail="No fields to update")
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = await db["workflows"].update_one(
        {"_id": ObjectId(workflow_id)},
        {"$set": update},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"status": "updated"}


@router.delete("/{workflow_id}")
async def delete_workflow(workflow_id: str, current_user: dict = Depends(get_current_active_user)):
    db = _get_db()
    user_id = str(current_user["id"])
    doc = await db["workflows"].find_one({"_id": ObjectId(workflow_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if doc.get("created_by") != user_id and current_user.get("role", "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Only workflow creator or admin can delete")
    result = await db["workflows"].delete_one({"_id": ObjectId(workflow_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"status": "deleted"}


@router.get("/{project_id}/allowed-transitions")
async def get_allowed_transitions(
    project_id: str,
    status: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    doc = await db["workflows"].find_one({"project_id": project_id})
    if not doc:
        transitions = DEFAULT_WORKFLOW["transitions"]
    else:
        transitions = doc.get("transitions", [])
    allowed = [t["to_status"] for t in transitions if t["from_status"] == status]
    return {"from_status": status, "allowed_to": allowed}
