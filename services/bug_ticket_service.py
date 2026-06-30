"""
Service for reading and updating bug tickets.
"""
from typing import Optional, Dict, Any, List
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from models.bug import BugStatus


class BugTicketService:
    """Handles all bug ticket operations for the AI bugfix workflow."""

    @staticmethod
    async def get_bug_by_id(bug_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a bug ticket by ID."""
        db = get_database()
        bug = await db.bugs.find_one({"_id": ObjectId(bug_id)})
        if bug:
            bug["_id"] = str(bug["_id"])
            if "reporter" in bug:
                bug["reporter"] = str(bug["reporter"])
            if "assignee" in bug and bug["assignee"]:
                bug["assignee"] = str(bug["assignee"])
        return bug

    @staticmethod
    async def get_project_id(bug_id: str) -> Optional[str]:
        """Get the project_id associated with a bug."""
        bug = await BugTicketService.get_bug_by_id(bug_id)
        return bug.get("project_id") if bug else None

    @staticmethod
    async def update_status(bug_id: str, status: BugStatus, extra_fields: Optional[Dict[str, Any]] = None) -> bool:
        """Update bug status and optionally other fields."""
        db = get_database()
        update_data = {"status": status.value, "updated_at": datetime.now(timezone.utc)}
        if extra_fields:
            ALLOWED_KEYS = {
                "ai_agent_id",
                "fix_started_at",
                "fix_summary",
                "files_changed",
                "ai_confidence",
                "fix_completed_at",
                "fix_failed_reason",
                "fix_failed_at",
                "project_id",
                "title",
                "description",
                "severity",
                "steps_to_reproduce",
                "expected_result",
                "actual_result",
                "environment",
                "is_regression",
                "attachments",
                "reporter",
                "assignee",
                "related_task",
                "custom_field_values",
                "updated_at",
            }
            invalid_keys = [k for k in extra_fields if k not in ALLOWED_KEYS]
            if invalid_keys:
                raise ValueError(f"Invalid fields in extra_fields: {invalid_keys}")
            update_data.update(extra_fields)
        result = await db.bugs.update_one({"_id": ObjectId(bug_id)}, {"$set": update_data})
        return result.modified_count > 0

    @staticmethod
    async def mark_fix_in_progress(bug_id: str, agent_id: str) -> bool:
        """Mark bug as being fixed by AI agent."""
        return await BugTicketService.update_status(
            bug_id, 
            BugStatus.FIX_IN_PROGRESS,
            {"ai_agent_id": agent_id, "fix_started_at": datetime.now(timezone.utc)}
        )

    @staticmethod
    async def mark_fix_ready(bug_id: str, fix_summary: str, files_changed: List[str], ai_confidence: float) -> bool:
        """Mark bug fix as ready for review."""
        return await BugTicketService.update_status(
            bug_id,
            BugStatus.FIX_READY,
            {
                "fix_summary": fix_summary,
                "files_changed": files_changed,
                "ai_confidence": ai_confidence,
                "fix_completed_at": datetime.now(timezone.utc),
            }
        )

    @staticmethod
    async def mark_fix_failed(bug_id: str, reason: str) -> bool:
        """Mark bug fix as failed."""
        return await BugTicketService.update_status(
            bug_id,
            BugStatus.FIX_FAILED,
            {"fix_failed_reason": reason, "fix_failed_at": datetime.now(timezone.utc)}
        )

    @staticmethod
    async def add_comment(bug_id: str, comment: str, author: str = "AI Agent") -> str:
        """Add a comment/note to the bug ticket."""
        db = get_database()
        comment_doc = {
            "content": comment,
            "entity_type": "bug",
            "entity_id": str(bug_id),
            "parent_id": None,
            "mentions": [],
            "author_id": "ai_agent",
            "author_name": author,
            "author_email": "",
            "author_role": "ai",
            "edited": False,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        result = await db.comments.insert_one(comment_doc)
        return str(result.inserted_id)

    @staticmethod
    async def get_assignee_email(bug_id: str) -> Optional[str]:
        """Get the email of the assignee (or reporter if unassigned)."""
        bug = await BugTicketService.get_bug_by_id(bug_id)
        if not bug:
            return None
        
        assignee_id = bug.get("assignee")
        if not assignee_id:
            # Fallback to reporter
            assignee_id = bug.get("reporter")
        
        if not assignee_id:
            return None
        
        db = get_database()
        user = await db.users.find_one({"_id": ObjectId(assignee_id)})
        return user.get("email") if user else None

    @staticmethod
    async def get_reporter_email(bug_id: str) -> Optional[str]:
        """Get the email of the bug reporter."""
        bug = await BugTicketService.get_bug_by_id(bug_id)
        if not bug:
            return None
        
        reporter_id = bug.get("reporter")
        if not reporter_id:
            return None
        
        db = get_database()
        user = await db.users.find_one({"_id": ObjectId(reporter_id)})
        return user.get("email") if user else None

    @staticmethod
    async def list_bugs_ready_for_fix(limit: int = 10) -> List[Dict[str, Any]]:
        """List bugs that are open and not yet being fixed."""
        db = get_database()
        query = {
            "status": {"$in": ["open", "in_progress"]},  # Bugs that need fixing
            "ai_agent_id": {"$exists": False}  # Not already being processed
        }
        cursor = db.bugs.find(query).sort("severity", -1).limit(limit)
        bugs = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            bugs.append(doc)
        return bugs


# Convenience instance
bug_ticket_service = BugTicketService()
