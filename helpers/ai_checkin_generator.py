import logging
from typing import Any, Dict
from ai_client import ai_client
from database import get_database

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a detail-oriented engineering lead running daily stand-ups. "
    "Generate a check-in question that asks for SPECIFIC, ACTIONABLE details about the developer's work. "
    "Avoid generic questions. Push for specifics: which module, which feature, which component, what progress stage, any PRs, any decisions made. "
    "Keep it warm and professional but direct. 1-3 sentences. Use their name. Be encouraging but expect substance."
)


async def generate_morning_checkin(user: Dict[str, Any]) -> str:
    """Generate a personalized morning check-in question asking for detailed plan."""
    name = user.get("name") or user.get("email", "there").split("@")[0]
    role = user.get("role") or "team member"
    
    # Query projects where the user is a team member (W186)
    projects = []
    try:
        db = get_database()
        user_id_str = str(user.get("_id") or user.get("id") or "")
        if user_id_str:
            cursor = db.projects.find({"team": user_id_str}, {"name": 1})
            projects = [p["name"] async for p in cursor]
    except Exception as e:
        logger.error(f"Failed to query user projects for check-in: {e}")

    prompt = (
        f"User: {name}\n"
        f"Role: {role}\n"
        f"Active projects: {', '.join(projects) if projects else 'None'}\n\n"
        "Ask them for their detailed plan today — not just which project, but specific modules, features, "
        "components they'll work on, any PRs in progress, and what they aim to complete before end of day. "
        "Ask for concrete deliverables, not vague goals."
    )
    try:
        res = await ai_client.chat_completion(
            system_prompt=_SYSTEM, user_prompt=prompt, temperature=0.7
        )
        if not res or not res.strip():
            raise ValueError("Empty AI response")  # Trigger fallback (W187)
        return res.strip()
    except Exception as e:
        logger.warning("AI morning check-in generation failed, using fallback: %s", e)
        return (
            f"Good morning {name}! Could you share a detailed plan for today? "
            f"Which specific modules, features, or components are you focusing on? "
            f"Any PRs in progress or decisions you're working through?"
        )


async def generate_evening_checkin(user: Dict[str, Any]) -> str:
    """Generate a personalized evening check-in question asking for detailed accomplishments."""
    name = user.get("name") or user.get("email", "there").split("@")[0]
    role = user.get("role") or "team member"
    prompt = (
        f"User: {name}\n"
        f"Role: {role}\n\n"
        "Ask them for a detailed wrap-up of today's work — not just what they did, but specific modules completed, "
        "features implemented, bugs fixed, PRs merged, blockers encountered, and any decisions made. "
        "Ask for concrete outcomes, not general statements."
    )
    try:
        res = await ai_client.chat_completion(
            system_prompt=_SYSTEM, user_prompt=prompt, temperature=0.7
        )
        if not res or not res.strip():
            raise ValueError("Empty AI response")  # Trigger fallback (W187)
        return res.strip()
    except Exception as e:
        logger.warning("AI evening check-in generation failed, using fallback: %s", e)
        return (
            f"Hi {name}, let's wrap up your day — what specific modules or features did you complete or make progress on? "
            f"Any PRs merged, bugs squashed, or blockers you ran into? Share the details so we can track progress accurately."
        )
