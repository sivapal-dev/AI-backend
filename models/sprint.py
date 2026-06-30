from enum import Enum
from typing import Optional, List
from datetime import datetime, timezone
from pydantic import BaseModel, Field, model_validator, ConfigDict
from bson import ObjectId


class SprintStatus(str, Enum):
    PLANNING = "planning"
    ACTIVE = "active"
    IN_PROGRESS = "in_progress"
    TESTING = "testing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SprintBase(BaseModel):
    name: str
    goal: Optional[str] = ""
    description: Optional[str] = ""
    priority: Optional[str] = "medium"  # low, medium, high, critical
    team_lead_id: Optional[str] = None
    team_member_ids: List[str] = Field(default_factory=list)
    estimated_hours: Optional[float] = 0.0
    tags: List[str] = Field(default_factory=list)
    attachments: List[dict] = Field(default_factory=list)  # [{filename, url, file_id, size}]
    status_history: List[dict] = Field(default_factory=list)  # [{status, changed_by, changed_at}]
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: SprintStatus = SprintStatus.PLANNING

    @model_validator(mode="after")
    def validate_dates(self) -> 'SprintBase':
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class SprintCreate(SprintBase):
    name: str = Field(max_length=500)
    project_id: str

    model_config = ConfigDict(extra="forbid")


class SprintUpdate(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    team_lead_id: Optional[str] = None
    team_member_ids: Optional[List[str]] = None
    estimated_hours: Optional[float] = None
    tags: Optional[List[str]] = None
    attachments: Optional[List[dict]] = None
    status_history: Optional[List[dict]] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: Optional[SprintStatus] = None

    @model_validator(mode="after")
    def validate_dates(self) -> 'SprintUpdate':
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self

    model_config = ConfigDict(extra="forbid")


class SprintInDB(SprintBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    project_id: str
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class SprintResponse(SprintBase):
    id: str = Field(alias="_id")
    project_id: str
    created_by: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )

