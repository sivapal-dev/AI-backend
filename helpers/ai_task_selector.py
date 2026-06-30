import json
import logging
from typing import Any, Dict, Optional, List
from bson import ObjectId
from database import get_database
from ai_client import ai_client

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are an AI project coordinator. Select the single most suitable next task for a developer "
    "from the available tasks list. Respond ONLY with a JSON object containing:\n"
    '{"task_id": "<id>", "reason": "<1-sentence explanation>"}\n'
    "If no suitable task exists, return {\"task_id\": null, \"reason\": \"No suitable tasks available.\"}"
)


async def pick_next_task(
    project_id: str, developer_id: str, exclude_task_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Query unassigned tasks from the project and use AI to pick the best one.
    Returns the chosen task document, or None if no suitable task.
    """
    db = get_database()
    # Confirm status selection constraints: unassigned tasks in backlog/todo (W192)
    query = {
        "project_id": project_id,
        "$or": [
            {"assignee_id": {"$exists": False}},
            {"assignee_id": None},
            {"assignee_id": ""},
        ],
        "status": {"$in": ["backlog", "todo"]},
    }
    if exclude_task_id:
        query["_id"] = {"$ne": ObjectId(exclude_task_id)}

    cursor = db.tasks.find(query)
    all_matching = await cursor.to_list(length=100)

    # Priority weight mapping: high=3, medium=2, low=1, others=0
    def get_priority_weight(task_doc):
        p = task_doc.get("priority", "medium") or "medium"
        p_str = p.lower() if isinstance(p, str) else str(p).lower()
        if p_str == "high":
            return 3
        elif p_str == "medium":
            return 2
        elif p_str == "low":
            return 1
        return 0

    def get_created_at_key(task_doc):
        val = task_doc.get("created_at")
        if val is None:
            return ""
        if isinstance(val, str):
            return val
        try:
            return val.isoformat()
        except Exception:
            return str(val)

    # Sort stably: first by created_at ascending, then by priority weight descending
    all_matching.sort(key=get_created_at_key)
    all_matching.sort(key=get_priority_weight, reverse=True)
    tasks = all_matching[:20]

    if not tasks:
        logger.info("No unassigned tasks in project %s", project_id)
        return None

    task_list = []
    for t in tasks:
        task_list.append(
            {
                "id": str(t["_id"]),
                "title": t.get("title", "Untitled"),
                "description": (t.get("description") or "")[:120],
                "priority": t.get("priority", "medium"),
                "status": t.get("status", "backlog"),
                "role": t.get("role", ""),
            }
        )

    user = await db.users.find_one({"_id": ObjectId(developer_id)})
    user_name = user.get("name") if user else "Developer"

    prompt = (
        f"Developer: {user_name}\n"
        f"Project ID: {project_id}\n"
        f"Available tasks:\n"
    )
    for t in task_list:
        prompt += f"- {t['id']}: {t['title']} (priority={t['priority']}, status={t['status']}, role={t['role']})\n"

    prompt += "\nPick the single best task for this developer and return JSON only."

    try:
        raw = await ai_client.chat_completion(
            system_prompt=_SYSTEM, user_prompt=prompt, temperature=0.3
        )
        import json
        result = json.loads(raw)
        chosen_id = result.get("task_id")
        if not chosen_id:
            return None
        chosen = next((t for t in tasks if str(t["_id"]) == chosen_id), None)
        if not chosen:
            # AI returned a chosen_id that is not in the list (W194)
            logger.warning(
                f"AI task selector returned task_id '{chosen_id}' which is not present in the matching list. Triggering fallback."
            )
            raise ValueError(f"AI returned invalid task_id {chosen_id}")
            
        chosen["ai_reason"] = result.get("reason", "")
        return chosen
    except Exception as e:
        logger.warning("AI task selection failed, executing fallback relevance matching: %s", e)
        # Fallback relevance sorting/matching loop (W193)
        dev_role = str(user.get("role") or "").lower() if user else ""
        chosen_fallback = None
        for t in tasks:
            t_role = str(t.get("role") or "").lower()
            # Simple match: if the task's role is in the developer's role or vice versa
            if dev_role and (dev_role in t_role or t_role in dev_role):
                chosen_fallback = t
                break
        
        if not chosen_fallback:
            # Fall back to the first task in sorted list
            chosen_fallback = tasks[0]

        chosen_fallback["ai_reason"] = f"Fallback selection: matched by role/priority. (Reason: {e})"
        return chosen_fallback
