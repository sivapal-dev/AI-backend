"""
BugFix Workflow Orchestrator
Coordinates the end-to-end AI bug-fixing process.
"""
import asyncio
from typing import Dict, Any, Optional
import logging
from datetime import datetime, timezone
from database import get_database
from config import get_settings
from services.bug_ticket_service import bug_ticket_service

settings = get_settings()
from agents.bugfix_agent import bugfix_agent
from services.email_service import email_service
from helpers.notification_sender import send_notification

logger = logging.getLogger(__name__)


class BugFixWorkflow:
    """
    Orchestrates the AI bug-fixing workflow:
    1. Trigger → 2. Analyze → 3. Fix → 4. Document → 5. Notify → 6. PR (future)
    """

    def __init__(self):
        self.running_tasks: Dict[str, str] = {}  # bug_id -> status

    async def start_fix(self, bug_id: str, force: bool = False) -> Dict[str, Any]:
        """
        Start the AI bug-fix workflow for a given ticket.
        This is the main entry point.
        """
        if bug_id in self.running_tasks:
            return {
                "success": False,
                "error": "Fix already in progress for this bug",
                "status": self.running_tasks[bug_id],
            }

        # Use atomic DB check to prevent race condition
        db = get_database()
        from bson import ObjectId
        from models.bug import BugStatus
        
        # Only start if status is not already FIX_IN_PROGRESS or if force is true
        query = {"_id": ObjectId(bug_id)}
        if not force:
            query["status"] = {"$ne": BugStatus.FIX_IN_PROGRESS.value}
            
        update = {
            "$set": {
                "status": BugStatus.FIX_IN_PROGRESS.value,
                "fix_started_at": datetime.now(timezone.utc)
            }
        }
        
        result = await db.bugs.find_one_and_update(query, update)
        if not result and not force:
            return {
                "success": False,
                "error": "Fix already in progress for this bug (database check)",
                "status": "in_progress",
            }

        # Mark as starting
        self.running_tasks[bug_id] = "starting"

        try:
            # Run the fix in background so we don't block the API response
            asyncio.create_task(self._run_workflow(bug_id, force))
            return {
                "success": True,
                "message": "AI fix started. You'll be notified when complete.",
                "status": "starting",
            }
        except Exception as e:
            logger.error(f"Failed to start fix for {bug_id}: {e}")
            self.running_tasks.pop(bug_id, None)
            return {"success": False, "error": str(e)}

    async def _run_workflow(self, bug_id: str, force: bool):
        """
        Internal: run the full workflow.
        """
        try:
            # Step 1: AI analyzes and applies fix
            result = await bugfix_agent.analyze_and_fix(bug_id, force=force)
            
            # Update running status
            self.running_tasks[bug_id] = "fix_ready" if result.get("success") else "fix_failed"

            # Step 2: Send email notification
            await self._send_notification(bug_id, result)

            # Step 3: Future — create PR when GitHub credentials available
            # await self._create_pr_if_enabled(bug_id, result)

            logger.info(f"Workflow complete for bug {bug_id}. Success: {result.get('success')}")

        except Exception as e:
            logger.error(f"Workflow failed for bug {bug_id}: {e}", exc_info=True)
            self.running_tasks[bug_id] = "fix_failed"
        finally:
            # Clean up running task after some time
            async def cleanup():
                await asyncio.sleep(300)  # Keep status for 5 min
                self.running_tasks.pop(bug_id, None)
            asyncio.create_task(cleanup())

    async def _send_notification(self, bug_id: str, fix_result: Dict[str, Any]):
        """
        Send email notification to bug assignee/reporter about the fix.
        """
        bug = await bug_ticket_service.get_bug_by_id(bug_id)
        if not bug:
            return

        # Get recipient email (assignee preferred, else reporter)
        recipient_email = await bug_ticket_service.get_assignee_email(bug_id)
        if not recipient_email:
            recipient_email = await bug_ticket_service.get_reporter_email(bug_id)
        
        if not recipient_email:
            logger.warning(f"No email found for bug {bug_id}")
            return

        # Build notification message
        if fix_result.get("success"):
            subject = f"✅ AI Fix Ready: {bug.get('title', 'Bug')}"
            body = f"""
The AI has analyzed and fixed the bug: **{bug.get('title')}**

**Fix Summary:**
{fix_result.get('summary', 'No summary available.')}

**Files Changed:**
{chr(10).join(f'- {f}' for f in fix_result.get('files_changed', []))}

**Confidence:** {fix_result.get('confidence', 0):.0%}
{'⚠️ This fix needs manual review.' if fix_result.get('needs_manual_review') else '✓ Fix appears confident.'}

View the bug ticket for details: {settings.frontend_url}/dashboard/bugs/{bug_id}
"""
        else:
            subject = f"❌ AI Fix Failed: {bug.get('title', 'Bug')}"
            body = f"""
The AI attempted to fix the bug but failed: **{bug.get('title')}**

**Error:** {fix_result.get('error', 'Unknown error')}

Please review the bug manually: {settings.frontend_url}/dashboard/bugs/{bug_id}
"""

        try:
            success = await email_service.send_notification_email(
                to_email=recipient_email,
                user_name=bug.get("assignee_name") or "Developer",
                notification_type="bug_fix",
                notification_title=subject,
                notification_message=body,
                action_link=f"/dashboard/bugs/{bug_id}",
            )
            if success:
                logger.info(f"Sent fix notification email to {recipient_email} for bug {bug_id}")
            else:
                logger.error(f"Failed to send notification email to {recipient_email}")
        except Exception as e:
            logger.error(f"Email sending failed: {e}")

    async def _create_pr_if_enabled(self, bug_id: str, fix_result: Dict[str, Any]):
        """
        Future: Create GitHub PR when credentials are configured.
        For now, just log.
        """
        if not fix_result.get("success"):
            return
        
        logger.info(f"PR creation deferred — GitHub credentials not yet configured. Bug {bug_id} fix ready.")


# Global workflow instance
bugfix_workflow = BugFixWorkflow()
