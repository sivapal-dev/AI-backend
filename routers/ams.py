from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status

from database import get_database
from dependencies import get_current_active_user, validate_object_id
from models.ams_ticket import AmsTicketCreate
from services.ams_service import ams_service

router = APIRouter(prefix="/ams", tags=["Automated AMS"])


@router.post("/tickets")
async def create_ams_ticket(
    payload: AmsTicketCreate,
    current_user: dict = Depends(get_current_active_user),
):
    db = get_database()
    project = await db.projects.find_one({"_id": validate_object_id(payload.project_id)})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    role = current_user.get("role", "").lower()
    if role != "admin" and current_user["id"] not in project.get("team", []):
        raise HTTPException(status_code=403, detail="You are not a member of this project")

    ticket = await ams_service.create_ticket(payload.model_dump(), current_user)
    if ticket.get("automation_enabled"):
        await ams_service.start_pipeline(ticket["_id"])
    return ticket


@router.get("/tickets")
async def list_ams_tickets(
    project_id: Optional[str] = None,
    current_user: dict = Depends(get_current_active_user),
):
    return await ams_service.list_tickets(current_user, project_id=project_id)


@router.get("/tickets/{ticket_id}")
async def get_ams_ticket(
    ticket_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    ticket = await ams_service.get_ticket(ticket_id, current_user)
    if not ticket:
        raise HTTPException(status_code=404, detail="AMS ticket not found")
    return ticket


@router.post("/tickets/{ticket_id}/run")
async def rerun_ams_pipeline(
    ticket_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    ticket = await ams_service.get_ticket(ticket_id, current_user)
    if not ticket:
        raise HTTPException(status_code=403, detail="You do not have access to this AMS ticket")

    await ams_service.start_pipeline(ticket_id)
    return {"success": True, "message": "Automated AMS rerun started"}


@router.post("/tickets/{ticket_id}/approve")
async def approve_ams_ticket(
    ticket_id: str,
    payload: Optional[dict] = None,
    current_user: dict = Depends(get_current_active_user),
):
    ticket = await ams_service.get_ticket(ticket_id, current_user)
    if not ticket:
        raise HTTPException(status_code=403, detail="You do not have access to this AMS ticket")

    try:
        result = await ams_service.approve_ticket(
            ticket_id,
            current_user,
            notes=(payload or {}).get("notes", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result
