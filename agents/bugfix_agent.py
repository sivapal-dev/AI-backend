"""
AI Agent for analyzing and fixing bugs.
Uses OpenRouter API to understand bugs and propose fixes.
"""
from typing import Optional, Dict, Any, List, Tuple
import json
import logging
from ai_client import ai_client
from config import get_settings
from services.code_fixer import code_fixer
from services.bug_ticket_service import bug_ticket_service
from models.bug import BugStatus

logger = logging.getLogger(__name__)
settings = get_settings()


class BugFixAgent:
    """
    AI agent that analyzes bug tickets and applies code fixes.
    Works within project-scoped file access restrictions.
    """

    def __init__(self):
        self.model = settings.openrouter_default_model
        self.agent_id = "bugfix-agent-v1"

    async def analyze_and_fix(self, bug_id: str, force: bool = False) -> Dict[str, Any]:
        """
        Main entry point: analyze bug and apply fix.
        Returns a result dict with status, fix details, and confidence.
        """
        try:
            # Step 1: Fetch bug ticket
            bug = await bug_ticket_service.get_bug_by_id(bug_id)
            if not bug:
                return {"success": False, "error": f"Bug {bug_id} not found"}

            # Check if bug is in a fixable state
            if not force and bug.get("status") not in ["open", "in_progress"]:
                return {
                    "success": False, 
                    "error": f"Bug is not fixable (status: {bug.get('status')}). Use force=True to override."
                }

            # Step 2: Mark as in progress
            await bug_ticket_service.mark_fix_in_progress(bug_id, self.agent_id)
            logger.info(f"Started AI fix for bug {bug_id}: {bug.get('title')}")

            # Step 3: Gather context from codebase
            context = await self._gather_context(bug)
            
            # Step 4: AI analyzes and plans fix
            fix_plan = await self._plan_fix(bug, context)
            
            # Step 5: Apply fix to files
            applied = await self._apply_fix(fix_plan)
            
            if not applied["success"]:
                await bug_ticket_service.mark_fix_failed(bug_id, applied.get("error", "Unknown error"))
                return applied

            # Step 6: Document fix
            await bug_ticket_service.mark_fix_ready(
                bug_id,
                fix_summary=fix_plan["summary"],
                files_changed=applied["files_changed"],
                ai_confidence=fix_plan["confidence"]
            )

            # Step 7: Add comment to bug
            comment = self._format_comment(fix_plan, applied)
            await bug_ticket_service.add_comment(bug_id, comment, author="AI BugFix Agent")

            logger.info(f"AI fix completed for bug {bug_id}. Files changed: {applied['files_changed']}")

            return {
                "success": True,
                "bug_id": bug_id,
                "status": "fix_ready",
                "files_changed": applied["files_changed"],
                "summary": fix_plan["summary"],
                "confidence": fix_plan["confidence"],
                "needs_manual_review": fix_plan["confidence"] < 0.8,
            }

        except Exception as e:
            logger.error(f"AI fix failed for bug {bug_id}: {e}", exc_info=True)
            await bug_ticket_service.mark_fix_failed(bug_id, str(e))
            return {"success": False, "error": str(e)}

    async def _gather_context(self, bug: Dict[str, Any]) -> Dict[str, Any]:
        """
        Gather relevant codebase context for the bug.
        Reads project files, related code, and any referenced code locations.
        """
        context = {
            "bug": {
                "title": bug.get("title"),
                "description": bug.get("description", ""),
                "severity": bug.get("severity"),
                "steps_to_reproduce": bug.get("steps_to_reproduce", ""),
                "expected_result": bug.get("expected_result", ""),
                "actual_result": bug.get("actual_result", ""),
            },
            "project_id": bug.get("project_id"),
            "related_task": bug.get("related_task"),
            "files": [],  # Will contain file contents
        }

        # TODO: In full implementation, scan project directory for relevant files
        # For now, return empty files list - AI will request specific files as needed
        return context

    async def _plan_fix(self, bug: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Use OpenRouter AI to analyze the bug and plan a fix.
        Returns a fix plan with summary, changes needed, and confidence score.
        """
        system_prompt = """You are an expert software engineer tasked with fixing bugs in a codebase.
Your job is to:
1. Understand the bug from the description and context
2. Identify the likely root cause
3. Propose a precise, minimal fix
4. Explain why the fix works

Guidelines:
- Be conservative: if you're not confident (confidence < 0.8), say so
- Only modify files that are directly related to the bug
- Preserve existing code style and patterns
- Add comments if the fix is non-obvious
- Return your response as JSON with: { "summary": "...", "changes": [{ "file": "...", "old_code": "...", "new_code": "..." }], "confidence": 0.0-1.0, "reasoning": "..." }
"""

        user_prompt = f"""
Bug Title: {bug.get('title')}
Description: {bug.get('description', 'N/A')}
Steps to Reproduce: {bug.get('steps_to_reproduce', 'N/A')}
Expected: {bug.get('expected_result', 'N/A')}
Actual: {bug.get('actual_result', 'N/A')}
Severity: {bug.get('severity')}
Related Task: {bug.get('related_task', 'None')}

Project Context:
- Project ID: {bug.get('project_id')}
- Available files: {context.get('files', [])}

Analyze this bug and propose a fix. Return JSON only.
"""

        try:
            content = await ai_client.chat_completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=self.model,
                temperature=0.2,
            )
            fix_plan = json.loads(content)
            
            # Validate required fields (W185)
            required_keys = ["summary", "changes", "confidence", "reasoning"]
            missing_keys = [k for k in required_keys if k not in fix_plan]
            if missing_keys:
                logger.warning(f"AI response missing required keys: {missing_keys}. Content: {content}")
                return {
                    "summary": f"AI analysis failed - missing required keys: {', '.join(missing_keys)}",
                    "changes": [],
                    "confidence": 0.0,
                    "reasoning": f"AI response was invalid: missing keys {missing_keys}",
                }
            
            if not isinstance(fix_plan["changes"], list):
                logger.warning(f"AI response 'changes' field is not a list. Content: {content}")
                return {
                    "summary": "AI analysis failed - 'changes' field is not a list",
                    "changes": [],
                    "confidence": 0.0,
                    "reasoning": "AI response was invalid: 'changes' field must be a list",
                }
            
            return fix_plan

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response: {e}")
            return {
                "summary": "AI analysis failed - invalid response format",
                "changes": [],
                "confidence": 0.0,
                "reasoning": f"Parse error: {e}",
            }
        except Exception as e:
            logger.error(f"AI planning failed: {e}")
            raise

    async def _apply_fix(self, fix_plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply the AI-proposed fix to the actual files.
        Uses code_fixer to safely write changes.
        """
        changes = fix_plan.get("changes", [])
        if not changes:
            return {"success": False, "error": "No changes proposed by AI"}

        files_changed = []
        errors = []

        for change in changes:
            file_path = change.get("file")
            old_code = change.get("old_code", "")
            new_code = change.get("new_code", "")
            
            if not file_path or not new_code:
                logger.warning(f"Silently skipping invalid change proposed by AI: file_path={file_path}, new_code_length={len(new_code) if new_code else 0}")
                continue

            try:
                # Apply the change
                success = code_fixer.replace_code(file_path, old_code, new_code)
                if success:
                    files_changed.append(file_path)
                else:
                    errors.append(f"Failed to apply change to {file_path}")
            except Exception as e:
                errors.append(f"Error in {file_path}: {str(e)}")

        if errors:
            return {"success": False, "error": "; ".join(errors), "files_changed": files_changed}

        return {
            "success": True,
            "files_changed": files_changed,
            "changes_applied": len(files_changed),
        }

    def _format_comment(self, fix_plan: Dict[str, Any], applied: Dict[str, Any]) -> str:
        """Format a nice comment for the bug ticket."""
        comment = f"""🤖 **AI Bug Fix Applied**

**Summary:** {fix_plan.get('summary', 'Fix applied')}

**Confidence:** {fix_plan.get('confidence', 0):.0%}

**Files Changed:**
{chr(10).join(f'- `{f}`' for f in applied.get('files_changed', []))}

**Reasoning:**
{fix_plan.get('reasoning', 'No reasoning provided.')}

---
*This fix was automatically generated. Please review before merging.*
"""
        return comment


# Global agent instance
bugfix_agent = BugFixAgent()
