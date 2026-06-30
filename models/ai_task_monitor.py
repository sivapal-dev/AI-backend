from enum import Enum
from typing import Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator, ConfigDict
from bson import ObjectId


class ConfirmationStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DENIED = "denied"
    REJECTED = "rejected"


class CheckinType(str, Enum):
    MORNING = "morning"
    EVENING = "evening"


class CheckinResponseStatus(str, Enum):
    PENDING = "pending"
    RESPONDED = "responded"
    SKIPPED = "skipped"


class TaskCompletionConfirmation(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    task_id: str
    project_id: str
    developer_id: str
    new_task_id: Optional[str] = None
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    developer_remark: Optional[str] = Field(default=None, max_length=2000)
    ai_evaluation: Optional[str] = Field(default=None, max_length=5000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confirmed_at: Optional[datetime] = None

    @field_validator("task_id", "project_id", "developer_id", "new_task_id")
    @classmethod
    def check_object_ids(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip() == "":
            return v
        if not ObjectId.is_valid(v):
            raise ValueError("ID must be a valid 24-character hex string")
        return v

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class DailyCheckin(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    user_id: str
    checkin_type: CheckinType
    question: str = Field(max_length=1000)
    response: Optional[str] = Field(default=None, max_length=5000)
    remark: Optional[str] = Field(default=None, max_length=2000)          # Reason for skipping (only for skipped check-ins)
    status: CheckinResponseStatus = CheckinResponseStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    responded_at: Optional[datetime] = None

    @field_validator("user_id")
    @classmethod
    def check_user_id(cls, v: str) -> str:
        if not ObjectId.is_valid(v):
            raise ValueError("user_id must be a valid 24-character hex string")
        return v

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )

