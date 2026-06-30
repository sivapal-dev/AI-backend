import json
import logging
from typing import Any, Dict
from ai_client import ai_client

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a fair and empathetic team lead evaluating whether a developer's reason for not completing a task is valid. "
    "Consider: time spent vs estimated hours, task complexity, status history, and the reason provided. "
    "Respond ONLY with a JSON object: {\"valid\": true/false, \"evaluation\": \"<1-2 sentence explanation>\"}"
)


async def validate_completion_reason(
    task: Dict[str, Any], reason: str
) -> tuple[bool, str]:
    """
    AI judges if a developer's reason for not completing a task is valid.
    Returns (is_valid: bool, evaluation: str).
    """
    title = task.get("title", "Untitled")
    description = (task.get("description") or "")[:200]
    time_spent = task.get("time_spent", 0)
    estimated = task.get("estimated_hours", "unknown")
    priority = task.get("priority", "medium")
    complexity = task.get("complexity", "medium")
    status = task.get("status", "in_progress")

    prompt = (
        f"Task: {title}\n"
        f"Description: {description}\n"
        f"Priority: {priority}\n"
        f"Complexity: {complexity}\n"
        f"Status: {status}\n"
        f"Time spent: {time_spent}h (estimated: {estimated}h)\n"
        f"Developer reason: '{reason}'\n\n"
        "Evaluate if this reason is valid. Return JSON only."
    )

    try:
        raw = await ai_client.chat_completion(
            system_prompt=_SYSTEM, user_prompt=prompt, temperature=0.3
        )
        import json
        result = json.loads(raw)
        is_valid = bool(result.get("valid", False))
        evaluation = result.get("evaluation", "No evaluation provided.")
        return is_valid, evaluation
    except Exception as e:
        logger.error("AI reason validation failed, flagging for human review: %s", e)
        return False, "AI evaluation failed — flagged for human review. Assuming invalid until manual review."
