from fastapi import APIRouter, HTTPException, Depends
import logging
from typing import List, Optional
from bson import ObjectId
from datetime import datetime, timezone, timedelta
from database import get_database
from models.meeting import MeetingCreate, MeetingUpdate
from dependencies import get_current_active_user
from services.google_calendar_service import google_calendar_service
from helpers.notification_sender import send_notification

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["Meetings"])


def _get_db():
    return get_database()


@router.post("", response_model=dict)
async def create_meeting(
    meeting: MeetingCreate,
    current_user: dict = Depends(get_current_active_user),
):
    # Only admin and HR can create meetings
    if current_user.get("role", "").lower() not in ("admin", "hr"):
        raise HTTPException(
            status_code=403,
            detail="Only admin and HR can create meetings"
        )

    db = _get_db()
    print(f"[DEBUG] Meeting create request payload: {meeting.model_dump()}")

    # Automatically add project team members to attendees if project_id is set
    attendee_set = set(meeting.attendees)
    if meeting.project_id:
        try:
            project = await db.projects.find_one({"_id": ObjectId(meeting.project_id)})
            if project:
                attendee_set.update(project.get("team", []))
        except Exception as e:
            logger.warning(f"Failed to fetch project team members for project {meeting.project_id}: {e}")
    final_attendees = list(attendee_set)

    # Fetch email addresses of attendees to add them to the Google Calendar event
    attendee_emails = []
    if final_attendees:
        try:
            object_ids = []
            for uid in final_attendees:
                try:
                    object_ids.append(ObjectId(uid))
                except Exception:
                    pass
            if object_ids:
                users_cursor = db.users.find({"_id": {"$in": object_ids}}, {"email": 1})
                async for u in users_cursor:
                    if u.get("email"):
                        attendee_emails.append(u["email"])
        except Exception as e:
            logger.warning(f"Failed to fetch emails for attendees: {e}")

    # Determine if we should generate a Google Meet link
    meet_link = meeting.meet_link
    if meeting.generate_meet and not meet_link:
        try:
            # Build start/end datetime in ISO format with Z suffix
            # Assume the date+time are in UTC (frontend sends date as YYYY-MM-DD, time as HH:MM)
            try:
                start_dt = datetime.strptime(f"{meeting.date}T{meeting.time}", "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid date or time format")
            end_dt = start_dt + timedelta(minutes=meeting.duration)
            start_iso = start_dt.isoformat().replace("+00:00", "Z")
            end_iso = end_dt.isoformat().replace("+00:00", "Z")

            meet_result = await google_calendar_service.create_meeting_event(
                title=meeting.title,
                start_datetime=start_iso,
                end_datetime=end_iso,
                description=meeting.description or "",
                attendees=attendee_emails,  # Pass the fetched emails to invite guests
                user_id=current_user["id"],
            )
            meet_link = meet_result["meet_link"]
            google_event_id = meet_result.get("event_id")
        except Exception as e:
            # Don't fail the whole meeting creation if Meet generation fails, but log the error with traceback
            logger.error(f"Failed to generate Google Meet link: {e}", exc_info=True)
            meet_error = str(e)
            meet_link = None
            google_event_id = None
    else:
        google_event_id = None

    meeting_doc = {
        "title": meeting.title,
        "description": meeting.description,
        "meeting_type": meeting.meeting_type.value,
        "date": meeting.date,
        "time": meeting.time,
        "duration": meeting.duration,
        "location": meeting.location,
        "meet_link": meet_link,
        "google_event_id": google_event_id,
        "project_id": meeting.project_id,
        "attendees": final_attendees,
        "agenda": meeting.agenda,
        "notes": meeting.notes,
        "status": meeting.status,
        "reminder_sent": meeting.reminder_sent,
        "created_by": current_user["id"],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    try:
        result = await db.meetings.insert_one(meeting_doc)
    except Exception as db_exc:
        if google_event_id:
            try:
                await google_calendar_service.delete_meeting_event(google_event_id, user_id=current_user["id"])
            except Exception as cal_exc:
                logger.error(f"Failed to clean up Google Calendar event after DB failure: {cal_exc}")
        raise db_exc
    print(f"[DEBUG] MongoDB saved document: {meeting_doc}")

    # Create notifications for attendees
    for attendee_id in final_attendees:
        if attendee_id != current_user["id"]:
            await send_notification(
                user_id=attendee_id,
                type_="meeting_scheduled",
                title="New Meeting Scheduled",
                message=f"You have been invited to: {meeting.title}",
                entity_type="meeting",
                entity_id=str(result.inserted_id),
                link=f"/dashboard/meetings/{str(result.inserted_id)}",
            )

    # Log activity
    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "meeting_created",
            "entity_type": "meeting",
            "entity_id": str(result.inserted_id),
            "metadata": {"title": meeting.title},
            "created_at": datetime.now(timezone.utc),
        }
    )

    response_data = {"id": str(result.inserted_id), "message": "Meeting created successfully"}
    if meeting.generate_meet and not meet_link:
        response_data["warning"] = f"Failed to generate Google Meet link: {meet_error if 'meet_error' in locals() else 'Unknown error'}"
    return response_data


@router.get("")
async def list_meetings(
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    query = {}

    if project_id:
        query["project_id"] = project_id

    if status:
        query["status"] = status

    # Regular users see meetings they're attending, created, meetings for projects they're on, or public meetings
    if current_user.get("role", "").lower() != "admin":
        user_projects = await db.projects.find({"team": current_user["id"]}, {"_id": 1}).to_list(length=None)
        user_project_ids = [str(p["_id"]) for p in user_projects]
        query["$or"] = [
            {"created_by": current_user["id"]},
            {"attendees": current_user["id"]},
            {"project_id": {"$in": user_project_ids}},
            {"project_id": {"$in": [None, ""]}, "attendees": {"$in": [[], None]}},
        ]
    print(f"[DEBUG] Employee API query: {query}")

    cursor = db.meetings.find(query).sort("date", -1)
    meetings = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        meetings.append(doc)
    print(f"[DEBUG] Employee API response: {meetings}")
    return meetings


@router.get("/{meeting_id}")
async def get_meeting(
    meeting_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    meeting = await db.meetings.find_one({"_id": ObjectId(meeting_id)})
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    
    # Permission: admin, creator, attendee, project team member, or public meeting
    if current_user.get("role", "").lower() != "admin":
        is_creator = meeting.get("created_by") == current_user["id"]
        is_attendee = current_user["id"] in meeting.get("attendees", [])
        
        # Check project team membership
        is_team_member = False
        project_id = meeting.get("project_id")
        if project_id:
            project = await db.projects.find_one({"_id": ObjectId(project_id)})
            if project and current_user["id"] in project.get("team", []):
                is_team_member = True
                
        # Check if it is a public meeting (no project and no attendees)
        is_public = (not project_id) and (not meeting.get("attendees"))
                
        if not (is_creator or is_attendee or is_team_member or is_public):
            raise HTTPException(status_code=403, detail="Not authorized to view this meeting")
    
    meeting["_id"] = str(meeting["_id"])
    return meeting


@router.put("/{meeting_id}")
async def update_meeting(
    meeting_id: str,
    meeting_update: MeetingUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    # Only admin and HR can update meetings
    if current_user.get("role", "").lower() not in ("admin", "hr"):
        raise HTTPException(
            status_code=403, detail="Only admin and HR can update meetings"
        )

    db = _get_db()
    existing = await db.meetings.find_one({"_id": ObjectId(meeting_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Meeting not found")

    update_data = {
        k: v for k, v in meeting_update.model_dump().items() if v is not None
    }
    
    # Automatically add project team members to attendees if project_id or attendees list is updated
    project_id = update_data.get("project_id") or existing.get("project_id")
    attendees = update_data.get("attendees")
    if attendees is not None or "project_id" in update_data:
        attendee_set = set(attendees if attendees is not None else existing.get("attendees", []))
        if project_id:
            try:
                project = await db.projects.find_one({"_id": ObjectId(project_id)})
                if project:
                    attendee_set.update(project.get("team", []))
            except Exception as e:
                logger.warning(f"Failed to fetch project team members for project {project_id}: {e}")
        update_data["attendees"] = list(attendee_set)

    if "meeting_type" in update_data and update_data["meeting_type"]:
        update_data["meeting_type"] = update_data["meeting_type"].value
    update_data["updated_at"] = datetime.now(timezone.utc)

    await db.meetings.update_one({"_id": ObjectId(meeting_id)}, {"$set": update_data})

    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": "meeting_updated",
            "entity_type": "meeting",
            "entity_id": meeting_id,
            "metadata": {"title": existing.get("title")},
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"message": "Meeting updated successfully"}


@router.delete("/{meeting_id}")
async def delete_meeting(
    meeting_id: str, current_user: dict = Depends(get_current_active_user)
):
    # Only admin and HR can delete meetings
    if current_user.get("role", "").lower() not in ("admin", "hr"):
        raise HTTPException(
            status_code=403, detail="Only admin and HR can delete meetings"
        )

    db = _get_db()
    existing = await db.meetings.find_one({"_id": ObjectId(meeting_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Meeting not found")

    await db.meetings.delete_one({"_id": ObjectId(meeting_id)})
    return {"message": "Meeting deleted successfully"}


@router.post("/{meeting_id}/cancel")
async def cancel_meeting(
    meeting_id: str, current_user: dict = Depends(get_current_active_user)
):
    # Only admin and HR can cancel meetings
    if current_user.get("role", "").lower() not in ("admin", "hr"):
        raise HTTPException(
            status_code=403, detail="Only admin and HR can cancel meetings"
        )

    db = _get_db()
    existing = await db.meetings.find_one({"_id": ObjectId(meeting_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Meeting not found")

    # Notify attendees about cancellation
    for attendee_id in existing.get("attendees", []):
        if attendee_id != current_user["id"]:
            await send_notification(
                user_id=attendee_id,
                type_="meeting_cancelled",
                title="Meeting Cancelled",
                message=f"Meeting '{existing['title']}' has been cancelled",
                entity_type="meeting",
                entity_id=meeting_id,
                link=f"/dashboard/meetings/{meeting_id}",
            )

    await db.meetings.update_one(
        {"_id": ObjectId(meeting_id)},
        {"$set": {"status": "cancelled", "updated_at": datetime.now(timezone.utc)}},
    )

    return {"message": "Meeting cancelled successfully"}
