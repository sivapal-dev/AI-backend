from datetime import datetime, date, timezone, timedelta
from typing import Optional, List, Set
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from bson import ObjectId
import logging

from database import get_database
from models.leave import LeaveCreate, LeaveUpdate, LeaveResponse, LeaveReject, LeaveType, LeaveStatus
from models.user import UserInDB, UserResponse
from dependencies import get_current_active_user
from services.google_calendar_service import google_calendar_service
from helpers.notification_sender import send_notification

router = APIRouter(prefix="/leave", tags=["leave"])


def calculate_leave_days(start: date, end: date, holiday_dates: List[date], saturday_working: bool = True) -> int:
    """Calculate working days between start and end, excluding weekends and holidays."""
    days = 0
    current = start
    while current <= end:
        if current.weekday() == 6:
            current += timedelta(days=1)
            continue
        if current.weekday() == 5 and not saturday_working:
            current += timedelta(days=1)
            continue
        if current not in holiday_dates:
            days += 1
        current += timedelta(days=1)
    return days


async def get_user_leave_balance(db, user_id: str) -> dict:
    """Get or initialize leave balance for a user, using admin policy for annual total."""
    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    current_year = datetime.now(timezone.utc).year
    policy = await db["leave_settings"].find_one({"year": current_year})
    policy_annual = policy.get("annual_leave_days", 18) if policy else 18
    
    balance = user.get("leave_balance", {})
    return {
        "annual_total": policy_annual,
        "annual_used": balance.get("annual_used", 0),
        "annual_pending": balance.get("annual_pending", 0),
        "emergency_total": balance.get("emergency_total", 10),
        "emergency_used": balance.get("emergency_used", 0),
        "emergency_pending": balance.get("emergency_pending", 0),
    }


async def update_leave_balance(db, user_id: str, leave_type: str, action: str, days: int = 1):
    """
    Update leave balance counters.
    action: 'create_pending', 'approve', 'reject', 'cancel'
    """
    delta_used = 0
    delta_pending = 0
    
    if action == "create_pending":
        delta_pending = days
    elif action == "approve":
        delta_pending = -days
        delta_used = days
    elif action == "reject" or action == "cancel":
        delta_pending = -days
    
    update_fields = {}
    if leave_type == "annual":
        if delta_pending:
            update_fields["leave_balance.annual_pending"] = delta_pending
        if delta_used:
            update_fields["leave_balance.annual_used"] = delta_used
    elif leave_type == "emergency":
        if delta_pending:
            update_fields["leave_balance.emergency_pending"] = delta_pending
        if delta_used:
            update_fields["leave_balance.emergency_used"] = delta_used
    
    if update_fields:
        await db["users"].update_one(
            {"_id": ObjectId(user_id)},
            {"$inc": update_fields}
        )


