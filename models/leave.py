from enum import Enum
from typing import Optional, List
from datetime import datetime, timezone, date, date as dt_date
from pydantic import BaseModel, Field, model_validator, ConfigDict
from bson import ObjectId
from models.user import ObjectIdStr


class LeaveType(str, Enum):
    ANNUAL = "annual"
    EMERGENCY = "emergency"


class LeaveStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class LeaveBase(BaseModel):
    leave_type: LeaveType
    start_date: date
    end_date: date
    reason: str

    @model_validator(mode="after")
    def validate_dates(self) -> 'LeaveBase':
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class LeaveCreate(LeaveBase):
    reason: str = Field(max_length=1000)

    model_config = ConfigDict(extra="forbid")


class LeaveUpdate(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    reason: Optional[str] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_dates(self) -> 'LeaveUpdate':
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self

    model_config = ConfigDict(extra="forbid")


class LeaveInDB(LeaveBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    user_id: ObjectIdStr
    user_name: str
    days: int
    status: LeaveStatus = LeaveStatus.PENDING
    approved_by: Optional[ObjectIdStr] = None
    approved_at: Optional[datetime] = None
    rejected_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    emergency_dates: Optional[List[date]] = None  # Backward compatibility

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, date: lambda v: v.isoformat()}
    )


class LeaveResponse(LeaveBase):
    id: str = Field(alias="_id")
    user_id: ObjectIdStr
    user_name: str
    days: int
    status: LeaveStatus
    approved_by: Optional[ObjectIdStr] = None
    approved_at: Optional[datetime] = None
    rejected_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    emergency_dates: Optional[List[date]] = None

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, date: lambda v: v.isoformat()}
    )


class LeaveApprove(BaseModel):
    approved: bool

    model_config = ConfigDict(extra="forbid")


class LeaveReject(BaseModel):
    reason: str = Field(max_length=1000)

    model_config = ConfigDict(extra="forbid")


# Holiday models
class HolidayBase(BaseModel):
    name: str
    date: date
    day: str
    year: int


class HolidayCreate(HolidayBase):
    pass


class HolidayUpdate(BaseModel):
    name: Optional[str] = None
    date: Optional[dt_date] = None
    day: Optional[str] = None
    year: Optional[int] = None


class HolidayInDB(HolidayBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, date: lambda v: v.isoformat()}
    )


class HolidayResponse(HolidayBase):
    id: str = Field(alias="_id")
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, date: lambda v: v.isoformat()}
    )


# Leave Settings
class LeaveSettingsBase(BaseModel):
    year: int
    annual_leave_days: int = 18
    carry_forward_days: int = 0
    saturday_working: bool = True


class LeaveSettingsUpdate(BaseModel):
    annual_leave_days: Optional[int] = None
    carry_forward_days: Optional[int] = None
    saturday_working: Optional[bool] = None


class LeaveSettingsResponse(LeaveSettingsBase):
    id: str = Field(alias="_id")
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )
