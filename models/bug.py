from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field, model_validator, ConfigDict
from bson import ObjectId
from models.user import ObjectIdStr


class BugSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    BLOCKER = "blocker"


class BugStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    VERIFIED = "verified"
    WONT_FIX = "wont_fix"
    DUPLICATE = "duplicate"
    FIX_IN_PROGRESS = "fix_in_progress"
    FIX_READY = "fix_ready"
    FIX_FAILED = "fix_failed"


class EnvironmentType(str, Enum):
    WINDOWS = "windows"
    MAC = "mac"
    LINUX = "linux"
    ANDROID = "android"
    IPHONE = "iphone"
    CUSTOM = "custom"


class EnvironmentInfo(BaseModel):
    os: Optional[EnvironmentType] = None
    custom_os: Optional[str] = None
    custom_fields: Optional[dict] = None

    @model_validator(mode="after")
    def validate_environment(self) -> 'EnvironmentInfo':
        if not self.os and not self.custom_os and not self.custom_fields:
            raise ValueError("At least one environment field (os, custom_os, or custom_fields) must be provided")
        return self

    model_config = ConfigDict(extra="forbid")


class Attachment(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    name: str
    filename: Optional[str] = None
    url: str
    thumbnail_link: Optional[str] = None
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


class BugBase(BaseModel):
    title: str
    description: str = ""
    severity: BugSeverity = BugSeverity.MEDIUM
    status: BugStatus = BugStatus.OPEN
    steps_to_reproduce: str = ""
    expected_result: str = ""
    actual_result: str = ""
    environment: EnvironmentInfo = EnvironmentInfo(os=EnvironmentType.CUSTOM)
    is_regression: bool = False
    attachments: List[Attachment] = []
    status_history: List[dict] = Field(default_factory=list)


class BugCreate(BugBase):
    title: str = Field(max_length=500)
    description: str = Field(default="", max_length=5000)
    steps_to_reproduce: str = Field(default="", max_length=5000)
    expected_result: str = Field(default="", max_length=5000)
    actual_result: str = Field(default="", max_length=5000)
    project_id: ObjectIdStr
    assignee_id: Optional[ObjectIdStr] = None
    related_task: Optional[ObjectIdStr] = None
    custom_field_values: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="forbid")


class BugUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=500)
    description: Optional[str] = None
    severity: Optional[BugSeverity] = None
    status: Optional[BugStatus] = None
    steps_to_reproduce: Optional[str] = None
    expected_result: Optional[str] = None
    actual_result: Optional[str] = None
    environment: Optional[EnvironmentInfo] = None
    assignee: Optional[ObjectIdStr] = None
    assignee_id: Optional[ObjectIdStr] = None
    related_task: Optional[ObjectIdStr] = None
    is_regression: Optional[bool] = None
    attachments: Optional[List[Attachment]] = None
    custom_field_values: Optional[Dict[str, Any]] = None
    rollback_reason: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class BugInDB(BugBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    project_id: ObjectIdStr
    reporter: ObjectIdStr
    assignee: Optional[ObjectIdStr] = None
    assignee_id: Optional[ObjectIdStr] = None
    related_task: Optional[ObjectIdStr] = None
    custom_field_values: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class BugResponse(BugBase):
    id: str = Field(alias="_id")
    project_id: ObjectIdStr
    reporter: ObjectIdStr
    assignee: Optional[ObjectIdStr] = None
    assignee_id: Optional[ObjectIdStr] = None
    related_task: Optional[ObjectIdStr] = None
    custom_field_values: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )
