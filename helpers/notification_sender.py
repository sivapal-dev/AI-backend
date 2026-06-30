"""
Notification sender utility - handles sending notifications via SSE and storing in DB.
"""
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from bson import ObjectId
from database import get_database
import asyncio
import logging
from services.email_service import email_service

logger = logging.getLogger(__name__)


# In-memory connection manager: {user_id: set of asyncio.Queue objects}
_connections: Dict[str, set] = {}

# Rate limiter: {user_id: [timestamp, ...]} for notification flood prevention
_notification_rate_limit: Dict[str, list] = {}
_NOTIFICATION_RATE_MAX = 100  # max notifications per user per window
_NOTIFICATION_RATE_WINDOW = 60  # seconds


# High-priority notification types that should trigger email notifications
# These correspond to keys in UserSettings.notification_preferences
_HIGH_PRIORITY_TYPES = {
    "task_assigned",
    "meeting_scheduled",
    "meeting_reminder",
    "meeting_cancelled",
    "project_invite",
    "project_deadline",
    "ai_task_confirmation",
    "ai_checkin",
    "ai_task_assigned",
    "ai_admin_alert",
    "bug_reported",
    "bug_assigned",
    "sprint_assigned",
    "sprint_member_added",
    "sprint_member_removed",
    "sprint_started",
    "sprint_completed",
    "sprint_status_changed",
    "leave_approved",
    "leave_rejected",
}


def _format_notification(
    user_id: str,
    type_: str,
    title: str,
    message: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    link: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Dict[str, Any]:
    """Format a notification document for storage and SSE push."""
    return {
        "user_id": user_id,
        "type": type_.value if hasattr(type_, "value") else str(type_),
        "title": title,
        "message": message,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "link": link,
        "read": False,
        "created_at": datetime.now(timezone.utc),
        "metadata": metadata,
    }


async def _check_notification_rate_limit(user_id: str) -> bool:
    try:
        from redis_client import get_redis
        r = get_redis()
        if r:
            now = datetime.now(timezone.utc).timestamp()
            cutoff = now - _NOTIFICATION_RATE_WINDOW
            key = f"notification_rate_limit:{user_id}"
            
            pipe = r.pipeline()
            pipe.zremrangebyscore(key, "-inf", cutoff)
            pipe.zcard(key)
            pipe.zadd(key, {str(now): now})
            pipe.expire(key, _NOTIFICATION_RATE_WINDOW)
            res = await pipe.execute()
            
            card = res[1]
            if card >= _NOTIFICATION_RATE_MAX:
                return False
            return True
    except Exception as e:
        logger.debug(f"Redis rate limiter check failed (falling back to memory): {e}")

    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - _NOTIFICATION_RATE_WINDOW
    timestamps = _notification_rate_limit.get(user_id, [])
    timestamps = [t for t in timestamps if t > cutoff]
    if len(timestamps) >= _NOTIFICATION_RATE_MAX:
        return False
    timestamps.append(now.timestamp())
    _notification_rate_limit[user_id] = timestamps
    return True


async def send_notification(
    user_id: str,
    type_: str,
    title: str,
    message: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    link: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> str:
    """
    Store notification in DB and push to connected SSE clients.

    Returns the notification ID (as string).
    """
    if not await _check_notification_rate_limit(user_id):
        logger.warning(f"Notification rate limit exceeded for user {user_id}, dropping notification")
        raise ValueError("Rate limit exceeded")

    db = get_database()
    notification_doc = _format_notification(
        user_id, type_, title, message, entity_type, entity_id, link, metadata
    )

    # Insert into DB
    result = await db.notifications.insert_one(notification_doc)
    notification_id = str(result.inserted_id)

    # Prepare SSE payload
    sse_data = {
        "id": notification_id,
        "type": type_.value if hasattr(type_, "value") else str(type_),
        "title": title,
        "message": message,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "link": link,
        "created_at": notification_doc["created_at"].isoformat(),
        "metadata": metadata,
    }

    # Push to all connected SSE clients for this user
    if user_id in _connections:
        disconnected = set()
        for queue in _connections[user_id]:
            try:
                await queue.put(sse_data)
            except Exception:
                disconnected.add(queue)

        # Clean up dead connections
        for queue in disconnected:
            _connections[user_id].remove(queue)
        if not _connections[user_id]:
            del _connections[user_id]

    # ── Send email notification for high-priority types (fire-and-forget) ──────
    if type_ in _HIGH_PRIORITY_TYPES:
        async def _send_email_task():
            try:
                db = get_database()
                user_doc = await db.users.find_one(
                    {"_id": ObjectId(user_id)},
                    {"settings": 1, "email": 1, "name": 1}
                )
                if not user_doc:
                    return

                settings = user_doc.get("settings", {})
                if not settings.get("email_notifications", True):
                    return  # User disabled email notifications

                notif_prefs = settings.get("notifications", {})
                if not notif_prefs.get(type_, True):
                    return  # This notification type is disabled

                user_email = user_doc.get("email")
                user_name = user_doc.get("name", "there")
                if not user_email:
                    return

                # Format notification type for display (e.g., "task_assigned" → "Task Assigned")
                display_type = type_.replace("_", " ").title()
                parts = display_type.split(" ")
                parts = ["AI" if p == "Ai" else p for p in parts]
                display_type = " ".join(parts)

                await email_service.send_notification_email(
                    to_email=user_email,
                    user_name=user_name,
                    notification_type=display_type,
                    notification_title=title,
                    notification_message=message,
                    action_link=link or "",
                )
            except Exception as e:
                logger.error(f"Failed to send notification email to user {user_id}: {e}")

        asyncio.create_task(_send_email_task())

    return notification_id


async def notify_role_watchers(
    notify_roles: List[str],
    type_: str,
    title: str,
    message: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    link: Optional[str] = None,
    exclude_user_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """
    Send an in-app notification (no email) to all users with specified roles.
    Skips the user identified by exclude_user_id, if provided.
    """
    db = get_database()
    query: Dict[str, Any] = {"role": {"$in": notify_roles}}
    if exclude_user_id:
        query["_id"] = {"$ne": ObjectId(exclude_user_id)}
    cursor = db.users.find(query, {"_id": 1})
    async for user_doc in cursor:
        await send_notification(
            user_id=str(user_doc["_id"]),
            type_=type_,
            title=title,
            message=message,
            entity_type=entity_type,
            entity_id=entity_id,
            link=link,
            metadata=metadata,
        )


def add_connection(user_id: str, queue: asyncio.Queue) -> None:
    """Register a new SSE connection for a user."""
    if user_id not in _connections:
        _connections[user_id] = set()
    _connections[user_id].add(queue)


def remove_connection(user_id: str, queue: asyncio.Queue) -> None:
    """Unregister an SSE connection for a user."""
    if user_id in _connections:
        _connections[user_id].discard(queue)
        if not _connections[user_id]:
            _connections.pop(user_id, None)
