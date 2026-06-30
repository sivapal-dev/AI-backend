from .auth import router as auth
from .projects import router as projects
from .tasks import router as tasks
from .bugs import router as bugs
from .users import router as users
from .activity import router as activity
from .ai import router as ai
from .admin import router as admin
from .meetings import router as meetings
from .comments import router as comments
from .upload import router as upload
from .notifications import router as notifications
from .sprints import router as sprints
from .issue_links import router as issue_links
from .epics import router as epics
from .workflows import router as workflows
from .custom_fields import router as custom_fields
from .off_project_tasks import router as off_project_tasks
from .leaves import router as leaves
from .holidays import router as holidays
from .leave_settings import router as leave_settings
from .employee_holidays import router as employee_holidays
from .ai_task_monitor import router as ai_task_monitor
from .ai_daily_checkin import router as ai_daily_checkin
from .ai_chat import router as ai_chat
from .bugfix import router as bugfix
from .ams import router as ams
from .github import router as github
from .google_calendar import router as google_calendar
from .inbox import router as inbox
from .teams import router as teams

__all__ = [
    "auth",
    "users",
    "projects",
    "tasks",
    "epics",
    "sprints",
    "meetings",
    "leave_settings",
    "holidays",
    "employee_holidays",
    "leaves",
    "comments",
    "issue_links",
    "activity",
    "custom_fields",
    "notifications",
    "admin",
    "ai",
    "whiteboards",
    "upload",
    "workflows",
    "off_project_tasks",
    "bugs",
    "ai_task_monitor",
    "ai_chat",
    "ai_daily_checkin",
    "bugfix",
    "ams",
    "github",
    "google_calendar",
    "inbox",
    "teams",
]
