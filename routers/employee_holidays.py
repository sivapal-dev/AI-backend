from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from bson import ObjectId

from database import get_database
from dependencies import get_current_active_user

router = APIRouter(prefix="/employee/holiday-selections", tags=["employee-holiday-selections"])


class HolidaySelectionUpdate(BaseModel):
    year: int
    holiday_names: List[str]


# GET /api/employee/holiday-selections - Get employee's holiday selections for a year
@router.get("")
async def get_holiday_selections(
    year: Optional[int] = None,
    db=Depends(get_database),
    current_user: dict = Depends(get_current_active_user),
):
    if year is None:
        year = datetime.now(timezone.utc).year
    
    user = await db["users"].find_one({"_id": ObjectId(current_user["id"])})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    selections = user.get("emergency_holiday_selections", {})
    year_str = str(year)
    if year_str in selections:
        return {"year": year, "holiday_names": selections[year_str]}
    
    # Auto-clone from previous year if available
    prev_year_str = str(year - 1)
    if prev_year_str in selections:
        prev_names = selections[prev_year_str]
        # Validate that all names still exist for this year
        valid_names = await db["holidays"].distinct("name", {"year": year})
        cloned_names = [n for n in prev_names if n in valid_names]
        if len(cloned_names) > 10:
            cloned_names = cloned_names[:10]
        # Save cloned selections
        new_selections = dict(selections)
        new_selections[year_str] = cloned_names
        await db["users"].update_one(
            {"_id": ObjectId(current_user["id"])},
            {"$set": {"emergency_holiday_selections": new_selections, "updated_at": datetime.now(timezone.utc)}}
        )
        return {"year": year, "holiday_names": cloned_names}
    
    # No previous year; return empty list
    return {"year": year, "holiday_names": []}


# PUT /api/employee/holiday-selections - Update employee's holiday selections for a year
@router.put("")
async def update_holiday_selections(
    data: HolidaySelectionUpdate,
    db=Depends(get_database),
    current_user: dict = Depends(get_current_active_user),
):
    # Validate max 10
    if len(data.holiday_names) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 holidays can be selected")
    
    # Validate that all holiday names exist in DB for the given year
    valid_names = await db["holidays"].distinct("name", {"year": data.year})
    invalid = [n for n in data.holiday_names if n not in valid_names]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid holiday names: {', '.join(invalid)}"
        )
    
    # Update user's selections
    user_id = ObjectId(current_user["id"])
    year_str = str(data.year)
    selections = current_user.get("emergency_holiday_selections", {})
    selections[year_str] = data.holiday_names
    
    await db["users"].update_one(
        {"_id": user_id},
        {"$set": {"emergency_holiday_selections": selections, "updated_at": datetime.now(timezone.utc)}}
    )
    
    return {"year": data.year, "holiday_names": data.holiday_names}
