from enum import Enum
from typing import Optional
from bson import ObjectId
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime, timezone


class EpicStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class EpicColor(str, Enum):
    BLUE = "blue"
    GREEN = "green"
    ORANGE = "orange"
    RED = "red"
    PURPLE = "purple"
    TEAL = "teal"
    YELLOW = "yellow"
    PINK = "pink"


class EpicCreate(BaseModel):
    name: str = Field(max_length=500)
    description: str = Field(default="", max_length=5000)
    project_id: str
    status: EpicStatus = EpicStatus.ACTIVE
    color: EpicColor = EpicColor.BLUE

    model_config = ConfigDict(extra="forbid")


class EpicUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[EpicStatus] = None
    color: Optional[EpicColor] = None

    model_config = ConfigDict(extra="forbid")


class EpicInDB(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    name: str
    description: str = ""
    project_id: str
    status: EpicStatus = EpicStatus.ACTIVE
    color: EpicColor = EpicColor.BLUE
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, datetime: lambda v: v.isoformat()}
    )


class EpicResponse(BaseModel):
    id: str = Field(alias="_id")
    name: str
    description: str = ""
    project_id: str
    status: EpicStatus
    color: EpicColor
    created_by: str
    created_at: datetime
    updated_at: datetime
    task_count: int = 0
    completed_task_count: int = 0

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, datetime: lambda v: v.isoformat()}
    )