# GET /api/leave - My leave requests
@router.get("", response_model=List[LeaveResponse])
async def get_my_leaves(
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    leaves = await db["leaves"].find(
        {"user_id": ObjectId(current_user["id"])}
    ).sort("created_at", -1).to_list(100)
    
    result = []
    for leave in leaves:
        leave_data = {
            **leave,
            "_id": str(leave["_id"]),
            "user_id": str(leave["user_id"]),
            "approved_by": str(leave["approved_by"]) if leave.get("approved_by") else None,
        }
        result.append(LeaveResponse(**leave_data))
    return result


# POST /api/leave - Create leave request
@router.post("", response_model=LeaveResponse)
async def create_leave(
    leave_data: LeaveCreate,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    year = leave_data.start_date.year
    
    # Fetch holidays for the year (just dates)
    holidays_cursor = db["holidays"].find({"year": year})
    holidays = await holidays_cursor.to_list(1000)
    holiday_dates = []
    for h in holidays:
        d = h.get("date")
        if isinstance(d, str):
            holiday_dates.append(date.fromisoformat(d))
        elif isinstance(d, datetime):
            holiday_dates.append(d.date())
        elif isinstance(d, date):
            holiday_dates.append(d)
    
    # Check if Saturday is a working day
    policy = await db["leave_settings"].find_one({"year": year})
    saturday_working = policy.get("saturday_working", True) if policy else True
    
    # Calculate working days (excludes weekends & all holidays)
    days = calculate_leave_days(leave_data.start_date, leave_data.end_date, holiday_dates, saturday_working)
    if days <= 0:
        raise HTTPException(status_code=400, detail="No valid working days in selected range")
    
    # Check balance
    balance = await get_user_leave_balance(db, current_user["id"])
    if leave_data.leave_type == LeaveType.ANNUAL:
        remaining = balance["annual_total"] - balance["annual_used"] - balance["annual_pending"]
        if days > remaining:
            raise HTTPException(status_code=400, detail=f"Insufficient annual leave balance. Remaining: {remaining} days")
    else:  # emergency
        remaining = balance["emergency_total"] - balance["emergency_used"] - balance["emergency_pending"]
        if days > remaining:
            raise HTTPException(status_code=400, detail=f"Insufficient emergency leave balance. Remaining: {remaining} days")
    
    # Create leave document
    leave_doc = {
        "user_id": ObjectId(current_user["id"]),
        "user_name": current_user["name"],
        "leave_type": leave_data.leave_type.value,
        "start_date": leave_data.start_date.isoformat(),
        "end_date": leave_data.end_date.isoformat(),
        "days": days,
        "status": LeaveStatus.PENDING.value,
        "reason": leave_data.reason,
        "emergency_dates": None,  # No longer used
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    
    result = await db["leaves"].insert_one(leave_doc)
    leave_doc["_id"] = result.inserted_id
    
    # Increment pending balance
    await update_leave_balance(db, current_user["id"], leave_data.leave_type.value, "create_pending", days)
    
    response = {
        **leave_doc,
        "_id": str(leave_doc["_id"]),
        "user_id": str(leave_doc["user_id"]),
    }
    return LeaveResponse(**response)


# PUT /api/leave/{id} - Update own pending leave
@router.put("/{leave_id}", response_model=LeaveResponse)
async def update_leave(
    leave_id: str,
    update_data: LeaveUpdate,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    leave = await db["leaves"].find_one({"_id": ObjectId(leave_id), "user_id": ObjectId(current_user["id"])})
    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")
    
    if leave["status"] != LeaveStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="Can only edit pending leave requests")
    
    update_doc = {"updated_at": datetime.now(timezone.utc)}
    
    if update_data.start_date:
        update_doc["start_date"] = update_data.start_date.isoformat()
    if update_data.end_date:
        update_doc["end_date"] = update_data.end_date.isoformat()
    if update_data.reason:
        update_doc["reason"] = update_data.reason
    
    # Recalculate days if dates changed
    days_diff = 0
    if update_data.start_date or update_data.end_date:
        start = update_data.start_date or date.fromisoformat(leave["start_date"])
        end = update_data.end_date or date.fromisoformat(leave["end_date"])
        
        year = start.year
        holidays_cursor = db["holidays"].find({"year": year})
        holidays = await holidays_cursor.to_list(1000)
        holiday_dates = []
        for h in holidays:
            d = h.get("date")
            if isinstance(d, str):
                holiday_dates.append(date.fromisoformat(d))
            elif isinstance(d, datetime):
                holiday_dates.append(d.date())
            elif isinstance(d, date):
                holiday_dates.append(d)
        
        policy = await db["leave_settings"].find_one({"year": year})
        saturday_working = policy.get("saturday_working", True) if policy else True
        
        # Calculate working days (excludes weekends & all holidays)
        new_days = calculate_leave_days(start, end, holiday_dates, saturday_working)
        
        # Check if the new days exceed the remaining balance
        if new_days > leave["days"]:
            balance = await get_user_leave_balance(db, current_user["id"])
            if leave["leave_type"] == LeaveType.ANNUAL.value:
                remaining = balance["annual_total"] - balance["annual_used"] - balance["annual_pending"]
            else:
                remaining = balance["emergency_total"] - balance["emergency_used"] - balance["emergency_pending"]
                
            if (new_days - leave["days"]) > remaining:
                raise HTTPException(status_code=400, detail="Insufficient leave balance for the extended dates")
                
        update_doc["days"] = new_days
        days_diff = new_days - leave["days"]
    
    await db["leaves"].update_one({"_id": ObjectId(leave_id)}, {"$set": update_doc})
    
    if days_diff != 0:
        await update_leave_balance(db, str(current_user["id"]), leave["leave_type"], "create_pending", days_diff)
        
    updated_leave = await db["leaves"].find_one({"_id": ObjectId(leave_id)})
    response_data = {
        **updated_leave,
        "_id": str(updated_leave["_id"]),
        "user_id": str(updated_leave["user_id"]),
        "approved_by": str(updated_leave["approved_by"]) if updated_leave.get("approved_by") else None,
    }
    return LeaveResponse(**response_data)


# DELETE /api/leave/{id} - Cancel own pending leave
@router.delete("/{leave_id}")
async def delete_leave(
    leave_id: str,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    leave = await db["leaves"].find_one({"_id": ObjectId(leave_id), "user_id": ObjectId(current_user["id"])})
    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")
    
    if leave["status"] != LeaveStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="Can only cancel pending leave requests")
    
    # Delete Google Calendar event if it exists
    google_event_id = leave.get("google_event_id")
    if google_event_id:
        try:
            await google_calendar_service.delete_leave_event(google_event_id)
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to delete Google Calendar event: {e}")
    
    await db["leaves"].delete_one({"_id": ObjectId(leave_id)})
    
    # Decrement pending balance (pass actual days so correct amount is reversed)
    await update_leave_balance(db, current_user["id"], leave["leave_type"], "cancel", leave["days"])
    
    return {"message": "Leave request cancelled"}


# GET /api/leave/all - All leave requests (admin/hr only)
@router.get("/all", response_model=List[LeaveResponse])
async def get_all_leaves(
    status: Optional[str] = None,
    user_id: Optional[str] = None,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    if current_user.get("role", "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only admin or HR can view all leave requests")
    
    query = {}
    if status:
        query["status"] = status
    if user_id:
        query["user_id"] = ObjectId(user_id)
    
    leaves = await db["leaves"].find(query).sort("created_at", -1).to_list(100)
    
    result = []
    for leave in leaves:
        leave_data = {
            **leave,
            "_id": str(leave["_id"]),
            "user_id": str(leave["user_id"]),
            "approved_by": str(leave["approved_by"]) if leave.get("approved_by") else None,
        }
        result.append(LeaveResponse(**leave_data))
    return result


# PUT /api/leave/{id}/approve - Approve leave (admin/hr only)
@router.put("/{leave_id}/approve")
async def approve_leave(
    leave_id: str,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    if current_user.get("role", "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only admin or HR can approve leave")
    
    leave = await db["leaves"].find_one({"_id": ObjectId(leave_id)})
    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")
    
    if leave["status"] != LeaveStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="Leave already processed")
    
    await db["leaves"].update_one(
        {"_id": ObjectId(leave_id)},
        {
            "$set": {
                "status": LeaveStatus.APPROVED.value,
                "approved_by": ObjectId(current_user["id"]),
                "approved_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        }
    )
    
    # Update balance: move from pending to used
    await update_leave_balance(db, str(leave["user_id"]), leave["leave_type"], "approve", leave["days"])
    
    # Send notification
    try:
        await send_notification(
            user_id=str(leave["user_id"]),
            type_="leave_approved",
            title="Leave Request Approved",
            message=f"Your leave request for {leave['days']} days ({leave['start_date']} to {leave['end_date']}) has been approved.",
            entity_type="leave",
            entity_id=str(leave_id),
            link="/dashboard/leave",
        )
    except Exception as e:
        logger.error(f"Failed to send leave approval notification: {e}")

    # Log activity
    try:
        await db["activity_logs"].insert_one(
            {
                "user_id": current_user["id"],
                "action": "leave_approved",
                "entity_type": "leave",
                "entity_id": str(leave_id),
                "metadata": {
                    "employee_id": str(leave["user_id"]),
                    "employee_name": leave.get("user_name", "Unknown"),
                    "days": leave["days"],
                    "start_date": leave["start_date"],
                    "end_date": leave["end_date"],
                },
                "created_at": datetime.now(timezone.utc),
            }
        )
    except Exception as e:
        logger.error(f"Failed to log leave approval activity: {e}")
    
    # Sync to Google Calendar (fire and forget — don't block approval)
    try:
        updated_leave = await db["leaves"].find_one({"_id": ObjectId(leave_id)})
        if updated_leave:
            # Get user name for event title
            user = await db["users"].find_one({"_id": leave["user_id"]})
            user_name = user.get("name", "Unknown") if user else "Unknown"
            
            google_event_id = await google_calendar_service.create_leave_event(
                leave_id=str(leave_id),
                user_name=user_name,
                leave_type=leave["leave_type"],
                start_date=leave["start_date"],
                end_date=leave["end_date"],
                reason=leave.get("reason", ""),
                days=leave["days"],
            )
            # Store google_event_id for later deletion
            await db["leaves"].update_one(
                {"_id": ObjectId(leave_id)},
                {"$set": {"google_event_id": google_event_id}}
            )
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to sync leave to Google Calendar: {e}")
        # Don't fail the approval if calendar sync fails
    
    return {"message": "Leave approved"}


# PUT /api/leave/{id}/reject - Reject leave (admin/hr only)
@router.put("/{leave_id}/reject")
async def reject_leave(
    leave_id: str,
    data: LeaveReject,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    if current_user.get("role", "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only admin or HR can reject leave")
    
    leave = await db["leaves"].find_one({"_id": ObjectId(leave_id)})
    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")
    
    if leave["status"] != LeaveStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="Leave already processed")
    
    await db["leaves"].update_one(
        {"_id": ObjectId(leave_id)},
        {
            "$set": {
                "status": LeaveStatus.REJECTED.value,
                "rejected_reason": data.reason,
                "updated_at": datetime.now(timezone.utc),
            }
        }
    )
    
    # Decrement pending balance
    await update_leave_balance(db, str(leave["user_id"]), leave["leave_type"], "reject", leave["days"])
    
    # Send notification
    try:
        await send_notification(
            user_id=str(leave["user_id"]),
            type_="leave_rejected",
            title="Leave Request Rejected",
            message=f"Your leave request for {leave['days']} days ({leave['start_date']} to {leave['end_date']}) has been rejected. Reason: {data.reason}",
            entity_type="leave",
            entity_id=str(leave_id),
            link="/dashboard/leave",
        )
    except Exception as e:
        logger.error(f"Failed to send leave rejection notification: {e}")

    # Log activity
    try:
        await db["activity_logs"].insert_one(
            {
                "user_id": current_user["id"],
                "action": "leave_rejected",
                "entity_type": "leave",
                "entity_id": str(leave_id),
                "metadata": {
                    "employee_id": str(leave["user_id"]),
                    "employee_name": leave.get("user_name", "Unknown"),
                    "days": leave["days"],
                    "start_date": leave["start_date"],
                    "end_date": leave["end_date"],
                    "rejected_reason": data.reason,
                },
                "created_at": datetime.now(timezone.utc),
            }
        )
    except Exception as e:
        logger.error(f"Failed to log leave rejection activity: {e}")
    
    # Delete Google Calendar event if it exists
    google_event_id = leave.get("google_event_id")
    if google_event_id:
        try:
            await google_calendar_service.delete_leave_event(google_event_id)
            # Clear the field
            await db["leaves"].update_one(
                {"_id": ObjectId(leave_id)},
                {"$unset": {"google_event_id": ""}}
            )
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to delete Google Calendar event: {e}")
    
    return {"message": "Leave rejected"}


# GET /api/leave/team - Team calendar data (all authenticated users)
@router.get("/team")
async def get_team_leaves(
    month: Optional[int] = None,
    year: Optional[int] = None,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    # All authenticated users can view team leave calendar
    now = datetime.now(timezone.utc)
    query_month = month or now.month
    query_year = year or now.year
    
    # Get start and end of month
    from_date = date(query_year, query_month, 1)
    if query_month == 12:
        to_date = date(query_year + 1, 1, 1) - timedelta(days=1)
    else:
        to_date = date(query_year, query_month + 1, 1) - timedelta(days=1)
    
    # Find approved/ pending leaves that overlap with this month
    leaves = await db["leaves"].find({
        "status": {"$in": ["pending", "approved"]},
        "start_date": {"$lte": to_date.isoformat()},
        "end_date": {"$gte": from_date.isoformat()},
    }).to_list(1000)
    
    # Format: { "2026-01-15": [{ "user_name": "John", "type": "annual" }] }
    calendar = {}
    for leave in leaves:
        start = date.fromisoformat(leave["start_date"])
        end = date.fromisoformat(leave["end_date"])
        current = max(start, from_date)
        while current <= min(end, to_date):
            date_str = current.isoformat()
            if date_str not in calendar:
                calendar[date_str] = []
            calendar[date_str].append({
                "user_name": leave["user_name"],
                "leave_type": leave["leave_type"],
                "status": leave["status"],
                "days": leave["days"],
            })
            current += timedelta(days=1)
    
    return {"month": query_month, "year": query_year, "calendar": calendar}


# GET /api/leave/balance - My leave balance
@router.get("/balance")
async def get_my_balance(
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    balance = await get_user_leave_balance(db, current_user["id"])
    return balance


# GET /api/leave/balance/all - All users' balances (admin/hr/team_lead)
# MUST come before /balance/{user_id} to avoid "all" being parsed as user_id
@router.get("/balance/all")
async def get_all_balances(
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    if current_user["role"] not in ("admin", "hr", "team_lead"):
        raise HTTPException(status_code=403, detail="Only admin, HR, or Team Lead can view all balances")
    
    current_year = datetime.now(timezone.utc).year
    policy = await db["leave_settings"].find_one({"year": current_year})
    policy_annual = policy.get("annual_leave_days", 18) if policy else 18

    # Scope: team_lead can only see users in their projects
    if current_user["role"] == "team_lead":
        projects = await db["projects"].find({"team": current_user["id"]}, {"team": 1}).to_list(1000)
        team_member_ids = set()
        for proj in projects:
            for uid in proj.get("team", []):
                team_member_ids.add(uid)
        team_member_ids.discard(current_user["id"])
        if not team_member_ids:
            return []
        valid_oids = []
        for uid in team_member_ids:
            try:
                valid_oids.append(ObjectId(uid))
            except Exception:
                continue
        users = await db["users"].find({"_id": {"$in": valid_oids}}).to_list(1000)
    else:
        users = await db["users"].find({"role": {"$ne": "admin"}}).to_list(1000)
    result = []
    for user in users:
        balance = user.get("leave_balance", {})
        result.append({
            "user_id": str(user["_id"]),
            "name": user["name"],
            "email": user["email"],
            "role": user["role"],
            "annual_total": policy_annual,
            "annual_used": balance.get("annual_used", 0),
            "annual_pending": balance.get("annual_pending", 0),
            "annual_remaining": policy_annual - balance.get("annual_used", 0) - balance.get("annual_pending", 0),
            "emergency_total": balance.get("emergency_total", 10),
            "emergency_used": balance.get("emergency_used", 0),
            "emergency_pending": balance.get("emergency_pending", 0),
            "emergency_remaining": 10 - balance.get("emergency_used", 0) - balance.get("emergency_pending", 0),
        })
    return result


# GET /api/leave/balance/{user_id} - Specific user's balance (admin/hr only)
@router.get("/balance/{user_id}")
async def get_user_balance(
    user_id: str,
    db=Depends(get_database),
    current_user: UserInDB = Depends(get_current_active_user)
):
    if current_user.get("role", "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only admin or HR can view other users' balance")
    balance = await get_user_leave_balance(db, user_id)
    return balance
