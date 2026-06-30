from datetime import datetime, timezone
from typing import Optional, List, AsyncGenerator
from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict
from enum import Enum
from bson import ObjectId
import json


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConversationStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChatMessage(BaseModel):
    role: MessageRole
    content: str = Field(max_length=20000)
    project_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("project_id")
    @classmethod
    def check_project_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.strip() != "":
            if not ObjectId.is_valid(v):
                raise ValueError("project_id must be a valid 24-character hex string")
        return v

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class ChatConversation(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    user_id: str
    title: str = Field(max_length=200)
    status: ConversationStatus = ConversationStatus.ACTIVE
    project_id: Optional[str] = None
    messages: List[ChatMessage] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("user_id", "project_id")
    @classmethod
    def check_object_ids(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip() == "":
            return v
        if not ObjectId.is_valid(v):
            raise ValueError("ID must be a valid 24-character hex string")
        return v

    @model_validator(mode="after")
    def limit_messages(self) -> "ChatConversation":
        if len(self.messages) > 500:
            raise ValueError("Conversation history cannot exceed 500 messages to prevent database size limits")
        return self

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )

