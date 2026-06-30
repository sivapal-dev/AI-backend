from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings
import logging

settings = get_settings()
logger = logging.getLogger(__name__)


class Database:
    client: AsyncIOMotorClient = None
    db = None


db = Database()


async def connect_db():
    db.client = AsyncIOMotorClient(settings.mongodb_uri)
    db.db = db.client[settings.database_name]
    logger.info(f"Connected to MongoDB: {settings.database_name}")
    await _ensure_indexes()


async def close_db():
    if db.client:
        db.client.close()
        logger.info("Disconnected from MongoDB")


async def _ensure_indexes():
    """Create indexes for optimal query performance."""
    # ── users ──
    await db.db.users.create_index("email", unique=True)
    await db.db.users.create_index("role")

    # ── tasks ──
    await db.db.tasks.create_index("project_id")
    await db.db.tasks.create_index("assignee_id")
    await db.db.tasks.create_index("reporter")
    await db.db.tasks.create_index("status")
    await db.db.tasks.create_index([("project_id", 1), ("status", 1)])
    await db.db.tasks.create_index([("assignee_id", 1), ("status", 1)])

    # ── daily_checkins ──
    await db.db.daily_checkins.create_index("user_id")
    await db.db.daily_checkins.create_index("created_at")
    await db.db.daily_checkins.create_index("status")
    await db.db.daily_checkins.create_index([("user_id", 1), ("created_at", -1)])
    await db.db.daily_checkins.create_index([("created_at", -1), "checkin_type"])

    # ── notifications ──
    await db.db.notifications.create_index("user_id")
    await db.db.notifications.create_index("read")
    await db.db.notifications.create_index("created_at")
    await db.db.notifications.create_index([("user_id", 1), ("read", 1), ("created_at", -1)])

    # ── chat_conversations ──
    await db.db.chat_conversations.create_index("user_id")
    await db.db.chat_conversations.create_index([("user_id", 1), ("updated_at", -1)])

    # ── ams_tickets ──
    await db.db.ams_tickets.create_index("ticket_key", unique=True)
    await db.db.ams_tickets.create_index("project_id")
    await db.db.ams_tickets.create_index("automation_status")
    await db.db.ams_tickets.create_index("approval_status")
    await db.db.ams_tickets.create_index("linked_bug_id")
    await db.db.ams_tickets.create_index([("project_id", 1), ("created_at", -1)])

    # ── projects ──
    await db.db.projects.create_index("team")
    await db.db.projects.create_index("status")

    # ── off_project_tasks ──
    await db.db.off_project_tasks.create_index("assignee_id")
    await db.db.off_project_tasks.create_index("created_by")
    await db.db.off_project_tasks.create_index("status")

    # ── task_completion_confirmations ──
    await db.db.task_completion_confirmations.create_index("developer_id")
    await db.db.task_completion_confirmations.create_index("status")
    await db.db.task_completion_confirmations.create_index([("developer_id", 1), ("status", 1)])

    # ── documents ──
    await db.db.documents.create_index("user_id")
    await db.db.documents.create_index("project_id")
    await db.db.documents.create_index("status")
    await db.db.documents.create_index("created_at")
    await db.db.documents.create_index([("project_id", 1), ("created_at", -1)])
    await db.db.documents.create_index([("user_id", 1), ("created_at", -1)])

    # ── bugs ──
    await db.db.bugs.create_index("project_id")
    await db.db.bugs.create_index("status")
    await db.db.bugs.create_index("assignee")
    await db.db.bugs.create_index("reporter")
    await db.db.bugs.create_index([("project_id", 1), ("status", 1)])

    # ── comments ──
    await db.db.comments.create_index([("entity_type", 1), ("entity_id", 1)])
    await db.db.comments.create_index("parent_id")

    # ── activity_logs ──
    await db.db.activity_logs.create_index([("entity_id", 1), ("entity_type", 1)])
    await db.db.activity_logs.create_index("user_id")
    await db.db.activity_logs.create_index("created_at")
    await db.db.activity_logs.create_index([("entity_type", 1), ("entity_id", 1), ("created_at", -1)])

    # ── meetings ──
    await db.db.meetings.create_index("project_id")
    await db.db.meetings.create_index("attendees")
    await db.db.meetings.create_index("date")

    # ── epics ──
    await db.db.epics.create_index("project_id")

    # ── workflows ──
    await db.db.workflows.create_index("project_id")

    # ── whiteboards ──
    await db.db.whiteboards.create_index("project_id")
    await db.db.whiteboards.create_index("created_by")

    # ── leave_settings ──
    await db.db.leave_settings.create_index("year")

    # ── leaves ──
    await db.db.leaves.create_index("user_id")
    await db.db.leaves.create_index("status")
    await db.db.leaves.create_index([("user_id", 1), ("status", 1)])

    # ── sprints ──
    await db.db.sprints.create_index("project_id")
    await db.db.sprints.create_index([("project_id", 1), ("status", 1)])

    # ── issue_links ──
    await db.db.issue_links.create_index("source_id")
    await db.db.issue_links.create_index("target_id")

    # ── oauth_tokens ──
    await db.db.oauth_tokens.create_index("user_id")
    await db.db.oauth_tokens.create_index([("user_id", 1), ("provider", 1)])

    # ── teams_cache ──
    await db.db.teams_cache.create_index("expires_at", expireAfterSeconds=0)

    # ── users ── additional indexes
    await db.db.users.create_index("name")

    logger.info("MongoDB indexes ensured")


def get_database():
    return db.db
