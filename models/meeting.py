from enum import Enum
from typing import Optional, List
from datetime import datetime, timezone, date, time
from bson import ObjectId
from pydantic import BaseModel, Field, model_validator, ConfigDict


class MeetingType(str, Enum):
    STANDUP = "standup"
    SPRINT = "sprint"
    REVIEW = "review"
    RETROSPECTIVE = "retrospective"
    PLANNING = "planning"
    OTHER = "other"


class MeetingStatus(str, Enum):
    SCHEDULED = "scheduled"
    ONGOING = "ongoing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MeetingBase(BaseModel):
    title: str
    description: Optional[str] = None
    meeting_type: MeetingType = MeetingType.OTHER
    date: date
    time: time
    duration: int = Field(gt=0, le=1440)
    location: Optional[str] = None
    meet_link: Optional[str] = None
    project_id: Optional[str] = None
    attendees: List[str] = []
    agenda: Optional[str] = None
    notes: Optional[str] = None
    status: MeetingStatus = MeetingStatus.SCHEDULED
    reminder_sent: bool = False

    @model_validator(mode="after")
    def validate_meeting_fields(self) -> "MeetingBase":
        # Deduplicate and validate attendees
        if self.attendees:
            deduped = list(dict.fromkeys(self.attendees))
            if len(deduped) > 100:
                raise ValueError("A meeting cannot have more than 100 attendees")
            # Validate each is a valid ObjectId
            for attendee in deduped:
                if not ObjectId.is_valid(attendee):
                    raise ValueError(f"Invalid attendee ID: {attendee} (must be a valid 24-character hex string)")
            self.attendees = deduped
        return self


class MeetingCreate(MeetingBase):
    title: str = Field(max_length=500)
    description: Optional[str] = Field(default=None, max_length=5000)
    generate_meet: Optional[bool] = None  # Internal flag — not stored in DB

    model_config = ConfigDict(extra="forbid")


class MeetingUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    meeting_type: Optional[MeetingType] = None
    date: Optional[date] = None
    time: Optional[time] = None
    duration: Optional[int] = Field(default=None, gt=0, le=1440)
    location: Optional[str] = None
    meet_link: Optional[str] = None
    project_id: Optional[str] = None
    attendees: Optional[List[str]] = None
    agenda: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[MeetingStatus] = None
    reminder_sent: Optional[bool] = None

    @model_validator(mode="after")
    def validate_meeting_update_fields(self) -> "MeetingUpdate":
        if self.attendees:
            deduped = list(dict.fromkeys(self.attendees))
            if len(deduped) > 100:
                raise ValueError("A meeting cannot have more than 100 attendees")
            for attendee in deduped:
                if not ObjectId.is_valid(attendee):
                    raise ValueError(f"Invalid attendee ID: {attendee} (must be a valid 24-character hex string)")
            self.attendees = deduped
        return self

    model_config = ConfigDict(extra="forbid")


class MeetingInDB(MeetingBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class MeetingResponse(MeetingBase):
    id: str = Field(alias="_id")
    created_by: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )
