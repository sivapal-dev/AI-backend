from typing import Optional, List, Literal
from datetime import datetime, timezone
from bson import ObjectId
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict
from models.user import UserRole


class CommentBase(BaseModel):
    content: str
    entity_type: Literal["task", "bug"]
    entity_id: str
    parent_id: Optional[str] = None  # For threaded replies
    mentions: List[str] = []  # List of user IDs mentioned

    @field_validator("entity_id")
    @classmethod
    def validate_entity_id(cls, v: str) -> str:
        if not ObjectId.is_valid(v):
            raise ValueError("entity_id must be a valid 24-character hex string")
        return v

    @field_validator("parent_id")
    @classmethod
    def validate_parent_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip() == "":
            return None
        if not ObjectId.is_valid(v):
            raise ValueError("parent_id must be a valid 24-character hex string")
        return v

    @field_validator("mentions")
    @classmethod
    def validate_mentions(cls, v: List[str]) -> List[str]:
        if v:
            for m in v:
                if not ObjectId.is_valid(m):
                    raise ValueError(f"Mentioned user ID '{m}' must be a valid 24-character hex string")
            return list(dict.fromkeys(v))
        return v


class CommentCreate(CommentBase):
    content: str = Field(max_length=10000)

    model_config = ConfigDict(extra="forbid")


class CommentUpdate(BaseModel):
    content: Optional[str] = Field(default=None, max_length=10000)

    model_config = ConfigDict(extra="forbid")


class CommentInDB(CommentBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    author_id: str
    author_name: str
    author_email: str
    author_role: UserRole
    edited: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def prevent_self_parent(self) -> "CommentInDB":
        if self.parent_id and self.parent_id == self.id:
            raise ValueError("A comment cannot be its own parent")
        return self

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class CommentResponse(CommentBase):
    id: str = Field(alias="_id")
    author_id: str
    author_name: str
    author_email: str
    author_role: UserRole
    edited: bool = False
    created_at: datetime
    updated_at: datetime
    replies: List[dict] = []

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )
