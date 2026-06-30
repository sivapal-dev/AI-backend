import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from bson import ObjectId

from dependencies import get_current_active_user, validate_object_id
from database import get_database
from models.ai_task_monitor import DailyCheckin, CheckinType, CheckinResponseStatus
from helpers.notification_sender import send_notification
from helpers.ai_checkin_generator import generate_morning_checkin, generate_evening_checkin
from helpers.ai_checkin_analyzer import analyze_response_repetition, analyze_remark_quality, analyze_remark_concern

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ai-daily-checkin"])


def _to_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)



@router.post("/ai/checkin/send-morning")
async def send_morning_checkins(
    request: Request,
):
    """
    Triggered by scheduler — sends morning check-in to all eligible users.
    """
    # 1. Check Service Token
    auth_header = request.headers.get("X-Service-Token")
    from config import get_settings
    settings = get_settings()
    is_service = False
    if auth_header and settings.jwt_secret and auth_header == settings.jwt_secret:
        is_service = True
        
    # 2. Check Admin session if not service
    if not is_service:
        try:
            from dependencies import get_current_user
            user = await get_current_user(request)
            if not user.get("email_verified", False):
                raise HTTPException(status_code=403, detail="Email not verified")
            if user.get("role", "").lower() != "admin":
                raise HTTPException(status_code=403, detail="Only administrators can trigger check-ins")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Not authenticated")

    db = get_database()
    cursor = db.users.find({})
    sent_count = 0
    async for user in cursor:

        user_id = str(user["_id"])
        try:
            question = await generate_morning_checkin(user)
            checkin = DailyCheckin(
                user_id=user_id,
                checkin_type=CheckinType.MORNING,
                question=question,
                status=CheckinResponseStatus.PENDING,
            )
            doc = checkin.model_dump(by_alias=True, exclude={"id"})
            result = await db.daily_checkins.insert_one(doc)
            await send_notification(
                user_id=user_id,
                type_="ai_checkin",
                title="Daily Check-in: Morning",
                message=question,
                entity_type="checkin",
                entity_id=str(result.inserted_id),
                link="/dashboard/ai/checkins",
            )
            sent_count += 1
        except Exception as e:
            logger.exception("Failed to send morning check-in to %s: %s", user_id, e)

    return {"sent": sent_count}


@router.post("/ai/checkin/send-evening")
async def send_evening_checkins(
    request: Request,
):
    """
    Triggered by scheduler — sends evening check-in to all eligible users.
    """
    # 1. Check Service Token
    auth_header = request.headers.get("X-Service-Token")
    from config import get_settings
    settings = get_settings()
    is_service = False
    if auth_header and settings.jwt_secret and auth_header == settings.jwt_secret:
        is_service = True
        
    # 2. Check Admin session if not service
    if not is_service:
        try:
            from dependencies import get_current_user
            user = await get_current_user(request)
            if not user.get("email_verified", False):
                raise HTTPException(status_code=403, detail="Email not verified")
            if user.get("role", "").lower() != "admin":
                raise HTTPException(status_code=403, detail="Only administrators can trigger check-ins")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Not authenticated")

    db = get_database()
    cursor = db.users.find({})
    sent_count = 0
    async for user in cursor:

        user_id = str(user["_id"])
        try:
            question = await generate_evening_checkin(user)
            checkin = DailyCheckin(
                user_id=user_id,
                checkin_type=CheckinType.EVENING,
                question=question,
                status=CheckinResponseStatus.PENDING,
            )
            doc = checkin.model_dump(by_alias=True, exclude={"id"})
            result = await db.daily_checkins.insert_one(doc)
            await send_notification(
                user_id=user_id,
                type_="ai_checkin",
                title="Daily Check-in: Evening",
                message=question,
                entity_type="checkin",
                entity_id=str(result.inserted_id),
                link="/dashboard/ai/checkins",
            )
            sent_count += 1
        except Exception as e:
            logger.exception("Failed to send evening check-in to %s: %s", user_id, e)

    return {"sent": sent_count}


@router.post("/ai/checkin/respond")
async def respond_to_checkin(
    payload: dict,
    current_user: dict = Depends(get_current_active_user),
):
    """
    User submits a check-in response within the allowed window.
    payload: {checkin_id: str, response: str}
    """
    db = get_database()
    checkin_id = payload.get("checkin_id")
    response_text = (payload.get("response") or "").strip()

    if not checkin_id or not response_text:
        raise HTTPException(status_code=400, detail="checkin_id and response are required")

    checkin = await db.daily_checkins.find_one({"_id": ObjectId(checkin_id)})
    if not checkin:
        raise HTTPException(status_code=404, detail="Check-in not found")

    if checkin["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="You can only respond to your own check-ins")

    if checkin["status"] != CheckinResponseStatus.PENDING:
        raise HTTPException(status_code=400, detail="Check-in is no longer pending")

    # Enforce time window: morning=20min, evening=45min from check-in creation
    now = datetime.now(timezone.utc)
    created = checkin["created_at"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if checkin["checkin_type"] == CheckinType.MORNING:
        deadline = created + timedelta(minutes=20)
    else:
        deadline = created + timedelta(minutes=45)
    if now > deadline:
        raise HTTPException(status_code=400, detail="Response window has closed. Please submit a remark instead.")

    now = datetime.now(timezone.utc)
    await db.daily_checkins.update_one(
        {"_id": ObjectId(checkin_id)},
        {
            "$set": {
                "response": response_text,
                "status": CheckinResponseStatus.RESPONDED,
                "responded_at": now,
            }
        },
    )

    asyncio.create_task(analyze_response_repetition(
        checkin_id=checkin_id,
        user_id=current_user["id"],
        checkin_type=checkin["checkin_type"],
        current_response=response_text,
        user_name=current_user.get("name", "A team member"),
    ))

    return {"status": "responded", "checkin_id": checkin_id}


@router.get("/ai/checkin/history")
async def get_checkin_history(
    current_user: dict = Depends(get_current_active_user),
    limit: int = 30,
):
    db = get_database()
    cursor = db.daily_checkins.find({"user_id": current_user["id"]}).sort("created_at", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    for item in items:
        item["_id"] = str(item["_id"])
        if "created_at" in item and item["created_at"]:
            item["created_at"] = _to_utc(item["created_at"])
        if "responded_at" in item and item["responded_at"]:
            item["responded_at"] = _to_utc(item["responded_at"])
    return items


@router.get("/ai/checkin/status/{date_str}")
async def get_checkin_status_for_date(
    date_str: str,  # YYYY-MM-DD
    current_user: dict = Depends(get_current_active_user),
):
    db = get_database()
    try:
        from datetime import datetime
        start = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")

    end = start.replace(hour=23, minute=59, second=59)

    cursor = db.daily_checkins.find(
        {
            "user_id": current_user["id"],
            "created_at": {"$gte": start, "$lte": end},
        }
    ).sort("created_at", 1)
    items = await cursor.to_list(length=10)
    for item in items:
        item["_id"] = str(item["_id"])
        if "created_at" in item and item["created_at"]:
            item["created_at"] = _to_utc(item["created_at"])
        if "responded_at" in item and item["responded_at"]:
            item["responded_at"] = _to_utc(item["responded_at"])
    return items


# ── Admin: Get all check-ins with user join ──────────────────────────────────

@router.get("/ai/checkin/admin/all")
async def get_all_checkins_admin(
    current_user: dict = Depends(get_current_active_user),
    status: Optional[str] = None,
    checkin_type: Optional[str] = None,
    user_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
):
    """
    Admin/TeamLead/HR only: Fetch all check-ins with user name joined.
    Supports filters: status, checkin_type, user_id, date range, pagination.
    """
    db = get_database()
    
    # Permission check
    role = current_user.get("role", "").lower()
    if role not in ["admin", "team_lead", "hr"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Build query
    query = {}
    if status:
        if status not in ["pending", "responded", "skipped"]:
            raise HTTPException(status_code=400, detail="Invalid status")
        query["status"] = status
    if checkin_type:
        if checkin_type not in ["morning", "evening"]:
            raise HTTPException(status_code=400, detail="Invalid checkin_type")
        query["checkin_type"] = checkin_type
    if user_id:
        query["user_id"] = user_id
    if date_from:
        try:
            from datetime import datetime
            start_dt = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            query["created_at"] = {"$gte": start_dt}
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_from format")
    if date_to:
        try:
            from datetime import datetime
            end_dt = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            if "created_at" in query:
                query["created_at"]["$lte"] = end_dt
            else:
                query["created_at"] = {"$lte": end_dt}
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_to format")

    # Fetch check-ins
    cursor = db.daily_checkins.find(query).sort("created_at", -1).skip(skip).limit(limit)
    checkins = await cursor.to_list(length=limit)

    # Get all unique user_ids to batch-fetch user names
    user_ids = list(set(c["user_id"] for c in checkins))
    # Validate and convert user_ids to ObjectIds, skipping any invalid
    valid_user_object_ids = []
    for uid in user_ids:
        try:
            valid_user_object_ids.append(validate_object_id(uid))
        except HTTPException:
            continue  # skip invalid user IDs
    users_cursor = db.users.find({"_id": {"$in": valid_user_object_ids}})
    users = await users_cursor.to_list(length=1000)
    user_map = {str(u["_id"]): u.get("name", "Unknown") for u in users}

    # Format response
    result = []
    for c in checkins:
        result.append({
            "id": str(c["_id"]),
            "user_id": c["user_id"],
            "user_name": user_map.get(c["user_id"], "Unknown"),
            "checkin_type": c["checkin_type"],
            "question": c["question"],
            "response": c.get("response"),
            "remark": c.get("remark"),
            "status": c.get("status", "pending"),
            "created_at": _to_utc(c["created_at"]),
            "responded_at": _to_utc(c["responded_at"]) if c.get("responded_at") else None,
        })

    return result


# ── Skip check-in with remark ────────────────────────────────────────────────

@router.post("/ai/checkin/skip")
async def skip_checkin(
    payload: dict,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Developer submits/updates a remark for a missed check-in (after window closes).
    Can be called multiple times to edit the remark.
    Only allowed if check-in is pending or already skipped, and window has passed.
    payload: {checkin_id: str, remark: str}
    """
    db = get_database()
    checkin_id = payload.get("checkin_id")
    remark = (payload.get("remark") or "").strip()

    if not checkin_id or not remark:
        raise HTTPException(status_code=400, detail="checkin_id and remark are required")

    checkin = await db.daily_checkins.find_one({"_id": ObjectId(checkin_id)})
    if not checkin:
        raise HTTPException(status_code=404, detail="Check-in not found")

    if checkin["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="You can only skip your own check-ins")

    # Only allow skip/remark if status is pending or already skipped
    if checkin["status"] not in [CheckinResponseStatus.PENDING, CheckinResponseStatus.SKIPPED]:
        raise HTTPException(status_code=400, detail="Cannot add remark to a responded check-in")

    # Enforce window must have passed
    now = datetime.now(timezone.utc)
    created = checkin["created_at"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if checkin["checkin_type"] == CheckinType.MORNING:
        deadline = created + timedelta(minutes=20)
    else:
        deadline = created + timedelta(minutes=45)
    if now < deadline:
        raise HTTPException(status_code=400, detail="Check-in window is still open. You can only submit a remark after the window closes.")

    now = datetime.now(timezone.utc)
    await db.daily_checkins.update_one(
        {"_id": ObjectId(checkin_id)},
        {
            "$set": {
                "remark": remark,
                "status": CheckinResponseStatus.SKIPPED,
                "responded_at": now,
            }
        },
    )

    asyncio.create_task(_analyze_after_skip(
        checkin_id=checkin_id,
        user_id=current_user["id"],
        remark=remark,
        user_name=current_user.get("name", "A team member"),
    ))

    return {"status": "skipped", "checkin_id": checkin_id}


async def _analyze_after_skip(checkin_id: str, user_id: str, remark: str, user_name: str) -> None:
    """Run quality check first; only check for concerns if remark is satisfactory."""
    satisfactory = await analyze_remark_quality(checkin_id, user_id, remark, user_name)
    if satisfactory:
        await analyze_remark_concern(checkin_id, user_id, remark, user_name)
