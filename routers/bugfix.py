"""
AI Bug-Fix API Routes
"""
from fastapi import APIRouter, HTTPException, status, Depends
from typing import Dict, Any
from bson import ObjectId
from database import get_database
from dependencies import get_current_active_user
from services.bug_ticket_service import bug_ticket_service
from services.bugfix_workflow import bugfix_workflow
from models.bug import BugStatus
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai/bugfix", tags=["AI Bug Fix"])


@router.post("/{bug_id}")
async def trigger_bug_fix(
    bug_id: str,
    force: bool = False,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Trigger AI bug-fix workflow for a specific bug ticket.
    
    - **bug_id**: The MongoDB _id of the bug
    - **force**: If true, allows fixing bugs not in 'open' status (use with caution)
    
    Returns immediately with workflow status. Fix proceeds asynchronously.
    """
    # Permission check: only admin/team_lead/hr can trigger AI fix
    if current_user.get("role", "").lower() not in ["admin", "team_lead", "hr"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin/team_lead/hr can trigger AI bug fixes"
        )

    # Verify bug exists
    bug = await bug_ticket_service.get_bug_by_id(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail="Bug not found")

    # Additional permission: user must belong to bug's project (unless admin)
    if current_user.get("role", "").lower() != "admin":
        project_id = bug.get("project_id")
        db = get_database()
        project = await db.projects.find_one({"_id": ObjectId(project_id)})
        if not project or current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Not authorized for this project")

    # Start the workflow
    result = await bugfix_workflow.start_fix(bug_id, force=force)
    
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    return {
        "success": True,
        "bug_id": bug_id,
        "status": result.get("status", "starting"),
        "message": result.get("message", "AI fix started"),
    }


@router.get("/{bug_id}/status")
async def get_fix_status(
    bug_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Get the current status of an AI bug-fix workflow.
    Returns bug details + fix status if applicable.
    """
    bug = await bug_ticket_service.get_bug_by_id(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail="Bug not found")

    # Permission check
    if current_user.get("role", "").lower() not in ["admin", "team_lead", "hr"]:
        project = await get_database().projects.find_one({"_id": ObjectId(bug.get("project_id"))})
        if not project or current_user["id"] not in project.get("team", []):
            raise HTTPException(status_code=403, detail="Not authorized")

    response = {
        "bug_id": bug_id,
        "title": bug.get("title"),
        "status": bug.get("status"),
        "ai_agent_id": bug.get("ai_agent_id"),
        "fix_summary": bug.get("fix_summary"),
        "files_changed": bug.get("files_changed", []),
        "ai_confidence": bug.get("ai_confidence"),
        "fix_started_at": bug.get("fix_started_at"),
        "fix_completed_at": bug.get("fix_completed_at"),
    }

    # Determine if manual review needed
    confidence = bug.get("ai_confidence", 0)
    response["needs_manual_review"] = confidence < 0.8 if bug.get("status") == "fix_ready" else None

    return response


@router.post("/{bug_id}/approve")
async def approve_fix(
    bug_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Approve an AI-generated fix and create PR (when GitHub credentials configured).
    Only admin/team_lead can approve.
    """
    if current_user.get("role", "").lower() not in ["admin", "team_lead"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin/team_lead can approve fixes"
        )

    bug = await bug_ticket_service.get_bug_by_id(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail="Bug not found")

    if bug.get("status") != "fix_ready":
        raise HTTPException(
            status_code=400, 
            detail=f"Bug is not ready for approval (status: {bug.get('status')})"
        )

    # Mark bug as resolved
    await bug_ticket_service.update_status(bug_id, BugStatus.RESOLVED)

    return {
        "success": True,
        "message": "Fix approved. PR creation pending GitHub credentials configuration.",
        "status": "resolved",
    }
