from enum import Enum
from typing import Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator, ConfigDict
from bson import ObjectId


class DocumentFormat(str, Enum):
    DOC = "doc"
    PPT = "ppt"


class DocumentStatus(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


def _validate_safe_path(path: Optional[str]) -> Optional[str]:
    if path is None:
        return None
    # Block directory traversal sequences and absolute paths
    if ".." in path or path.startswith("/") or path.startswith("\\") or ":" in path:
        raise ValueError("Directory traversal characters, absolute paths, or drive letters are not allowed")
    return path


class DocumentBase(BaseModel):
    format: DocumentFormat = DocumentFormat.DOC
    prompt: str
    project_id: Optional[str] = None


class DocumentCreate(DocumentBase):
    prompt: str = Field(max_length=10000)

    model_config = ConfigDict(extra="forbid")


class DocumentInDB(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    user_id: str
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    format: DocumentFormat
    prompt: str
    file_name: str
    file_path: str
    pdf_path: Optional[str] = None
    size_bytes: int = Field(default=0, ge=0, le=104857600)  # Max 100 MB
    status: DocumentStatus = DocumentStatus.PENDING
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("file_path", "pdf_path")
    @classmethod
    def check_safe_paths(cls, v: Optional[str]) -> Optional[str]:
        return _validate_safe_path(v)

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class DocumentResponse(BaseModel):
    id: str = Field(alias="_id")
    user_id: str
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    format: DocumentFormat
    prompt: str
    file_name: str
    file_path: str
    pdf_path: Optional[str] = None
    size_bytes: int = Field(default=0, ge=0, le=104857600)
    status: DocumentStatus
    error_message: Optional[str] = None
    created_at: datetime

    @field_validator("file_path", "pdf_path")
    @classmethod
    def check_safe_paths(cls, v: Optional[str]) -> Optional[str]:
        return _validate_safe_path(v)

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )
