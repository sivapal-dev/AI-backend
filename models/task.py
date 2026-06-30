from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field, model_validator, ConfigDict
from bson import ObjectId
from models.user import ObjectIdStr


class TaskStatus(str, Enum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    CODE_REVIEW = "code_review"
    TESTING = "testing"
    DONE = "done"


class TaskRole(str, Enum):
    FRONTEND = "frontend"
    BACKEND = "backend"
    UI_UX = "ui_ux"
    QA = "qa"
    DEVOPS = "devops"
    FULLSTACK = "fullstack"


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskComplexity(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"
    VERY_COMPLEX = "very_complex"


class Attachment(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    name: str
    filename: Optional[str] = None
    url: str
    view_url: Optional[str] = None
    direct_url: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    uploaded_by: Optional[str] = None
    uploaded_by_name: Optional[str] = None
    drive_file_id: Optional[str] = None
    created_at: Optional[str] = None

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class TaskImage(BaseModel):
    url: str
    filename: str
    uploaded_at: str


class TaskBase(BaseModel):
    title: str
    description: str = ""
    role: TaskRole = TaskRole.FRONTEND
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.BACKLOG
    complexity: TaskComplexity = TaskComplexity.MEDIUM
    tags: List[str] = []
    estimated_hours: Optional[float] = None
    story_points: Optional[int] = None
    time_spent: int = 0
    remaining_hours: Optional[float] = None
    ai_generated: bool = False
    source_markdown: Optional[str] = None
    order: int = 0
    images: Optional[List[TaskImage]] = None
    assignee_id: Optional[ObjectIdStr] = None
    reporter_name: Optional[str] = None
    # Timer fields
    started_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    total_paused_ms: int = 0
    is_timer_running: bool = False


class TaskCreate(TaskBase):
    title: str = Field(max_length=500)
    description: str = Field(default="", max_length=5000)
    project_id: ObjectIdStr
    assignee: Optional[str] = None
    dependencies: List[ObjectIdStr] = []
    start_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    sprint: Optional[str] = None
    parent_id: Optional[ObjectIdStr] = None
    epic_id: Optional[ObjectIdStr] = None
    custom_field_values: Optional[Dict[str, Any]] = None
    skip_notification: bool = False

    @model_validator(mode="after")
    def validate_task_relations(self) -> 'TaskCreate':
        if self.parent_id and self.parent_id in self.dependencies:
            raise ValueError("parent_id cannot be in dependencies")
        if self.dependencies:
            # Deduplicate dependencies while preserving order
            self.dependencies = list(dict.fromkeys(self.dependencies))
        return self

    model_config = ConfigDict(extra="forbid")


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=500)
    description: Optional[str] = None
    role: Optional[TaskRole] = None
    priority: Optional[TaskPriority] = None
    status: Optional[TaskStatus] = None
    assignee: Optional[str] = None
    assignee_id: Optional[ObjectIdStr] = None
    complexity: Optional[TaskComplexity] = None
    tags: Optional[List[str]] = None
    estimated_hours: Optional[float] = None
    story_points: Optional[int] = None
    time_spent: Optional[int] = None
    remaining_hours: Optional[float] = None
    dependencies: Optional[List[ObjectIdStr]] = None
    start_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    sprint: Optional[str] = None
    order: Optional[int] = None
    attachments: Optional[List[Attachment]] = None
    images: Optional[List[TaskImage]] = None
    parent_id: Optional[ObjectIdStr] = None
    epic_id: Optional[ObjectIdStr] = None
    custom_field_values: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_task_relations(self) -> 'TaskUpdate':
        if self.parent_id and self.parent_id in (self.dependencies or []):
            raise ValueError("parent_id cannot be in dependencies")
        if self.dependencies:
            # Deduplicate dependencies while preserving order
            self.dependencies = list(dict.fromkeys(self.dependencies))
        return self

    model_config = ConfigDict(extra="forbid")


class TaskInDB(TaskBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    project_id: ObjectIdStr
    assignee: Optional[str] = None
    assignee_id: Optional[ObjectIdStr] = None
    reporter: ObjectIdStr
    dependencies: List[ObjectIdStr] = []
    attachments: List[Attachment] = []
    start_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    sprint: Optional[str] = None
    parent_id: Optional[ObjectIdStr] = None
    epic_id: Optional[ObjectIdStr] = None
    subtask_ids: List[ObjectIdStr] = []
    custom_field_values: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class TaskResponse(TaskBase):
    id: str = Field(alias="_id")
    project_id: ObjectIdStr
    assignee: Optional[str] = None
    assignee_id: Optional[ObjectIdStr] = None
    reporter: ObjectIdStr
    dependencies: List[ObjectIdStr] = []
    attachments: List[Attachment] = []
    start_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    sprint: Optional[str] = None
    parent_id: Optional[ObjectIdStr] = None
    epic_id: Optional[ObjectIdStr] = None
    subtask_ids: List[ObjectIdStr] = []
    custom_field_values: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )
