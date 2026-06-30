from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings
from datetime import datetime, timezone, timedelta
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
    try:
        await db.db.notifications.drop_index("created_at_1")
    except Exception:
        pass
    await db.db.notifications.create_index("created_at", expireAfterSeconds=2592000)
    await db.db.notifications.create_index([("user_id", 1), ("read", 1), ("created_at", -1)])

    # ── chat_conversations ──
    await db.db.chat_conversations.create_index("user_id")
    await db.db.chat_conversations.create_index("project_id")
    await db.db.chat_conversations.create_index("status")
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
    try:
        await db.db.projects.create_index([("tenant_id", 1), ("name", 1)], unique=True)
    except Exception as e:
        logger.warning(f"Could not create unique index projects.name per tenant: {e}")

    # ── off_project_tasks ──
    await db.db.off_project_tasks.create_index("assignee_id")
    await db.db.off_project_tasks.create_index("created_by")
    await db.db.off_project_tasks.create_index("status")
    await db.db.off_project_tasks.create_index("priority")
    await db.db.off_project_tasks.create_index("due_date")

    # ── task_completion_confirmations ──
    await db.db.task_completion_confirmations.create_index("developer_id")
    await db.db.task_completion_confirmations.create_index("status")
    await db.db.task_completion_confirmations.create_index("task_id")
    await db.db.task_completion_confirmations.create_index("project_id")
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
    await db.db.meetings.create_index([("date", 1), ("time", 1)])

    # ── epics ──
    await db.db.epics.create_index("project_id")
    try:
        await db.db.epics.create_index([("project_id", 1), ("name", 1)], unique=True)
    except Exception as e:
        logger.warning(f"Could not create unique index epics.name per project: {e}")

    # ── workflows ──
    await db.db.workflows.create_index("project_id")
    await db.db.workflows.create_index("created_by")
    try:
        await db.db.workflows.create_index([("project_id", 1), ("name", 1)], unique=True)
    except Exception as e:
        logger.warning(f"Could not create unique index workflows.name per project: {e}")

    # ── whiteboards ──
    await db.db.whiteboards.create_index("project_id")
    await db.db.whiteboards.create_index("created_by")
    await db.db.whiteboards.create_index("is_public")
    await db.db.whiteboards.create_index("updated_at")
    try:
        await db.db.whiteboards.create_index([("project_id", 1), ("name", 1)], unique=True)
    except Exception as e:
        logger.warning(f"Could not create unique index whiteboards.name per project: {e}")

    # ── leave_settings ──
    await db.db.leave_settings.create_index("year")
    await db.db.leave_settings.create_index("project_id")
    try:
        await db.db.leave_settings.create_index([("project_id", 1), ("year", 1)], unique=True)
    except Exception as e:
        logger.warning(f"Could not create unique index leave_settings per project and year: {e}")

    # ── leaves ──
    await db.db.leaves.create_index("user_id")
    await db.db.leaves.create_index("status")
    await db.db.leaves.create_index("start_date")
    await db.db.leaves.create_index("end_date")
    await db.db.leaves.create_index([("user_id", 1), ("status", 1)])
    await db.db.leaves.create_index([("user_id", 1), ("start_date", 1), ("end_date", 1)])

    # ── sprints ──
    await db.db.sprints.create_index("project_id")
    await db.db.sprints.create_index([("project_id", 1), ("status", 1)])
    try:
        await db.db.sprints.create_index([("project_id", 1), ("name", 1)], unique=True)
    except Exception as e:
        logger.warning(f"Could not create unique index sprints.name per project: {e}")

    # ── issue_links ──
    await db.db.issue_links.create_index("source_id")
    await db.db.issue_links.create_index("target_id")
    try:
        await db.db.issue_links.create_index([("source_id", 1), ("target_id", 1), ("link_type", 1)], unique=True)
    except Exception as e:
        logger.warning(f"Could not create unique index issue_links compound: {e}")

    # ── oauth_tokens ──
    await db.db.oauth_tokens.create_index("user_id")
    await db.db.oauth_tokens.create_index([("user_id", 1), ("provider", 1)])

    # ── teams_cache ──
    await db.db.teams_cache.create_index("expires_at", expireAfterSeconds=0)

    # ── custom_fields ──
    await db.db.custom_fields.create_index("project_id")
    try:
        await db.db.custom_fields.create_index([("project_id", 1), ("name", 1)], unique=True)
    except Exception as e:
        logger.warning(f"Could not create unique index custom_fields.name per project: {e}")

    # ── holidays ──
    await db.db.holidays.create_index("year")
    try:
        await db.db.holidays.create_index([("year", 1), ("date", 1)], unique=True)
    except Exception as e:
        logger.warning(f"Could not create unique index holidays.date per year: {e}")

    # ── users ── additional indexes
    await db.db.users.create_index("name")

    # ── otp_requests ──
    # TTL: auto-delete documents older than 1 hour
    await db.db.otp_requests.create_index("requested_at", expireAfterSeconds=3600)
    # Fast lookups for per-IP and per-email rate limiting in auth_service
    await db.db.otp_requests.create_index([("ip_address", 1), ("requested_at", 1)])
    await db.db.otp_requests.create_index([("email", 1), ("requested_at", 1)])

    # ── rate_limits ──
    # TTL: auto-delete documents not updated for 120 seconds (2× the 60s window).
    # Drop-and-recreate to fix any wrong TTL value from previous deploys.
    try:
        await db.db.rate_limits.drop_index("updated_at_1")
    except Exception:
        pass  # Index may not exist yet on first deploy — that's fine
    await db.db.rate_limits.create_index("updated_at", expireAfterSeconds=120)

    # ── Startup purge: remove stale rate_limit documents ──────────────────
    # Documents whose ALL timestamps are older than the largest window (60s)
    # should have been auto-expired by TTL, but TTL runs every ~60s on MongoDB.
    # An eager startup purge guarantees a fresh slate after every Render redeploy.
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        result = await db.db.rate_limits.delete_many(
            {"timestamps": {"$not": {"$gt": cutoff}}}
        )
        if result.deleted_count:
            logger.info(
                f"[Startup] Purged {result.deleted_count} stale rate_limit document(s) "
                "(all timestamps expired)."
            )
    except Exception as e:
        logger.warning(f"[Startup] Could not purge stale rate_limit documents: {e}")

    logger.info("MongoDB indexes ensured")


def get_database():
    return db.db

