import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.jobstores.memory import MemoryJobStore

from database import get_database
from helpers.notification_sender import send_notification
from helpers.ai_checkin_generator import generate_morning_checkin, generate_evening_checkin
from bson import ObjectId

logger = logging.getLogger(__name__)

IST = "Asia/Kolkata"
_ist_tz = ZoneInfo(IST)

_scheduler_instance: AsyncIOScheduler | None = None


def create_scheduler() -> AsyncIOScheduler:
    global _scheduler_instance
    jobstores = {"default": MemoryJobStore()}
    _scheduler_instance = AsyncIOScheduler(jobstores=jobstores, timezone=IST)
    return _scheduler_instance


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler_instance


def register_checkin_jobs(scheduler: AsyncIOScheduler):
    """Register daily morning and evening check-in jobs."""
    scheduler.add_job(
        _send_morning_checkins_job,
        CronTrigger(hour=9, minute=25, day_of_week="mon-sat"),
        id="daily_morning_checkin",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_evening_checkins_job,
        CronTrigger(hour=17, minute=15, day_of_week="mon-sat"),
        id="daily_evening_checkin",
        replace_existing=True,
    )
    scheduler.add_job(
        _check_sprint_deadlines_job,
        CronTrigger(hour=9, minute=0, day_of_week="mon-sat"),
        id="daily_sprint_deadline_check",
        replace_existing=True,
    )
    logger.info("Registered daily check-in jobs and sprint deadline check (09:00 AM IST)")


def schedule_force_assign(
    scheduler: AsyncIOScheduler | None,
    confirmation_id: str,
    project_id: str,
    developer_id: str,
    task_id: str,
):
    """Schedule a force-assign job 15 minutes from now."""
    if scheduler is None:
        scheduler = _scheduler_instance
    if scheduler is None:
        return

    run_at = datetime.now() + timedelta(minutes=15)
    scheduler.add_job(
        _force_assign_task_job,
        DateTrigger(run_date=run_at),
        id=f"force_assign_{confirmation_id}",
        replace_existing=True,
        args=[confirmation_id, project_id, developer_id, task_id],
    )
    logger.info(
        "Scheduled force-assign for confirmation %s at %s", confirmation_id, run_at
    )


# ── Job implementations (APScheduler runs these) ────────────────────────────

async def _send_morning_checkins_job():
    db = get_database()
    
    # Skip if today is Saturday and not marked as working day
    today = datetime.now(_ist_tz)
    if today.weekday() == 5:
        policy = await db["leave_settings"].find_one({"year": today.year})
        if policy and not policy.get("saturday_working", True):
            logger.info("Saturday is not a working day — skipping morning check-in")
            return
    
    # Decoupled lazy imports
    from models.ai_task_monitor import DailyCheckin, CheckinType, CheckinResponseStatus

    async for user in db.users.find({}):
        if user.get("role", "").lower() == "admin":
            continue
        notif_prefs = user.get("settings", {}).get("notifications", {})
        if not notif_prefs.get("ai_checkin", True):
            continue
        user_id = str(user["_id"])
        try:
            question = await generate_morning_checkin(user)
            checkin = DailyCheckin(
                user_id=user_id,
                checkin_type=CheckinType.MORNING,
                question=question,
                status=CheckinResponseStatus.PENDING,
            )
            doc = checkin.model_dump(by_alias=True, exclude={"id"})
            result = await db.daily_checkins.insert_one(doc)
            await send_notification(
                user_id=user_id,
                type_="ai_checkin",
                title="Daily Check-in: Morning",
                message=question,
                entity_type="checkin",
                entity_id=str(result.inserted_id),
                link="/dashboard/ai/checkins",
            )
        except Exception as e:
            logger.exception("Failed to send morning check-in to user %s: %s", user_id, e)


async def _send_evening_checkins_job():
    db = get_database()
    
    # Skip if today is Saturday and not marked as working day
    today = datetime.now(_ist_tz)
    if today.weekday() == 5:
        policy = await db["leave_settings"].find_one({"year": today.year})
        if policy and not policy.get("saturday_working", True):
            logger.info("Saturday is not a working day — skipping evening check-in")
            return
    
    # Decoupled lazy imports
    from models.ai_task_monitor import DailyCheckin, CheckinType, CheckinResponseStatus

    async for user in db.users.find({}):
        if user.get("role", "").lower() == "admin":
            continue
        notif_prefs = user.get("settings", {}).get("notifications", {})
        if not notif_prefs.get("ai_checkin", True):
            continue
        user_id = str(user["_id"])
        try:
            question = await generate_evening_checkin(user)
            checkin = DailyCheckin(
                user_id=user_id,
                checkin_type=CheckinType.EVENING,
                question=question,
                status=CheckinResponseStatus.PENDING,
            )
            doc = checkin.model_dump(by_alias=True, exclude={"id"})
            result = await db.daily_checkins.insert_one(doc)
            await send_notification(
                user_id=user_id,
                type_="ai_checkin",
                title="Daily Check-in: Evening",
                message=question,
                entity_type="checkin",
                entity_id=str(result.inserted_id),
                link="/dashboard/ai/checkins",
            )
        except Exception as e:
            logger.exception("Failed to send evening check-in to user %s: %s", user_id, e)


async def _force_assign_task_job(
    confirmation_id: str, project_id: str, developer_id: str, task_id: str
):
    """Forcefully assign the same task to the developer after 15 minutes if appropriate."""
    db = get_database()
    try:
        # Verify if the confirmation is still pending and has not been approved/manually updated
        conf = await db.task_completion_confirmations.find_one({"_id": ObjectId(confirmation_id)})
        if not conf or conf.get("status") != "pending":
            logger.info("Confirmation %s is no longer pending (status: %s) — skipping force-assign", confirmation_id, conf.get("status") if conf else "None")
            return

        # Verify task state
        task = await db.tasks.find_one({"_id": ObjectId(task_id)})
        if not task:
            logger.info("Task %s not found — skipping force-assign", task_id)
            return

        # Skip if the task was already completed/resolved or has a different assignee
        if task.get("status") in ("completed", "done", "resolved"):
            logger.info("Task %s is already completed/resolved — skipping force-assign", task_id)
            return

        # Update confirmation status
        await db.task_completion_confirmations.update_one(
            {"_id": ObjectId(confirmation_id)},
            {
                "$set": {
                    "status": "rejected",
                    "ai_evaluation": "Forcefully assigned after 15-minute wait period.",
                }
            },
        )
        # Re-assign the same task using a timezone-aware UTC datetime
        await db.tasks.update_one(
            {"_id": ObjectId(task_id)},
            {
                "$set": {
                    "assignee_id": developer_id,
                    "status": "todo",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        # Notify developer
        task = await db.tasks.find_one({"_id": ObjectId(task_id)})
        if task:
            await send_notification(
                user_id=developer_id,
                type_="ai_task_assigned",
                title="Task Forcefully Assigned",
                message=f"The task '{task.get('title')}' has been forcefully assigned to you. Please begin working on it.",
                entity_type="task",
                entity_id=task_id,
                link=f"/dashboard/projects/{project_id}/kanban",
            )
        logger.info(
            "Force-assigned task %s to developer %s", task_id, developer_id
        )
    except Exception as e:
        logger.exception("Force-assign job failed: %s", e)


async def _check_sprint_deadlines_job():
    """Daily check for active, in-progress, or testing sprints whose end date is in exactly 2 days."""
    db = get_database()
    try:
        now_ist = datetime.now(_ist_tz)
        target_date_start = (now_ist + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        target_date_end = target_date_start + timedelta(days=1)
        
        target_utc_start = target_date_start.astimezone(timezone.utc)
        target_utc_end = target_date_end.astimezone(timezone.utc)

        sprints_cursor = db.sprints.find({
            "status": {"$in": ["active", "in_progress", "testing"]},
            "end_date": {"$gte": target_utc_start, "$lt": target_utc_end}
        })
        
        async for sprint in sprints_cursor:
            sprint_id = str(sprint["_id"])
            sprint_name = sprint.get("name", "Sprint")
            end_date_str = sprint["end_date"].astimezone(_ist_tz).strftime("%A, %b %d")
            
            recipients = set(sprint.get("team_member_ids", []))
            if sprint.get("team_lead_id"):
                recipients.add(sprint["team_lead_id"])
                
            for r_id in recipients:
                await send_notification(
                    user_id=r_id,
                    type_="sprint_deadline_approaching",
                    title="Sprint Deadline Approaching",
                    message=f"Sprint '{sprint_name}' is ending soon on {end_date_str}. Please review and update your tasks.",
                    entity_type="sprint",
                    entity_id=sprint_id,
                    link=f"/dashboard/sprints/{sprint_id}",
                )
            logger.info("Sent deadline approaching notification for sprint %s (%s)", sprint_id, sprint_name)
    except Exception as e:
        logger.exception("Sprint deadline check job failed: %s", e)

