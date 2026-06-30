import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId

from ai_client import AIClientError, ai_client
from database import get_database
from dependencies import validate_object_id
from helpers.notification_sender import send_notification
from models.ams_ticket import AmsApprovalStatus, AmsAutomationStatus, AmsTicketStatus
from services.code_fixer import PROJECT_ROOT, code_fixer
from services.email_service import email_service
from services.git_service import git_service

from config import get_settings
from utils.encryption import decrypt_password

logger = logging.getLogger(__name__)
settings = get_settings()

AMS_MODEL = settings.openrouter_default_model or "poolside/laguna-m.1:free"
CHANGELOG_PATH = "CHANGELOG.md"
_changelog_lock = asyncio.Lock()


class AMSService:
    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}

    async def create_ticket(self, payload: dict, reporter: dict) -> dict:
        db = get_database()
        ticket_key = await self._generate_ticket_key()
        now = datetime.now(timezone.utc)
        linked_bug_id = await self._create_linked_bug(payload, reporter)
        reporter_settings = reporter.get("settings", {}) or {}
        automation_enabled = payload.get("automation_enabled")
        if automation_enabled is None:
            automation_enabled = reporter_settings.get("ams_enabled", True)

        doc = {
            "ticket_key": ticket_key,
            "project_id": payload["project_id"],
            "title": payload["title"].strip(),
            "description": payload["description"].strip(),
            "priority": payload.get("priority", "medium"),
            "steps_to_reproduce": payload.get("steps_to_reproduce", "").strip(),
            "expected_result": payload.get("expected_result", "").strip(),
            "actual_result": payload.get("actual_result", "").strip(),
            "module_hint": payload.get("module_hint", "").strip(),
            "affected_platform": payload.get("affected_platform", "").strip(),
            "stakeholder_emails": payload.get("stakeholder_emails", []),
            "automation_enabled": automation_enabled,
            "reporter_id": reporter["id"],
            "reporter_name": reporter.get("name", reporter.get("email", "Unknown")),
            "linked_bug_id": linked_bug_id,
            "status": AmsTicketStatus.OPEN.value,
            "automation_status": (
                AmsAutomationStatus.QUEUED.value
                if automation_enabled
                else AmsAutomationStatus.IDLE.value
            ),
            "approval_status": AmsApprovalStatus.PENDING.value,
            "approved_by": None,
            "approved_at": None,
            "approval_notes": None,
            "ai_provider": "openrouter",
            "ai_model": AMS_MODEL,
            "changelog_path": CHANGELOG_PATH,
            "analysis": None,
            "patch": None,
            "documentation": None,
            "notification": None,
            "pull_request": None,
            "timeline": [
                self._event(
                    "ticket_created",
                    "completed",
                    f"Internal AMS ticket {ticket_key} created.",
                    metadata={"linked_bug_id": linked_bug_id},
                )
            ],
            "last_run_at": None,
            "completed_at": None,
            "failed_reason": None,
            "created_at": now,
            "updated_at": now,
        }
        result = await db.ams_tickets.insert_one(doc)
        doc["_id"] = str(result.inserted_id)
        return doc

    async def approve_ticket(self, ticket_id: str, approver: dict, notes: str = "") -> dict:
        db = get_database()
        doc = await db.ams_tickets.find_one({"_id": ObjectId(ticket_id)})
        if not doc:
            raise ValueError("AMS ticket not found")
        if doc.get("status") != AmsTicketStatus.REVIEW_READY.value:
            raise ValueError(f"Ticket is not ready for approval (status: {doc.get('status')})")

        ticket = self._normalize(doc)
        project_doc = await db.projects.find_one({"_id": ObjectId(ticket["project_id"])}) or {}
        
        # Fetch fresh approver doc to get the github_access_token if connected
        approver_doc = await db.users.find_one({"_id": ObjectId(approver["id"])}) or {}
        
        approval_started_at = datetime.now(timezone.utc)
        changelog_entry = (ticket.get("documentation") or {}).get("changelog_entry", "").strip()
        changelog_path = await self._append_changelog(ticket, changelog_entry)
        git_artifact = await self._prepare_git_artifacts(ticket, changelog_path, project_doc, approver_doc)

        if git_artifact.get("remote_push_error"):
            await self._merge_ticket(
                ticket_id,
                {
                    "status": AmsTicketStatus.FAILED.value,
                    "failed_reason": f"GitHub push failed: {git_artifact['remote_push_error']}",
                },
                self._event(
                    "git_push_failed",
                    "failed",
                    f"GitHub push failed: {git_artifact['remote_push_error']}",
                    duration_ms=self._elapsed_ms(approval_started_at),
                    metadata=git_artifact,
                ),
            )
            raise ValueError(f"GitHub push failed: {git_artifact['remote_push_error']}")

        if ticket.get("linked_bug_id"):
            await self._sync_linked_bug_resolution(ticket, approver, notes)

        pull_request = ticket.get("pull_request") or {}
        pull_request.update(
            {
                "status": "approved",
                "branch_name": git_artifact.get("branch_name") or pull_request.get("branch_name"),
                "commit_sha": git_artifact.get("commit_sha"),
                "commit_message": git_artifact.get("commit_message"),
                "base_branch": git_artifact.get("base_branch"),
            }
        )

        await self._merge_ticket(
            ticket_id,
            {
                "status": AmsTicketStatus.COMPLETED.value,
                "approval_status": AmsApprovalStatus.APPROVED.value,
                "approved_by": approver["id"],
                "approved_at": datetime.now(timezone.utc),
                "approval_notes": notes.strip() or None,
                "changelog_path": changelog_path,
                "pull_request": pull_request,
            },
            self._event(
                "approval_completed",
                "completed",
                "AMS ticket approved. Changelog updated and local git artifact prepared.",
                duration_ms=self._elapsed_ms(approval_started_at),
                metadata=git_artifact,
            ),
        )

        return {
            "success": True,
            "ticket_id": ticket_id,
            "changelog_path": changelog_path,
            "git_artifact": git_artifact,
        }

    async def list_tickets(self, current_user: dict, project_id: Optional[str] = None) -> List[dict]:
        db = get_database()
        query: Dict[str, Any] = {}
        role = current_user.get("role", "").lower()

        if role != "admin":
            user_id = str(current_user["id"])
            user_oid = ObjectId(user_id) if ObjectId.is_valid(user_id) else None
            team_query = [user_id]
            if user_oid:
                team_query.append(user_oid)
            user_projects = await db.projects.find(
                {"team": {"$in": team_query}},
                {"_id": 1},
            ).to_list(length=500)
            allowed_project_ids = [str(item["_id"]) for item in user_projects]
            if not allowed_project_ids:
                return []
            query["project_id"] = {"$in": allowed_project_ids}

        if project_id:
            # Narrow to that project only if the user is allowed to access it
            if role != "admin" and project_id not in allowed_project_ids:
                return []
            query["project_id"] = project_id

        tickets = []
        cursor = db.ams_tickets.find(query).sort("created_at", -1)
        async for doc in cursor:
            tickets.append(self._normalize(doc))
        return tickets

    async def get_ticket(self, ticket_id: str, user: dict) -> Optional[dict]:
        db = get_database()
        doc = await db.ams_tickets.find_one({"_id": validate_object_id(ticket_id)})
        if not doc:
            return None

        ticket = self._normalize(doc)
        if user.get("role", "").lower() == "admin":
            return ticket

        project = await db.projects.find_one({"_id": validate_object_id(ticket["project_id"])})
        if project:
            user_id = str(user["id"])
            team = [str(t) for t in project.get("team", [])]
            if user_id in team:
                return ticket
        return None

    async def start_pipeline(self, ticket_id: str) -> None:
        if ticket_id in self._tasks and not self._tasks[ticket_id].done():
            return
        task = asyncio.create_task(self._run_pipeline(ticket_id))
        self._tasks[ticket_id] = task

    async def _run_pipeline(self, ticket_id: str) -> None:
        db = get_database()
        try:
            ticket = await db.ams_tickets.find_one({"_id": validate_object_id(ticket_id)})
            if not ticket:
                return

            await self._merge_ticket(
                ticket_id,
                {
                    "status": AmsTicketStatus.ANALYZING.value,
                    "automation_status": AmsAutomationStatus.RUNNING.value,
                    "last_run_at": datetime.now(timezone.utc),
                    "failed_reason": None,
                },
                self._event("analysis_started", "running", "OpenRouter is analyzing the ticket."),
            )

            analysis_started_at = datetime.now(timezone.utc)
            analysis_payload = await self._analyze_ticket(ticket)

            await self._merge_ticket(
                ticket_id,
                {
                    "analysis": analysis_payload["analysis"],
                    "documentation": analysis_payload["documentation"],
                },
                self._event(
                    "analysis_completed",
                    "completed",
                    "Root cause analysis and documentation prepared.",
                    duration_ms=self._elapsed_ms(analysis_started_at),
                    metadata={
                        "affected_module": analysis_payload["analysis"].get("affected_module"),
                        "confidence": analysis_payload["analysis"].get("confidence"),
                    },
                ),
            )

            patch_started_at = datetime.now(timezone.utc)
            patch_result = await self._apply_patch_from_analysis(ticket, analysis_payload)
            patch_fields = {"patch": patch_result}
            if patch_result.get("applied"):
                patch_fields["status"] = AmsTicketStatus.FIXING.value
                patch_message = "AI patch applied to the codebase on the safe working tree."
            else:
                patch_fields["status"] = AmsTicketStatus.REVIEW_READY.value
                patch_message = "AI created a fix plan and PR draft, but no automatic patch was applied."

            pull_request = analysis_payload["pull_request"]
            pull_request["status"] = "ready_for_review" if patch_result.get("applied") else "draft"
            notification_started_at = datetime.now(timezone.utc)
            notification_artifact = await self._notify_stakeholders(ticket, analysis_payload)
            await self._notify_approvers(ticket_id, ticket, analysis_payload)

            await self._merge_ticket(
                ticket_id,
                {
                    **patch_fields,
                    "pull_request": pull_request,
                    "notification": notification_artifact,
                    "status": AmsTicketStatus.REVIEW_READY.value,
                    "automation_status": AmsAutomationStatus.COMPLETED.value,
                    "approval_status": AmsApprovalStatus.PENDING.value,
                    "completed_at": datetime.now(timezone.utc),
                },
                self._event(
                    "pipeline_completed",
                    "completed",
                    patch_message,
                    duration_ms=self._elapsed_ms(patch_started_at),
                    metadata={
                        "changed_files": patch_result.get("changed_files", []),
                        "linked_bug_id": ticket.get("linked_bug_id"),
                        "notification_sent_at": notification_artifact.get("sent_at"),
                        "notification_duration_ms": self._elapsed_ms(notification_started_at),
                    },
                ),
            )
        except Exception as exc:
            logger.exception("AMS pipeline failed for ticket %s", ticket_id)
            await self._merge_ticket(
                ticket_id,
                {
                    "status": AmsTicketStatus.FAILED.value,
                    "automation_status": AmsAutomationStatus.FAILED.value,
                    "failed_reason": str(exc),
                },
                self._event("pipeline_failed", "failed", str(exc)),
            )

    def _scrub_pii(self, text: str, ticket: dict) -> str:
        if not text:
            return text
        email_regex = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
        text = re.sub(email_regex, '[EMAIL_MASKED]', text)
        reporter_name = ticket.get("reporter_name")
        if reporter_name and reporter_name not in ("Unknown", ""):
            text = re.sub(re.escape(reporter_name), '[REPORTER_NAME_MASKED]', text, flags=re.IGNORECASE)
        return text

    async def _analyze_ticket(self, ticket: dict) -> dict:
        file_context = self._load_module_context(ticket.get("module_hint", ""))
        
        scrubbed_title = self._scrub_pii(ticket["title"], ticket)
        scrubbed_description = self._scrub_pii(ticket.get("description", ""), ticket)
        scrubbed_steps = self._scrub_pii(ticket.get("steps_to_reproduce", ""), ticket)
        scrubbed_expected = self._scrub_pii(ticket.get("expected_result", ""), ticket)
        scrubbed_actual = self._scrub_pii(ticket.get("actual_result", ""), ticket)
        scrubbed_module = self._scrub_pii(ticket.get("module_hint", ""), ticket)
        scrubbed_platform = self._scrub_pii(ticket.get("affected_platform", ""), ticket)

        system_prompt = (
            "You are the Automated AMS agent for an internal engineering tool. "
            "Read an internal Jira-style ticket, identify the root cause, design a safe fix, "
            "draft documentation, draft stakeholder notification content, and prepare a pull request summary. "
            "Return JSON only."
        )
        
        attempts = 2
        last_error = None
        for attempt in range(attempts):
            user_prompt = f"""
Ticket Key: {ticket["ticket_key"]}
Title: {scrubbed_title}
Description: {scrubbed_description}
Priority: {ticket.get("priority", "medium")}
Steps To Reproduce: {scrubbed_steps}
Expected Result: {scrubbed_expected}
Actual Result: {scrubbed_actual}
Affected Platform: {scrubbed_platform}
Module Hint: {scrubbed_module}

Relevant File Context:
{file_context or "No file context provided. Produce analysis and a non-destructive fix plan only."}

Return valid JSON in this exact shape:
{{
  "analysis": {{
    "root_cause": "string",
    "affected_module": "string",
    "candidate_files": ["string"],
    "fix_strategy": "string",
    "confidence": 0.0,
    "patch": {{
      "file_path": "string or empty",
      "old_code": "exact old snippet or empty",
      "new_code": "replacement code or empty",
      "summary": "string"
    }}
  }},
  "documentation": {{
    "fix_summary": "string",
    "changelog_entry": "string",
    "internal_ticket_comment": "string"
  }},
  "notification": {{
    "email_subject": "string",
    "email_body": "string"
  }},
  "pull_request": {{
    "branch_name": "string",
    "title": "string",
    "body": "string"
  }}
}}
"""
            if attempt > 0:
                user_prompt += "\nIMPORTANT: Your previous output failed to parse as valid JSON. You must return ONLY a valid JSON block, starting with '{' and ending with '}'."

            try:
                raw = await ai_client.chat_completion(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    provider="openrouter",
                    model=AMS_MODEL,
                    temperature=0.2,
                    timeout=90.0,
                )
                parsed = self._parse_json(raw)
                return parsed
            except Exception as exc:
                last_error = exc
                logger.warning(f"AMS analysis attempt {attempt + 1} failed: {exc}")
                if attempt == attempts - 1:
                    raise exc
        raise last_error

    async def _apply_patch_from_analysis(self, ticket: dict, analysis_payload: dict) -> dict:
        patch = ((analysis_payload.get("analysis") or {}).get("patch")) or {}
        file_path = (patch.get("file_path") or ticket.get("module_hint") or "").strip()
        old_code = (patch.get("old_code") or "").strip("\n")
        new_code = (patch.get("new_code") or "").strip("\n")

        result = {
            "file_path": file_path or None,
            "old_code": old_code or None,
            "new_code": new_code or None,
            "applied": False,
            "changed_files": [],
            "summary": patch.get("summary", ""),
        }

        if not file_path or not old_code or not new_code:
            return result

        applied = code_fixer.replace_code(file_path, old_code, new_code)
        if applied:
            result["applied"] = True
            result["changed_files"] = [file_path]
        return result

    async def _notify_stakeholders(self, ticket: dict, analysis_payload: dict) -> dict:
        db = get_database()
        notification_payload = analysis_payload.get("notification") or {}
        recipients = [email for email in ticket.get("stakeholder_emails", []) if email]
        reporter = await db.users.find_one({"_id": ObjectId(ticket["reporter_id"])}, {"email": 1})
        if reporter and reporter.get("email"):
            recipients.append(reporter["email"])
        recipients = list(dict.fromkeys(recipients))

        subject = notification_payload.get(
            "email_subject",
            f"{ticket['ticket_key']} automated fix report",
        )
        body = notification_payload.get(
            "email_body",
            analysis_payload.get("documentation", {}).get("fix_summary", ""),
        )

        for email in recipients:
            await email_service.send_email(
                to_email=email,
                subject=subject,
                html_content=body.replace("\n", "<br>"),
            )

        await send_notification(
            user_id=ticket["reporter_id"],
            type_="system",
            title=f"{ticket['ticket_key']} AMS pipeline finished",
            message="Automated AMS prepared a fix summary, notification payload, and PR draft.",
            entity_type="ams_ticket",
            entity_id=str(ticket["_id"]) if ticket.get("_id") else None,
            link="/dashboard/ai/ams",
        )

        return {
            "email_subject": subject,
            "email_body": body,
            "recipients": recipients,
            "sent_at": datetime.now(timezone.utc),
        }

    async def _notify_approvers(self, ticket_id: str, ticket: dict, analysis_payload: dict) -> None:
        db = get_database()
        approvers = await db.users.find(
            {"role": {"$in": ["admin", "team_lead", "hr"]}},
            {"_id": 1},
        ).to_list(length=100)
        for approver in approvers:
            await send_notification(
                user_id=str(approver["_id"]),
                type_="ai_admin_alert",
                title=f"{ticket['ticket_key']} ready for approval",
                message=analysis_payload.get("documentation", {}).get(
                    "fix_summary",
                    "Automated AMS prepared a fix and is waiting for approval.",
                ),
                entity_type="ams_ticket",
                entity_id=ticket_id,
                link="/dashboard/ai/ams",
            )

    async def _create_linked_bug(self, payload: dict, reporter: dict) -> Optional[str]:
        db = get_database()
        project = await db.projects.find_one({"_id": ObjectId(payload["project_id"])}, {"name": 1, "team": 1})
        if not project:
            return None

        role = reporter.get("role", "").lower()
        team = [str(uid) for uid in project.get("team", [])]
        if role != "admin" and str(reporter.get("id")) not in team:
            raise ValueError("Reporter is not a member of this project")

        bug_doc = {
            "project_id": payload["project_id"],
            "title": payload["title"].strip(),
            "description": payload["description"].strip(),
            "severity": payload.get("priority", "medium"),
            "status": "open",
            "steps_to_reproduce": payload.get("steps_to_reproduce", "").strip(),
            "expected_result": payload.get("expected_result", "").strip(),
            "actual_result": payload.get("actual_result", "").strip(),
            "environment": {
                "device": payload.get("affected_platform", "").strip(),
                "custom_fields": {"source": "ams"},
            },
            "is_regression": False,
            "attachments": [],
            "reporter": reporter["id"],
            "assignee": None,
            "related_task": None,
            "custom_field_values": {
                "ams_enabled": True,
                "ams_module_hint": payload.get("module_hint", "").strip(),
            },
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        result = await db.bugs.insert_one(bug_doc)
        return str(result.inserted_id)

    async def _sync_linked_bug_resolution(self, ticket: dict, approver: dict, notes: str) -> None:
        db = get_database()
        update_data = {
            "status": "resolved",
            "updated_at": datetime.now(timezone.utc),
        }
        await db.bugs.update_one({"_id": ObjectId(ticket["linked_bug_id"])}, {"$set": update_data})
        
        approver_id = str(approver.get("id")) if approver.get("id") else "approver"
        approver_doc = await db.users.find_one({"_id": ObjectId(approver_id)}) if approver_id != "approver" else {}
        author_id = str(approver_doc.get("_id", approver_id))
        author_name = approver_doc.get("name", approver.get("name", "Approver"))
        author_email = approver_doc.get("email", approver.get("email", ""))
        author_role = approver_doc.get("role", approver.get("role", ""))

        await db.comments.insert_one(
            {
                "content": (
                    f"AMS approval completed for {ticket['ticket_key']}.\n\n"
                    f"{(ticket.get('documentation') or {}).get('fix_summary', '')}\n\n"
                    f"Approval notes: {notes or 'No notes'}"
                ),
                "entity_type": "bug",
                "entity_id": str(ticket["linked_bug_id"]),
                "parent_id": None,
                "mentions": [],
                "author_id": author_id,
                "author_name": author_name,
                "author_email": author_email,
                "author_role": author_role,
                "edited": False,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )

    async def _append_changelog(self, ticket: dict, changelog_entry: str) -> str:
        async with _changelog_lock:
            def _write():
                changelog_file = PROJECT_ROOT / CHANGELOG_PATH
                if not changelog_file.exists():
                    changelog_file.write_text("# Changelog\n\n", encoding="utf-8")

                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                entry = changelog_entry or f"{ticket['ticket_key']} - {ticket['title']}"
                block = (
                    f"## {ticket['ticket_key']} - {timestamp}\n\n"
                    f"- Ticket: {ticket['title']}\n"
                    f"- Summary: {entry}\n"
                    f"- Linked Bug: {ticket.get('linked_bug_id') or 'None'}\n\n"
                )
                with changelog_file.open("a", encoding="utf-8") as handle:
                    handle.write(block)
            
            await asyncio.to_thread(_write)
        return CHANGELOG_PATH

    async def _prepare_git_artifacts(self, ticket: dict, changelog_path: str, project_doc: dict, approver_doc: dict) -> dict:
        branch_name = self._sanitize_branch_name(ticket["ticket_key"], ticket["title"])
        base_branch = await asyncio.to_thread(git_service.get_current_branch)
        changed_files = list((ticket.get("patch") or {}).get("changed_files", []))
        stage_files = changed_files + [changelog_path]
        commit_message = f"{ticket['ticket_key']}: {ticket['title']}"

        artifact = {
            "branch_name": branch_name,
            "base_branch": base_branch,
            "commit_message": commit_message,
            "commit_sha": None,
            "staged_files": stage_files,
            "commit_created": False,
            "remote_push_error": None,
        }

        try:
            def _run_git():
                git_service.ensure_branch(branch_name)
                git_service.stage_files(stage_files)
                if git_service.has_staged_changes():
                    artifact["commit_sha"] = git_service.commit(commit_message)
                    artifact["commit_created"] = True
                    
                    # Check for GitHub integration
                    github_repo = project_doc.get("github_repo")
                    github_token = approver_doc.get("github_access_token")
                    if github_token and "." in github_token:
                        try:
                            github_token = decrypt_password(github_token)
                        except Exception as dec_exc:
                            logger.error(f"Failed to decrypt GitHub token: {dec_exc}")
                    
                    if github_repo and github_token:
                        try:
                            git_service.push_to_remote(branch_name, github_repo, github_token)
                            artifact["remote_pushed"] = True
                        except Exception as push_exc:
                            logger.warning(f"Failed to push to GitHub remote for {ticket['ticket_key']}: {push_exc}")
                            artifact["remote_push_error"] = str(push_exc)
                            artifact["remote_pushed"] = False

            await asyncio.to_thread(_run_git)

        except Exception as exc:
            artifact["error"] = str(exc)
        return artifact

    async def _generate_ticket_key(self) -> str:
        db = get_database()
        count = await db.ams_tickets.count_documents({})
        return f"AMS-{count + 1001}"

    async def _merge_ticket(self, ticket_id: str, fields: dict, event: Optional[dict] = None) -> None:
        db = get_database()
        update: Dict[str, Any] = {"$set": {**fields, "updated_at": datetime.now(timezone.utc)}}
        if event:
            update["$push"] = {"timeline": event}
        await db.ams_tickets.update_one({"_id": ObjectId(ticket_id)}, update)

    def _load_module_context(self, module_hint: str) -> str:
        module_hint = module_hint.strip()
        if not module_hint:
            return ""

        candidate = (PROJECT_ROOT / module_hint).resolve()
        try:
            candidate.relative_to(PROJECT_ROOT.resolve())
        except ValueError:
            return ""

        if not candidate.exists() or not candidate.is_file():
            return ""

        try:
            content = candidate.read_text(encoding="utf-8")
        except Exception:
            return ""

        return f"File: {module_hint}\n{content[:100000]}"

    def _parse_json(self, raw: str) -> dict:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            try:
                start_idx = text.find("{")
                end_idx = text.rfind("}")
                if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                    return json.loads(text[start_idx:end_idx + 1])
            except Exception:
                pass
            raise AIClientError(f"AMS AI returned invalid JSON: {text}")

    def _normalize(self, doc: dict) -> dict:
        normalized = dict(doc)
        normalized["_id"] = str(normalized["_id"])
        return normalized

    def _event(
        self,
        stage: str,
        status: str,
        message: str,
        *,
        duration_ms: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        return {
            "stage": stage,
            "status": status,
            "message": message,
            "duration_ms": duration_ms,
            "metadata": metadata,
            "created_at": datetime.now(timezone.utc),
        }

    def _sanitize_branch_name(self, ticket_key: str, title: str) -> str:
        safe_title = "".join(ch.lower() if ch.isalnum() else "-" for ch in title).strip("-")
        while "--" in safe_title:
            safe_title = safe_title.replace("--", "-")
        return f"ams/{ticket_key.lower()}-{safe_title[:40]}".rstrip("-")

    def _elapsed_ms(self, started_at: datetime) -> int:
        return max(0, int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000))


ams_service = AMSService()
