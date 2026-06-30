from enum import Enum
from typing import Optional, Literal
from bson import ObjectId
from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict
from datetime import datetime, timezone


class LinkType(str, Enum):
    BLOCKS = "blocks"
    IS_BLOCKED_BY = "is_blocked_by"
    RELATES_TO = "relates_to"
    DUPLICATES = "duplicates"
    IS_DUPLICATED_BY = "is_duplicated_by"
    CLONES = "clones"
    IS_CLONED_BY = "is_cloned_by"


class IssueLinkBase(BaseModel):
    source_id: str
    source_type: Literal["task", "bug", "epic"] = "task"
    target_id: str
    target_type: Literal["task", "bug", "epic"] = "task"
    link_type: LinkType

    @field_validator("source_id", "target_id")
    @classmethod
    def validate_ids(cls, v: str) -> str:
        if not ObjectId.is_valid(v):
            raise ValueError("ID must be a valid 24-character hex string")
        return v


class IssueLinkCreate(IssueLinkBase):
    @model_validator(mode="after")
    def verify_no_self_link(self) -> "IssueLinkCreate":
        if self.source_id == self.target_id:
            raise ValueError("source_id and target_id cannot be the same (self-linking is not allowed)")
        return self

    model_config = ConfigDict(extra="forbid")


class IssueLinkInDB(IssueLinkBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class IssueLinkResponse(IssueLinkBase):
    id: str = Field(alias="_id")
    created_by: str
    created_at: datetime
    source_title: Optional[str] = None
    target_title: Optional[str] = None

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )

