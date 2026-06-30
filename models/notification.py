from typing import Optional, List
from datetime import datetime, timezone
from enum import Enum
from bson import ObjectId
from pydantic import BaseModel, Field, ConfigDict


class NotificationType(str, Enum):
    """Enumeration of all valid notification types.

    Previously declared as `class NotificationType(str)` (a plain class, not an
    Enum), which meant the constants were just class attributes with no
    enforcement — any arbitrary string could be stored as the type field.
    """
    TASK_ASSIGNED = "task_assigned"
    TASK_UPDATED = "task_updated"
    TASK_COMMENTED = "task_commented"
    BUG_ASSIGNED = "bug_assigned"
    BUG_UPDATED = "bug_updated"
    BUG_REPORTED = "bug_reported"
    MEETING_SCHEDULED = "meeting_scheduled"
    MEETING_REMINDER = "meeting_reminder"
    MEETING_CANCELLED = "meeting_cancelled"
    PROJECT_INVITE = "project_invite"
    PROJECT_DEADLINE = "project_deadline"
    SYSTEM = "system"
    AI_TASK_CONFIRMATION = "ai_task_confirmation"
    AI_TASK_ASSIGNED = "ai_task_assigned"
    AI_CHECKIN = "ai_checkin"
    AI_ADMIN_ALERT = "ai_admin_alert"
    
    BUG_STATUS_CHANGED = "bug_status_changed"
    TASK_STATUS_CHANGED = "task_status_changed"
    AI_CHECKIN_REPEATED = "ai_checkin_repeated"
    LEAVE_APPROVED = "leave_approved"
    LEAVE_REJECTED = "leave_rejected"
    SPRINT_ASSIGNED = "sprint_assigned"
    SPRINT_COMPLETED = "sprint_completed"
    SPRINT_MEMBER_ADDED = "sprint_member_added"
    SPRINT_MEMBER_REMOVED = "sprint_member_removed"
    SPRINT_STARTED = "sprint_started"
    SPRINT_DEADLINE_APPROACHING = "sprint_deadline_approaching"
    
    EPIC_COMMENTED = "epic_commented"
    SPRINT_COMMENTED = "sprint_commented"
    WHITEBOARD_COMMENTED = "whiteboard_commented"
    DOCUMENT_COMMENTED = "document_commented"
    WORKFLOW_COMMENTED = "workflow_commented"
    LEAVE_COMMENTED = "leave_commented"
    MEETING_COMMENTED = "meeting_commented"
    PROJECT_COMMENTED = "project_commented"


class NotificationBase(BaseModel):
    user_id: str = Field(max_length=50)
    type: NotificationType
    title: str = Field(max_length=200)
    message: str = Field(max_length=1000)
    entity_type: Optional[str] = Field(default=None, max_length=100)
    entity_id: Optional[str] = Field(default=None, max_length=50)
    read: bool = False
    link: Optional[str] = Field(default=None, max_length=2048)
    metadata: Optional[dict] = None


class NotificationCreate(NotificationBase):
    pass


class NotificationInDB(NotificationBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class NotificationResponse(NotificationBase):
    id: str = Field(alias="_id")
    created_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )

