from fastapi import APIRouter, HTTPException, Depends, status
from typing import List
from bson import ObjectId
from datetime import datetime, timezone

from database import get_database
from dependencies import get_current_active_user
from models.custom_field import CustomFieldCreate, CustomFieldUpdate, CustomFieldDefinition

router = APIRouter(prefix="/custom-fields", tags=["Custom Fields"])


def _get_db():
    return get_database()


def _generate_field_id():
    import uuid
    return str(uuid.uuid4())[:8]


@router.get("/project/{project_id}")
async def get_project_custom_fields(
    project_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project.get("custom_fields", [])


@router.post("/project/{project_id}")
async def create_custom_field(
    project_id: str,
    data: CustomFieldCreate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    field = CustomFieldDefinition(
        id=_generate_field_id(),
        name=data.name,
        field_type=data.field_type,
        options=data.options,
        required=data.required,
        default_value=data.default_value,
        description=data.description,
    )

    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {
            "$push": {"custom_fields": field.model_dump()},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )
    return field


@router.put("/project/{project_id}/{field_id}")
async def update_custom_field(
    project_id: str,
    field_id: str,
    data: CustomFieldUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    custom_fields = project.get("custom_fields", [])
    existing_field = next((f for f in custom_fields if f.get("id") == field_id), None)
    if not existing_field:
        raise HTTPException(status_code=404, detail="Custom field not found")

    update_data = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    
    set_fields = {f"custom_fields.$.{k}": v for k, v in update_data.items()}
    set_fields["updated_at"] = datetime.now(timezone.utc)

    await db.projects.update_one(
        {"_id": ObjectId(project_id), "custom_fields.id": field_id},
        {"$set": set_fields}
    )
    
    return {**existing_field, **update_data}


@router.delete("/project/{project_id}/{field_id}")
async def delete_custom_field(
    project_id: str,
    field_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    project = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.get("role", "").lower() not in ["admin", "team_lead"] and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="Access denied")

    custom_fields = project.get("custom_fields", [])
    existing_field = next((f for f in custom_fields if f.get("id") == field_id), None)
    if not existing_field:
        raise HTTPException(status_code=404, detail="Custom field not found")

    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {
            "$pull": {"custom_fields": {"id": field_id}},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    # Also remove this field from all tasks in the project
    await db.tasks.update_many(
        {"project_id": project_id},
        {"$unset": {f"custom_field_values.{field_id}": ""}},
    )

    return {"success": True}
