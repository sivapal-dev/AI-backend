from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from models.whiteboard import (
    WhiteboardCreate,
    WhiteboardUpdate,
    WhiteboardInDB,
    WhiteboardResponse,
    CanvasElementCreate,
    CanvasElementUpdate,
)
from dependencies import get_current_active_user

router = APIRouter(prefix="/whiteboards", tags=["Whiteboards"])


def _get_db():
    return get_database()


@router.get("", response_model=List[WhiteboardResponse])
async def list_whiteboards(current_user: dict = Depends(get_current_active_user)):
    """List all whiteboards accessible to the user (personal + project + public)"""
    db = _get_db()
    user_id = current_user["id"]

    # Collect project IDs the user is a member of
    user_projects = await db.projects.find({"team": user_id}, {"_id": 1}).to_list(length=None)
    project_ids = [str(p["_id"]) for p in user_projects]

    # Find: personal whiteboards (created_by), project whiteboards, and public whiteboards
    query = {"$or": [
        {"created_by": user_id},
        {"is_public": True},
        {"project_id": {"$in": project_ids}},
    ]}
    cursor = db.whiteboards.find(query).sort("updated_at", -1)
    whiteboards = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        whiteboards.append(doc)

    return whiteboards


@router.get("/project/{project_id}", response_model=List[WhiteboardResponse])
async def list_project_whiteboards(
    project_id: str, current_user: dict = Depends(get_current_active_user)
):
    """Get all whiteboards for a project"""
    db = _get_db()
    # Check project access
    try:
        project = await db.projects.find_one({"_id": ObjectId(project_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    role = current_user.get("role", "").lower()
    if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    cursor = db.whiteboards.find({"project_id": project_id}).sort("updated_at", -1)
    whiteboards = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        whiteboards.append(doc)
    return whiteboards


@router.get("/{whiteboard_id}", response_model=WhiteboardResponse)
async def get_whiteboard(
    whiteboard_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    try:
        wb = await db.whiteboards.find_one({"_id": ObjectId(whiteboard_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid whiteboard ID")
    if not wb:
        raise HTTPException(status_code=404, detail="Whiteboard not found")

    # Access check
    if wb.get("project_id"):
        # Project whiteboard — check project membership
        project = await db.projects.find_one({"_id": ObjectId(wb["project_id"])})
        if not project:
            raise HTTPException(status_code=404, detail="Associated project not found")
        role = current_user.get("role", "").lower()
        if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        # Personal whiteboard — only creator can access
        if wb["created_by"] != current_user["id"] and not wb.get("is_public"):
            raise HTTPException(status_code=403, detail="Access denied")

    wb["_id"] = str(wb["_id"])
    return wb


@router.post("", response_model=dict)
async def create_whiteboard(
    wb: WhiteboardCreate, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()

    # If project_id provided, check project access
    if wb.project_id:
        project = await db.projects.find_one({"_id": ObjectId(wb.project_id)})
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        role = current_user.get("role", "").lower()
        if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")

    wb_doc = {
        "name": wb.name,
        "description": wb.description or "",
        "project_id": wb.project_id,
        "elements": wb.elements or [],  # initial elements if passed, else empty
        "background_color": wb.background_color,
        "grid_size": wb.grid_size,
        "is_public": wb.is_public,
        "created_by": current_user["id"],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = await db.whiteboards.insert_one(wb_doc)
    return {"id": str(result.inserted_id), "message": "Whiteboard created successfully"}


@router.put("/{whiteboard_id}", response_model=dict)
async def update_whiteboard(
    whiteboard_id: str,
    wb: WhiteboardUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    try:
        existing = await db.whiteboards.find_one({"_id": ObjectId(whiteboard_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid whiteboard ID")
    if not existing:
        raise HTTPException(status_code=404, detail="Whiteboard not found")

    # Only creator can update (or project members if project whiteboard)
    if existing.get("project_id"):
        project = await db.projects.find_one({"_id": ObjectId(existing["project_id"])})
        if not project:
            raise HTTPException(status_code=404, detail="Associated project not found")
        role = current_user.get("role", "").lower()
        if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        if existing["created_by"] != current_user["id"]:
            raise HTTPException(status_code=403, detail="Only creator can update")

    update_data = {k: v for k, v in wb.model_dump().items() if v is not None}
    update_data["updated_at"] = datetime.now(timezone.utc)
    await db.whiteboards.update_one({"_id": ObjectId(whiteboard_id)}, {"$set": update_data})
    return {"message": "Whiteboard updated successfully"}


@router.delete("/{whiteboard_id}", response_model=dict)
async def delete_whiteboard(
    whiteboard_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    try:
        wb = await db.whiteboards.find_one({"_id": ObjectId(whiteboard_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid whiteboard ID")
    if not wb:
        raise HTTPException(status_code=404, detail="Whiteboard not found")

    # Only creator can delete
    if wb["created_by"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only creator can delete")

    await db.whiteboards.delete_one({"_id": ObjectId(whiteboard_id)})
    return {"message": "Whiteboard deleted successfully"}


# --- Canvas Elements ---

@router.get("/{whiteboard_id}/elements", response_model=List[dict])
async def get_elements(whiteboard_id: str, current_user: dict = Depends(get_current_active_user)):
    """Get all canvas elements for a whiteboard"""
    db = _get_db()
    # Access check (reuse get_whiteboard logic inline)
    try:
        wb = await db.whiteboards.find_one({"_id": ObjectId(whiteboard_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid whiteboard ID")
    if not wb:
        raise HTTPException(status_code=404, detail="Whiteboard not found")

    if wb.get("project_id"):
        project = await db.projects.find_one({"_id": ObjectId(wb["project_id"])})
        if not project:
            raise HTTPException(status_code=404, detail="Associated project not found")
        role = current_user.get("role", "").lower()
        if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        if wb["created_by"] != current_user["id"] and not wb.get("is_public"):
            raise HTTPException(status_code=403, detail="Access denied")

    elements = wb.get("elements", [])
    # Ensure each element has id field
    for el in elements:
        if "_id" in el:
            el["id"] = str(el["_id"])
            del el["_id"]
    return elements


@router.post("/{whiteboard_id}/elements", response_model=dict)
async def add_element(
    whiteboard_id: str,
    element: CanvasElementCreate,
    current_user: dict = Depends(get_current_active_user),
):
    """Add a new element to the whiteboard"""
    db = _get_db()
    # Access check
    try:
        wb = await db.whiteboards.find_one({"_id": ObjectId(whiteboard_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid whiteboard ID")
    if not wb:
        raise HTTPException(status_code=404, detail="Whiteboard not found")

    if wb.get("project_id"):
        project = await db.projects.find_one({"_id": ObjectId(wb["project_id"])})
        if not project:
            raise HTTPException(status_code=404, detail="Associated project not found")
        role = current_user.get("role", "").lower()
        if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        if wb["created_by"] != current_user["id"]:
            raise HTTPException(status_code=403, detail="Only creator can edit")

    element_id = str(ObjectId())
    element_doc = element.model_dump()
    element_doc["id"] = element_id
    # Remove _id field if present
    element_doc.pop("_id", None)

    await db.whiteboards.update_one(
        {"_id": ObjectId(whiteboard_id)},
        {"$push": {"elements": element_doc}, "$set": {"updated_at": datetime.now(timezone.utc)}}
    )
    return {"id": element_id, "message": "Element added"}


@router.put("/{whiteboard_id}/elements/{element_id}", response_model=dict)
async def update_element(
    whiteboard_id: str,
    element_id: str,
    element: CanvasElementUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    """Update an existing canvas element"""
    db = _get_db()
    # Access check
    try:
        wb = await db.whiteboards.find_one({"_id": ObjectId(whiteboard_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid whiteboard ID")
    if not wb:
        raise HTTPException(status_code=404, detail="Whiteboard not found")

    if wb.get("project_id"):
        project = await db.projects.find_one({"_id": ObjectId(wb["project_id"])})
        if not project:
            raise HTTPException(status_code=404, detail="Associated project not found")
        role = current_user.get("role", "").lower()
        if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        if wb["created_by"] != current_user["id"]:
            raise HTTPException(status_code=403, detail="Only creator can edit")

    update_data = {k: v for k, v in element.model_dump().items() if v is not None}
    if update_data:
        set_payload = {f"elements.$.{k}": v for k, v in update_data.items()}
        set_payload["updated_at"] = datetime.now(timezone.utc)
        await db.whiteboards.update_one(
            {"_id": ObjectId(whiteboard_id), "elements.id": element_id},
            {"$set": set_payload}
        )
    return {"message": "Element updated"}


@router.delete("/{whiteboard_id}/elements/{element_id}", response_model=dict)
async def delete_element(
    whiteboard_id: str,
    element_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Delete a canvas element"""
    db = _get_db()
    # Access check
    try:
        wb = await db.whiteboards.find_one({"_id": ObjectId(whiteboard_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid whiteboard ID")
    if not wb:
        raise HTTPException(status_code=404, detail="Whiteboard not found")

    if wb.get("project_id"):
        project = await db.projects.find_one({"_id": ObjectId(wb["project_id"])})
        if not project:
            raise HTTPException(status_code=404, detail="Associated project not found")
        role = current_user.get("role", "").lower()
        if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        if wb["created_by"] != current_user["id"]:
            raise HTTPException(status_code=403, detail="Only creator can edit")

    await db.whiteboards.update_one(
        {"_id": ObjectId(whiteboard_id)},
        {"$pull": {"elements": {"id": element_id}}, "$set": {"updated_at": datetime.now(timezone.utc)}}
    )
    return {"message": "Element deleted"}


@router.post("/{whiteboard_id}/elements/reorder", response_model=dict)
async def reorder_elements(
    whiteboard_id: str,
    element_ids: List[str],
    current_user: dict = Depends(get_current_active_user),
):
    """Reorder elements (z-index) by providing ordered list of element IDs"""
    db = _get_db()
    # Access check
    try:
        wb = await db.whiteboards.find_one({"_id": ObjectId(whiteboard_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid whiteboard ID")
    if not wb:
        raise HTTPException(status_code=404, detail="Whiteboard not found")

    if wb.get("project_id"):
        project = await db.projects.find_one({"_id": ObjectId(wb["project_id"])})
        if not project:
            raise HTTPException(status_code=404, detail="Associated project not found")
        role = current_user.get("role", "").lower()
        if role not in ("admin", "team_lead") and current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        if wb["created_by"] != current_user["id"]:
            raise HTTPException(status_code=403, detail="Only creator can edit")

    # Update z_index based on order
    updates = []
    for idx, el_id in enumerate(element_ids):
        updates.append({
            "filter": {"_id": ObjectId(whiteboard_id), "elements.id": el_id},
            "update": {"$set": {"elements.$.z_index": idx}}
        })

    for upd in updates:
        await db.whiteboards.update_one(upd["filter"], upd["update"])
    await db.whiteboards.update_one(
        {"_id": ObjectId(whiteboard_id)},
        {"$set": {"updated_at": datetime.now(timezone.utc)}}
    )
    return {"message": "Elements reordered"}
