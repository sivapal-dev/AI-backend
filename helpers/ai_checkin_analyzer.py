import logging
from database import get_database
from helpers.notification_sender import send_notification
from ai_client import ai_client

logger = logging.getLogger(__name__)

REPETITION_SYSTEM_PROMPT = """You are a project management AI analyzing daily check-in responses from a team member.
Detect if the team member is describing work on the SAME task across consecutive check-ins, even if they use different words, spelling variations, or paraphrasing.

Examples of SAME vs DIFFERENT tasks:
- "completed the hr portal integration" / "finalized the hr portal backend API" → SAME task (hr portal integration work)
- "working on the login page styling" / "updating login screen CSS" → SAME task (login page styling)
- "fixing button styling on landing page" / "writing unit tests for backend user registration" → DIFFERENT tasks (frontend UI vs backend testing)
- "refactoring dashboard charts" / "working on billing invoice layout" → DIFFERENT tasks (dashboard visual refactoring vs billing layout work)

Be lenient with spelling mistakes and grammatical errors. Focus on the core TASK/PROJECT being described, not the exact words.

Reply with ONLY valid JSON in this exact format:
{"is_repeated": true, "same_task": "brief task name", "confidence": 85, "reason": "Brief explanation in 1 sentence"}"""

REMARK_SYSTEM_PROMPT = """You are a project management AI analyzing a team member's remark about missing a daily check-in.
Determine if the remark indicates a potential concern (blocker, burnout, confusion, disengagement).

Reply with ONLY valid JSON in this exact format:
{"has_concern": true, "concern_type": "blocker|burnout|confusion|disengagement|other", "severity": "low|medium|high", "reason": "Brief explanation in 1 sentence"}"""

REMARK_QUALITY_SYSTEM_PROMPT = """You are a project management AI reviewing a team member's remark about missing a daily check-in.
Determine if the remark is a genuine, meaningful explanation or if it is vague, dismissive, or unsatisfactory.

A satisfactory remark:
- Gives a real reason (e.g., "was in back-to-back meetings", "had an urgent production issue", "was on leave")
- Is specific and honest
- Shows awareness of the missed check-in

An unsatisfactory remark:
- Is too vague ("was busy", "forgot", "nothing", "idk")
- Is dismissive or sarcastic ("who cares", "does it matter")
- Is nonsensical or gibberish
- Is extremely short with no real content (1-2 words)

Reply with ONLY valid JSON in this exact format:
{"is_satisfactory": true, "reason": "Brief explanation in 1 sentence"}"""


async def _get_previous_responses(user_id: str, checkin_type: str, limit: int = 3) -> list:
    """Fetch the user's most recent responded check-ins (excluding the latest one being submitted)."""
    db = get_database()
    cursor = db.daily_checkins.find(
        {"user_id": user_id, "checkin_type": checkin_type, "response": {"$exists": True, "$ne": None}},
    ).sort("created_at", -1).skip(1).limit(limit)
    return await cursor.to_list(length=limit)


async def analyze_response_repetition(
    checkin_id: str,
    user_id: str,
    checkin_type: str,
    current_response: str,
    user_name: str,
) -> None:
    """Compare current response with previous responses to detect repeated tasks."""
    # Retrieve the last 3 check-ins for analysis (W190)
    prev = await _get_previous_responses(user_id, checkin_type, limit=3)
    if not prev:
        return

    prev_text = prev[0].get("response", "").strip()
    if not prev_text:
        return

    current_clean = current_response.strip().lower()
    prev_clean = prev_text.strip().lower()

    # Optimization: local detection for exact matches or trivial same replies (W191)
    is_exact_match = current_clean == prev_clean
    is_trivial_same = current_clean in [
        "same", "same as yesterday", "no changes", "same as above",
        "same task", "still working on the same task", "nothing new"
    ] or len(current_clean) < 10

    if is_exact_match or is_trivial_same:
        logger.info("Local repetition match detected, skipping AI call.")
        parsed = {
            "is_repeated": True,
            "same_task": prev_text[:100],
            "confidence": 100,
            "reason": "The response is identical or indicates no change from the previous check-in."
        }
    else:
        # Build prompt comparing against the last 3 check-ins (W190)
        prev_texts = []
        for i, p in enumerate(prev):
            t = p.get("response", "").strip()
            if t:
                prev_texts.append(f"Previous response {i+1}: \"{t[:300]}\"")

        prompt = (
            f"{chr(10).join(prev_texts)}\n"
            f"Current response: \"{current_response[:500]}\""
        )

        try:
            result = await ai_client.chat_completion(
                system_prompt=REPETITION_SYSTEM_PROMPT,
                user_prompt=prompt,
                temperature=0.3,
            )
            import json
            parsed = json.loads(result)
        except Exception as e:
            logger.warning("AI repetition analysis failed: %s", e)
            return

    if not parsed.get("is_repeated"):
        return

    await send_notification(
        user_id=user_id,
        type_="ai_checkin_repeated",
        title="Check-in: Repeated Task Detected",
        message=parsed.get("reason", f"You mentioned working on the same task again. Need help unblocking it?"),
        entity_type="checkin",
        entity_id=checkin_id,
        link="/dashboard/ai/checkins",
    )

    db = get_database()
    admin_cursor = db.users.find({"role": {"$in": ["admin", "team_lead", "hr"]}})
    async for admin in admin_cursor:
        admin_id = str(admin["_id"])
        await send_notification(
            user_id=admin_id,
            type_="ai_admin_alert",
            title=f"Task Stalled: {user_name}",
            message=parsed.get("reason", f"{user_name} appears to be working on the same task across check-ins."),
            entity_type="checkin",
            entity_id=checkin_id,
            link="/dashboard/ai/checkins/admin",
        )


async def analyze_remark_quality(
    checkin_id: str,
    user_id: str,
    current_remark: str,
    user_name: str,
) -> bool:
    """Check if the remark is a genuine explanation or vague/dismissive.
    Notify user if unsatisfactory. Returns True if satisfactory, False otherwise."""
    prompt = f"Remark: \"{current_remark[:500]}\""
    try:
        result = await ai_client.chat_completion(
            system_prompt=REMARK_QUALITY_SYSTEM_PROMPT,
            user_prompt=prompt,
            temperature=0.3,
        )
        import json
        parsed = json.loads(result)
    except Exception as e:
        logger.warning("AI remark quality analysis failed: %s", e)
        return True

    if parsed.get("is_satisfactory", True):
        return True

    await send_notification(
        user_id=user_id,
        type_="ai_checkin",
        title="Remark Needs Improvement",
        message=parsed.get("reason", "Your remark was too vague. Please provide a genuine reason for missing the check-in."),
        entity_type="checkin",
        entity_id=checkin_id,
        link="/dashboard/ai/checkins",
    )
    return False


async def analyze_remark_concern(
    checkin_id: str,
    user_id: str,
    current_remark: str,
    user_name: str,
) -> None:
    """Analyze a skip remark for potential concerns and notify admin if needed."""
    prompt = f"Remark: \"{current_remark[:500]}\""

    try:
        result = await ai_client.chat_completion(
            system_prompt=REMARK_SYSTEM_PROMPT,
            user_prompt=prompt,
            temperature=0.3,
        )
        import json
        parsed = json.loads(result)
    except Exception as e:
        logger.warning("AI remark analysis failed: %s", e)
        return

    if not parsed.get("has_concern"):
        return

    db = get_database()
    admin_cursor = db.users.find({"role": {"$in": ["admin", "team_lead", "hr"]}})
    async for admin in admin_cursor:
        admin_id = str(admin["_id"])
        concern_type = parsed.get("concern_type", "other")
        severity = parsed.get("severity", "low")
        reason = parsed.get("reason", "A team member's check-in remark raised a concern.")

        await send_notification(
            user_id=admin_id,
            type_="ai_admin_alert",
            title=f"[{severity.upper()}] {user_name}: {concern_type.replace('_', ' ').title()}",
            message=f"Severity: {severity.upper()}. Reason: {reason}",
            entity_type="checkin",
            entity_id=checkin_id,
            link="/dashboard/ai/checkins/admin",
        )
