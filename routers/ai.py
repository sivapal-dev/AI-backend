from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from bson import ObjectId
from database import get_database
from dependencies import get_current_active_user
from config import get_settings
from ai_client import ai_client, AIClientError
import json
import re as _re


class ExtractProjectMetadataRequest(BaseModel):
    markdown: str


class GenerateTasksRequest(BaseModel):
    markdown: str
    project_id: str


class RegenerateTaskRequest(BaseModel):
    markdown: str
    task: Dict[str, Any]
    project_id: Optional[str] = None


class GenerateSubtasksRequest(BaseModel):
    task_id: str
    context: str


class RegenerateSubtaskRequest(BaseModel):
    context: str
    subtask: Dict[str, Any]


class GenerateDescriptionRequest(BaseModel):
    title: str
    context: Optional[str] = None


class TaskStatusSummaryRequest(BaseModel):
    project_id: Optional[str] = None
    task_summary: Dict[str, Any]

settings = get_settings()
router = APIRouter(prefix="/ai", tags=["AI"])


def _get_db():
    return get_database()


def _extract_json(ai_content: str) -> dict:
    """Extract a JSON object from AI output, stripping system tags and markdown."""
    clean = str(ai_content)
    clean = _re.sub(r'<system-reminder>[\s\S]*?</system-reminder>', '', clean, flags=_re.IGNORECASE)
    clean = _re.sub(r'<system-prompt>[\s\S]*?</system-prompt>', '', clean, flags=_re.IGNORECASE)
    clean = _re.sub(r'<[^>]{1,30}>[\s\S]*?</[^>]{1,30}>', '', clean)
    clean = _re.sub(r'<[^>]{1,30}>', '', clean)
    clean = clean.strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    m = _re.search(r'```(?:json)?\s*\n([\s\S]*?)\n```', clean)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = _re.search(r'\{[\s\S]*?\}', clean, _re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    start = clean.find('{')
    if start != -1:
        decoder = json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(clean, start)
            return obj
        except json.JSONDecodeError:
            pass
    raise HTTPException(status_code=422, detail="AI returned unparseable JSON")


def _get_ai_prefs(current_user: dict):
    """Extract AI provider + model from user settings."""
    settings = current_user.get("settings", {})
    provider = settings.get("ai_provider", "auto")  # openrouter | mistral | auto (default)
    model = settings.get("ai_model", None)          # e.g. poolside/laguna-m.1:free
    return provider, model


SYSTEM_PROMPT = """You are a project management AI assistant. Given a project description in markdown, generate structured tasks for a modern AI-assisted software development team.

You will also receive context about:
- EXISTING TASKS: Tasks that already exist in this project. Review them carefully.
- TEAM MEMBERS: Available developers with their names, roles, and IDs.

Rules:
1. Carefully review EXISTING TASKS before generating. If the markdown describes a change to an existing task (e.g. "change the DB local to SQLite3"), mark the existing task with `"action": "update"` and update its `title`/`description` accordingly — do NOT create a duplicate.
2. For genuinely new work not covered by any existing task, create a new task with `"action": "create"`.
3. Assign tasks to appropriate team members using `assignee_id` and `assignee_name`. Match the role required (frontend/backend/etc.) to the developer's role and skills.
4. Categorize tasks by role: frontend, backend, fullstack, ui_ux, qa, devops
5. Each task must have: title (concise), description (detailed), priority (low/medium/high/critical), complexity (simple/medium/complex/very_complex)
6. Identify dependencies between tasks
7. Do not duplicate tasks
8. Ensure all requirements from the markdown are covered
9. CRITICAL — TASK DISTRIBUTION:
   - Distribute tasks EVENLY across ALL available team members.
   - NO developer should receive more than 3 tasks total.
   - If you have more tasks than (team size x 3), leave the extra tasks unassigned (`assignee_id`: null, `assignee_name`: null).
   - Never dump all tasks on one developer. Spread the work across the team.
   - Example: 3 team members, 8 tasks → Developer A: 3 tasks, Developer B: 3 tasks, Developer C: 2 tasks.
10. VERY IMPORTANT: Developers use AI coding assistants. Because of this, `estimated_hours` should be AGGRESSIVELY LOW. 
    - simple tasks: 0.5 to 1 hour
    - medium tasks: 1 to 2 hours
    - complex tasks: 2 to 4 hours
    - very_complex tasks: 4 to 6 hours max
11. Return ONLY valid JSON in this exact format:

{
  "tasks": [
    {
      "action": "create|update",
      "title": "string",
      "description": "string",
      "role": "frontend|backend|fullstack|ui_ux|qa|devops",
      "priority": "low|medium|high|critical",
      "complexity": "simple|medium|complex|very_complex",
      "dependencies": ["title of dependent task"],
      "estimated_hours": number,
      "assignee_id": "string or null",
      "assignee_name": "string or null"
    }
   ]
 }
 """

SUBTASK_SYSTEM_PROMPT = """You are a project management AI assistant. Given a task description, break it down into smaller, actionable subtasks for a modern AI-assisted software team.

Rules:
1. Generate 3-8 subtasks that collectively complete the parent task
2. Each subtask must have:
   - title (concise, action-oriented, start with verb)
   - description (detailed enough to implement without further clarification)
   - role (one of: frontend, backend, ui_ux, qa, devops, fullstack)
   - priority (low/medium/high/critical) based on impact and urgency
   - complexity (simple/medium/complex/very_complex) — estimate implementation effort
   - estimated_hours (VERY LOW due to AI assistance, 0.5-2 hours typical, max 4 hours)
3. Subtasks should be:
   - Specific and measurable
   - Independent where possible (minimize coupling)
   - Appropriately sized (0.5 to 2 hours ideal)
   - Cover all aspects: implementation, testing, documentation, deployment
4. Consider the technical stack implied by the task
5. Return ONLY valid JSON in this exact format:

{
  "subtasks": [
    {
      "title": "string",
      "description": "string",
      "role": "frontend|backend|ui_ux|qa|devops|fullstack",
      "priority": "low|medium|high|critical",
      "complexity": "simple|medium|complex|very_complex",
      "estimated_hours": number
    }
  ]
}
"""

DESCRIPTION_SYSTEM_PROMPT = """You are a project management AI assistant. Given a task title, generate a comprehensive description with acceptance criteria.

Rules:
1. Write a clear, detailed description that explains:
   - What needs to be done
   - Why it matters (context/purpose)
   - Who it's for (stakeholder/end user)
   - Any technical considerations (stack, patterns, constraints)
2. Include 3-5 specific, testable acceptance criteria (Given/When/Then format)
3. Keep description professional but readable (2-4 paragraphs)
4. Return ONLY valid JSON in this exact format:

{
  "description": "string (detailed description with paragraphs)",
  "acceptance_criteria": ["string", "string", ...]
}
"""

BUG_ANALYSIS_PROMPT = """You are a senior QA engineer and bug analyst. Given a list of software bugs, analyze them and provide actionable insights.

Rules:
1. Identify patterns, recurring issues, and root cause categories
2. Assess severity distribution and highlight critical/blocker trends
3. Suggest prioritized fixes based on impact and frequency
4. Identify areas of the codebase or features most prone to bugs
5. Provide 3-5 specific, actionable recommendations
6. Return ONLY valid JSON in this exact format:

{
  "summary": "string (2-3 sentence overview)",
  "patterns": [
    {
      "pattern": "string (pattern name)",
      "description": "string (explanation)",
      "affected_bugs": ["bug title 1", "bug title 2"],
      "severity": "low|medium|high|critical|blocker"
    }
  ],
  "severity_distribution": {
    "low": number,
    "medium": number,
    "high": number,
    "critical": number,
    "blocker": number
  },
  "recommendations": [
    {
      "priority": 1,
      "title": "string",
      "description": "string",
      "expected_impact": "string"
    }
  ],
  "risk_areas": ["string", "string"]
}
"""

TASK_STATUS_PROMPT = """You are a project management AI assistant. Given a summary of task statuses across projects, provide insights on team productivity, bottlenecks, and recommendations.

Rules:
1. Analyze the task distribution and identify bottlenecks
2. Assess team velocity and productivity trends
3. Identify risks and blockers
4. Provide 3-5 specific, actionable recommendations
5. Return ONLY valid JSON in this exact format:

{
  "summary": "string (2-3 sentence overview)",
  "health_score": number (0-100),
  "bottlenecks": [
    {
      "stage": "string (status name)",
      "severity": "low|medium|high|critical",
      "description": "string",
      "affected_tasks": number
    }
  ],
  "recommendations": [
    {
      "priority": 1,
      "title": "string",
      "description": "string"
    }
  ],
  "velocity_trend": "improving|stable|declining",
  "risk_level": "low|medium|high|critical"
}
"""


def _validate_tasks(tasks: list) -> list:
    """Validate and deduplicate generated tasks."""
    seen_titles = set()
    validated = []
    for task in tasks:
        title = task.get("title", "").strip()
        if not title or title.lower() in seen_titles:
            continue
        seen_titles.add(title.lower())
        validated.append({
            "title": title,
            "description": task.get("description", ""),
            "role": task.get("role", "frontend"),
            "priority": task.get("priority", "medium"),
            "complexity": task.get("complexity", "medium"),
            "dependencies": task.get("dependencies", []),
            "estimated_hours": task.get("estimated_hours"),
            "action": task.get("action", "create"),
            "assignee_id": task.get("assignee_id"),
            "assignee_name": task.get("assignee_name"),
        })
    return validated


def _validate_subtasks(subtasks: list) -> list:
    """Validate and deduplicate generated subtasks."""
    seen_titles = set()
    validated = []
    for subtask in subtasks:
        title = subtask.get("title", "").strip()
        if not title or title.lower() in seen_titles:
            continue
        seen_titles.add(title.lower())
        validated.append({
            "title": title,
            "description": subtask.get("description", ""),
            "role": subtask.get("role", "frontend"),
            "priority": subtask.get("priority", "medium"),
            "complexity": subtask.get("complexity", "medium"),
            "estimated_hours": subtask.get("estimated_hours"),
        })
    return validated


MAX_TASKS_PER_DEV = 3


def _redistribute_tasks(tasks: list, team_members: list) -> list:
    """Enforce max 3 tasks per developer and round-robin distribute excess."""
    if not tasks:
        return tasks
    if not team_members:
        for t in tasks:
            t["assignee_id"] = None
            t["assignee_name"] = None
        return tasks
    dev_ids = [m["id"] for m in team_members]

    dev_names = {m["id"]: m["name"] for m in team_members}
    n = len(dev_ids)

    kept: dict[str, int] = {did: 0 for did in dev_ids}
    rr_idx = 0
    result = []

    for t in tasks:
        did = t.get("assignee_id")
        if did and did in kept and kept[did] < MAX_TASKS_PER_DEV:
            kept[did] += 1
            result.append(t)
            continue
        # Reassign via round-robin to developers under the limit
        found = False
        for _ in range(n):
            candidate = dev_ids[rr_idx % n]
            if kept[candidate] < MAX_TASKS_PER_DEV:
                t["assignee_id"] = candidate
                t["assignee_name"] = dev_names.get(candidate)
                kept[candidate] += 1
                rr_idx += 1
                found = True
                break
            rr_idx += 1
        if not found:
            t["assignee_id"] = None
            t["assignee_name"] = None
        result.append(t)
    return result


PROJECT_METADATA_SYSTEM_PROMPT = """You are a project analysis AI. Given a project description in markdown, extract structured project metadata.

Rules:
1. Analyze the markdown content thoroughly
2. Extract or infer:
   - project_name: A concise, descriptive project name (max 80 chars)
   - description: A comprehensive project description (2-3 sentences, max 300 chars)
   - tags: 3-5 relevant tags as an array of strings (e.g., ["web", "mobile", "api", "redesign"])
3. If the markdown already has headings like "# Project Name", "## Description", use them directly
4. If content is ambiguous, use your best judgment based on the text
5. Return ONLY valid JSON in this exact format:

{
  "project_name": "string",
  "description": "string",
  "tags": ["string", "string", ...]
}

Do not include markdown formatting in the output strings.
"""


@router.post("/extract-project-metadata")
async def extract_project_metadata(data: ExtractProjectMetadataRequest, current_user: dict = Depends(get_current_active_user)):
    markdown = data.markdown
    if not markdown or len(markdown) < 10:
        raise HTTPException(status_code=400, detail="Markdown content too short")
    try:
        provider, model = _get_ai_prefs(current_user)
        ai_content = await ai_client.chat_completion(system_prompt=PROJECT_METADATA_SYSTEM_PROMPT,
            user_prompt=f"Analyze the following project description and extract metadata:\n\n{markdown}",
            provider=provider,
            model=model,
        )
        parsed = _extract_json(ai_content)
        return {"success": True, "project_name": parsed.get("project_name", "Untitled Project"), "description": parsed.get("description", ""), "tags": parsed.get("tags", [])}
    except AIClientError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/generation-history/{project_id}")
async def get_generation_history(
    project_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    is_preview = project_id == "preview"
    if not is_preview:
        try:
            project = await db.projects.find_one({"_id": ObjectId(project_id)})
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid project_id format")
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if current_user.get("role", "").lower() not in ("admin", "team_lead", "hr"):
            if current_user["id"] not in project.get("team", []):
                raise HTTPException(status_code=403, detail="Not a member of this project")
    cursor = (
        db.ai_generations.find({"project_id": project_id})
        .sort("created_at", -1)
        .limit(10)
    )
    history = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        history.append(doc)
    return history


@router.post("/generate-tasks")
async def generate_tasks(data: GenerateTasksRequest, current_user: dict = Depends(get_current_active_user)):
    markdown = data.markdown
    project_id = data.project_id
    if not markdown or len(markdown) < 10:
        raise HTTPException(status_code=400, detail="Markdown content too short (min 10 characters)")
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    
    db = _get_db()
    project = None
    is_preview = project_id == "preview"
    if not is_preview:
        try:
            project = await db.projects.find_one({"_id": ObjectId(project_id)})
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid project_id format")
    if not project and not is_preview:
        raise HTTPException(status_code=404, detail="Project not found")
    if not is_preview:
        if current_user.get("role", "").lower() not in ("admin", "team_lead", "hr"):
            if current_user["id"] not in project.get("team", []):
                raise HTTPException(status_code=403, detail="Not a member of this project")
    
    # Fetch existing tasks for context (skip for preview)
    existing_tasks = []
    if not is_preview:
        existing_tasks = await db.tasks.find(
            {"project_id": project_id, "parent_id": None}
        ).sort("created_at", -1).limit(50).to_list(50)
    tasks_context = "\n".join([
        f"- {t['title']} ({t.get('role', '')}) [status: {t.get('status', 'todo')}]"
        for t in existing_tasks
    ]) if existing_tasks else "No existing tasks."
    
    # Fetch team members for assignment
    team_ids = project.get("team", []) if project else []
    team_members = []
    if team_ids:
        team_cursor = db.users.find({"_id": {"$in": [ObjectId(uid) for uid in team_ids]}})
        async for u in team_cursor:
            team_members.append({
                "id": str(u["_id"]),
                "name": u.get("name", ""),
                "role": u.get("role", ""),
            })
    team_context = "\n".join([
        f"- {m['name']} ({m['role']}) id={m['id']}"
        for m in team_members
    ]) if team_members else "No team members (will not assign)."
    
    provider, model = _get_ai_prefs(current_user)
    ai_content = await ai_client.chat_completion(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=f"""EXISTING TASKS ({len(existing_tasks)} total):
{tasks_context}

TEAM MEMBERS ({len(team_members)} total):
{team_context}

CURRENT USER CONTEXT:
The user requesting this generation is {current_user.get('name', 'Unknown')} (Role: {current_user.get('role', 'Unknown')}).

MARKDOWN DESCRIPTION:
{markdown}

Based on the existing tasks and the new markdown description, determine which existing tasks need UPDATING and which NEW tasks need to be created.

TASK DISTRIBUTION RULES:
- Distribute tasks EVENLY across ALL {len(team_members)} team members
- Maximum 3 tasks per developer
- If you have more tasks than {len(team_members) * 3}, leave extras unassigned
- Never assign more than 3 tasks to any single developer""",
        provider=provider,
        model=model,
    )
    parsed = _extract_json(ai_content)
    tasks = parsed.get("tasks", [])
    validated = _validate_tasks(tasks)
    validated = _redistribute_tasks(validated, team_members)

    await db.ai_generations.insert_one({
        "project_id": project_id,
        "user_id": current_user["id"],
        "markdown_preview": markdown[:500],
        "generated_tasks_count": len(validated),
        "tasks_preview": [{"title": t["title"], "role": t["role"], "action": t.get("action", "create")} for t in validated],
        "created_at": datetime.now(timezone.utc),
    })

    return {"tasks": validated}


@router.post("/regenerate-task")
async def regenerate_task(data: RegenerateTaskRequest, current_user: dict = Depends(get_current_active_user)):
    markdown = data.markdown
    task = data.task
    project_id = data.project_id or ""
    if not markdown or not task:
        raise HTTPException(status_code=400, detail="markdown and task are required")
    provider, model = _get_ai_prefs(current_user)
    prompt = (
        f"The following tasks were generated from this markdown:\n\n{markdown}\n\n"
        f"Regenerate ONLY the following task differently (different approach, different implementation):\n"
        f"Title: {task.get('title', '')}\n"
        f"Description: {task.get('description', '')}\n"
        f"Role: {task.get('role', 'frontend')}\n"
        f"Priority: {task.get('priority', 'medium')}\n\n"
        f"Return ONLY valid JSON in this format:\n"
        f'{{"task": {{"title": "string", "description": "string", "role": "frontend|backend|fullstack|ui_ux|qa|devops", "priority": "low|medium|high|critical", "complexity": "simple|medium|complex|very_complex", "estimated_hours": number}}}}'
    )
    try:
        ai_content = await ai_client.chat_completion(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            provider=provider,
            model=model,
        )
        parsed = _extract_json(ai_content)
        validated = _validate_tasks([parsed.get("task", {})])
        return {"task": validated[0] if validated else parsed.get("task", {})}
    except AIClientError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/generate-subtasks")
async def generate_subtasks(data: GenerateSubtasksRequest, current_user: dict = Depends(get_current_active_user)):
    task_id = data.task_id
    context = data.context
    if not task_id or not context:
        raise HTTPException(status_code=400, detail="task_id and context are required")
    provider, model = _get_ai_prefs(current_user)
    db = _get_db()
    task_doc = await db.tasks.find_one({"_id": ObjectId(task_id)}) if task_id else None
    task_title = task_doc.get("title", "") if task_doc else ""
    task_desc = task_doc.get("description", "") if task_doc else ""
    prompt = f"Break down this task into subtasks:\nTask title: {task_title}\nTask description: {task_desc}\nAdditional context from user: {context}"
    try:
        ai_content = await ai_client.chat_completion(
            system_prompt=SUBTASK_SYSTEM_PROMPT,
            user_prompt=prompt,
            provider=provider,
            model=model,
        )
        parsed = _extract_json(ai_content)
        subtasks = parsed.get("subtasks", [])
        return {"subtasks": _validate_subtasks(subtasks)}
    except AIClientError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/regenerate-subtask")
async def regenerate_subtask(data: RegenerateSubtaskRequest, current_user: dict = Depends(get_current_active_user)):
    context = data.context
    subtask = data.subtask
    if not context or not subtask:
        raise HTTPException(status_code=400, detail="context and subtask are required")
    provider, model = _get_ai_prefs(current_user)
    prompt = (
        f"Task context:\n{context}\n\n"
        f"Regenerate ONLY the following subtask differently:\n"
        f"Title: {subtask.get('title', '')}\n"
        f"Description: {subtask.get('description', '')}\n"
        f"Role: {subtask.get('role', 'frontend')}\n\n"
        f"Return ONLY valid JSON in this format:\n"
        f'{{"subtask": {{"title": "string", "description": "string", "role": "frontend|backend|ui_ux|qa|devops|fullstack", "priority": "low|medium|high|critical", "complexity": "simple|medium|complex|very_complex", "estimated_hours": number}}}}'
    )
    try:
        ai_content = await ai_client.chat_completion(
            system_prompt=SUBTASK_SYSTEM_PROMPT,
            user_prompt=prompt,
            provider=provider,
            model=model,
        )
        parsed = _extract_json(ai_content)
        validated = _validate_subtasks([parsed.get("subtask", {})])
        return {"subtask": validated[0] if validated else parsed.get("subtask", {})}
    except AIClientError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/generate-description")
async def generate_description(data: GenerateDescriptionRequest, current_user: dict = Depends(get_current_active_user)):
    title = data.title
    context = data.context or ""
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    provider, model = _get_ai_prefs(current_user)
    prompt = f"Generate a comprehensive description with acceptance criteria for:\nTitle: {title}\n"
    if context:
        prompt += f"Additional context:\n{context}\n"
    try:
        ai_content = await ai_client.chat_completion(
            system_prompt=DESCRIPTION_SYSTEM_PROMPT,
            user_prompt=prompt,
            provider=provider,
            model=model,
        )
        parsed = _extract_json(ai_content)
        return {
            "description": parsed.get("description", ""),
            "acceptance_criteria": parsed.get("acceptance_criteria", []),
        }
    except AIClientError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/task-status-summary")
async def get_task_status_summary(
    data: TaskStatusSummaryRequest,
    current_user: dict = Depends(get_current_active_user)
):
    task_summary = data.task_summary
    provider, model = _get_ai_prefs(current_user)
    
    prompt = f"""Analyze the following task status summary:
Project: {task_summary.get('project_name', 'All Projects')}
Total Tasks: {task_summary.get('total', 0)}
Backlog: {task_summary.get('backlog', 0)}
To Do: {task_summary.get('todo', 0)}
In Progress: {task_summary.get('in_progress', 0)}
Code Review: {task_summary.get('code_review', 0)}
Testing: {task_summary.get('testing', 0)}
Done: {task_summary.get('done', 0)}
Completion Rate: {task_summary.get('completion_rate', 0)}%
Overdue Tasks: {task_summary.get('overdue', 0)}
High/Critical Priority Tasks: {task_summary.get('high_priority', 0)}
"""
    try:
        ai_content = await ai_client.chat_completion(
            system_prompt=TASK_STATUS_PROMPT,
            user_prompt=prompt,
            provider=provider,
            model=model,
        )
        parsed = _extract_json(ai_content)
        return {"summary": parsed}
    except AIClientError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/models")
async def get_available_models(
    current_user: dict = Depends(get_current_active_user)
):
    """Return available AI models."""
    from ai_client import OPENROUTER_MODELS
    has_key = bool(settings.openrouter_api_key) and not any(settings.openrouter_api_key.startswith(p) for p in ("placeholder", "your-", "<your-"))
    return {
        "openrouter": OPENROUTER_MODELS if has_key else [],
        "default": settings.openrouter_default_model or OPENROUTER_MODELS[0] if has_key else "mistral",
        "provider": "openrouter" if has_key else "mistral",
    }
