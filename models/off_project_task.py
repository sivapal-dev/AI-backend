from enum import Enum
from typing import Optional, List
from datetime import datetime, timezone
from bson import ObjectId
from pydantic import BaseModel, Field, ConfigDict


class OffProjectTaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class OffProjectTaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class OffProjectTaskBase(BaseModel):
    title: str
    description: str = ""
    priority: OffProjectTaskPriority = OffProjectTaskPriority.MEDIUM
    status: OffProjectTaskStatus = OffProjectTaskStatus.TODO
    estimated_hours: Optional[float] = Field(default=None, ge=0)
    actual_hours: Optional[float] = Field(default=None, ge=0)
    notes: str = ""
    status_history: List[dict] = Field(default_factory=list)


class OffProjectTaskCreate(OffProjectTaskBase):
    title: str = Field(max_length=500)
    description: str = Field(default="", max_length=5000)
    assignee_id: Optional[str] = None
    assignee_name: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class OffProjectTaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=500)
    description: Optional[str] = None
    priority: Optional[OffProjectTaskPriority] = None
    status: Optional[OffProjectTaskStatus] = None
    estimated_hours: Optional[float] = Field(default=None, ge=0)
    actual_hours: Optional[float] = Field(default=None, ge=0)
    notes: Optional[str] = None
    rollback_reason: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class OffProjectTaskInDB(OffProjectTaskBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    assignee_id: str
    assignee_name: str
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class OffProjectTaskResponse(OffProjectTaskBase):
    id: str = Field(alias="_id")
    assignee_id: str
    assignee_name: str
    created_by: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )
