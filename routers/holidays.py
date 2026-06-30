from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from bson import ObjectId

from database import get_database
from models.leave import HolidayCreate, HolidayUpdate, HolidayResponse
from dependencies import get_current_active_user
from models.user import UserInDB

router = APIRouter(prefix="/holidays", tags=["holidays"])


# GET /api/holidays - Get all holidays (optionally filter by year)
@router.get("", response_model=List[HolidayResponse])
async def get_holidays(
    year: Optional[int] = None,
    db=Depends(get_database)
):
    query = {}
    if year:
        query["year"] = year
    
    holidays = await db["holidays"].find(query).sort("date", 1).to_list(1000)
    now = datetime.now(timezone.utc)
    result = []
    for h in holidays:
        h_data = {
            **h,
            "_id": str(h["_id"]),
            "day": h.get("day", ""),
            "created_at": h.get("created_at", now),
            "updated_at": h.get("updated_at", now),
        }
        result.append(HolidayResponse(**h_data))
    return result


# POST /api/holidays - Create holiday (admin only)
@router.post("", response_model=HolidayResponse)
async def create_holiday(
    holiday_data: HolidayCreate,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    if current_user.get("role", "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Only admin can create holidays")
    
    # Check if holiday with same name and year already exists
    existing = await db["holidays"].find_one({
        "name": holiday_data.name,
        "year": holiday_data.year
    })
    if existing:
        raise HTTPException(status_code=400, detail="Holiday already exists for this year")
    
    holiday_doc = {
        "name": holiday_data.name,
        "date": holiday_data.date.isoformat(),
        "day": holiday_data.day,
        "year": holiday_data.year,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    
    result = await db["holidays"].insert_one(holiday_doc)
    holiday_doc["_id"] = str(result.inserted_id)
    
    return HolidayResponse(**holiday_doc)


# PUT /api/holidays/{id} - Update holiday (admin only)
@router.put("/{holiday_id}", response_model=HolidayResponse)
async def update_holiday(
    holiday_id: str,
    update_data: HolidayUpdate,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    if current_user.get("role", "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Only admin can update holidays")
    
    holiday = await db["holidays"].find_one({"_id": ObjectId(holiday_id)})
    if not holiday:
        raise HTTPException(status_code=404, detail="Holiday not found")
    
    update_doc = {"updated_at": datetime.now(timezone.utc)}
    for field, value in update_data.model_dump(exclude_unset=True).items():
        if field == "date" and value:
            update_doc[field] = value.isoformat()
        else:
            update_doc[field] = value
    
    await db["holidays"].update_one(
        {"_id": ObjectId(holiday_id)},
        {"$set": update_doc}
    )
    
    updated = await db["holidays"].find_one({"_id": ObjectId(holiday_id)})
    return HolidayResponse(**{**updated, "_id": str(updated["_id"])})


# DELETE /api/holidays/{id} - Delete holiday (admin only)
@router.delete("/{holiday_id}")
async def delete_holiday(
    holiday_id: str,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    if current_user.get("role", "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Only admin can delete holidays")
    
    result = await db["holidays"].delete_one({"_id": ObjectId(holiday_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Holiday not found")
    
    return {"message": "Holiday deleted"}
