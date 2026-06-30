from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from bson import ObjectId
from typing import Optional

from database import get_database
from models.leave import LeaveSettingsBase, LeaveSettingsUpdate, LeaveSettingsResponse
from dependencies import get_current_active_user
from models.user import UserInDB

router = APIRouter(prefix="/leave-settings", tags=["leave-settings"])


# GET /api/leave-settings - Get settings (public)
@router.get("", response_model=LeaveSettingsResponse)
async def get_leave_settings(year: Optional[int] = None, db=Depends(get_database)):
    if year is None:
        year = datetime.now(timezone.utc).year
    
    settings = await db["leave_settings"].find_one({"year": year})
    if settings:
        settings["_id"] = str(settings["_id"])
        return LeaveSettingsResponse(**settings)
    
    # Auto-clone from previous year if available
    prev_settings = await db["leave_settings"].find_one({"year": year - 1})
    if prev_settings:
        new_doc = {
            "year": year,
            "annual_leave_days": prev_settings.get("annual_leave_days", 18),
            "carry_forward_days": prev_settings.get("carry_forward_days", 0),
            "saturday_working": prev_settings.get("saturday_working", True),
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    else:
        new_doc = {
            "year": year,
            "annual_leave_days": 18,
            "carry_forward_days": 0,
            "saturday_working": True,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    result = await db["leave_settings"].insert_one(new_doc)
    new_doc["_id"] = result.inserted_id
    return LeaveSettingsResponse(**new_doc)


# PUT /api/leave-settings - Update settings (admin only)
@router.put("", response_model=LeaveSettingsResponse)
async def update_leave_settings(
    update_data: LeaveSettingsUpdate,
    year: Optional[int] = None,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user),
):
    if current_user.get("role", "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Only admin can update leave settings")
    
    if year is None:
        year = datetime.now(timezone.utc).year
    
    settings = await db["leave_settings"].find_one({"year": year})
    if not settings:
        new_doc = update_data.model_dump(exclude_unset=True)
        new_doc["year"] = year
        new_doc["created_at"] = datetime.now(timezone.utc)
        new_doc["updated_at"] = datetime.now(timezone.utc)
        if "annual_leave_days" not in new_doc:
            new_doc["annual_leave_days"] = 18
        if "carry_forward_days" not in new_doc:
            new_doc["carry_forward_days"] = 0
        if "saturday_working" not in new_doc:
            new_doc["saturday_working"] = True
        result = await db["leave_settings"].insert_one(new_doc)
        new_doc["_id"] = result.inserted_id
        return LeaveSettingsResponse(**new_doc)
    
    update_doc = {"updated_at": datetime.now(timezone.utc)}
    for field, value in update_data.model_dump(exclude_unset=True).items():
        update_doc[field] = value
    
    await db["leave_settings"].update_one(
        {"_id": settings["_id"]},
        {"$set": update_doc}
    )
    
    updated = await db["leave_settings"].find_one({"_id": settings["_id"]})
    updated["_id"] = str(updated["_id"])
    return LeaveSettingsResponse(**updated)
