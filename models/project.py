from enum import Enum
from typing import Optional, List, Dict
from datetime import datetime, timezone
from pydantic import BaseModel, Field, ConfigDict
from bson import ObjectId


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    COMPLETED = "completed"


class ProjectAttachment(BaseModel):
    url: str
    view_url: str = ""
    direct_url: str = ""
    filename: str
    uploaded_at: str
    project_id: str = ""
    uploaded_by: str = ""
    uploaded_by_name: str = ""
    mime_type: str = ""
    file_id: str = ""
    size: int = 0
    created_at: str = ""
    source_type: str = "project"
    source_task_id: str = ""
    source_task_title: str = ""
    source_attachment_id: str = ""


class ProjectBase(BaseModel):
    name: str = Field(max_length=500)
    description: str = Field(default="", max_length=5000)
    markdown_content: Optional[str] = Field(default=None, max_length=50000)
    status: ProjectStatus = ProjectStatus.ACTIVE
    creation_source: str = "manual"
    tags: List[str] = []
    start_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    github_repo: Optional[str] = Field(default=None, max_length=2048)
    attachments: Optional[List[ProjectAttachment]] = None
    team_roles: Dict[str, str] = Field(default_factory=dict)  # {user_id: role_name} for rich permissions (W153)


class ProjectCreate(ProjectBase):
    name: str = Field(max_length=500)
    description: str = Field(default="", max_length=5000)
    team: List[str] = []

    model_config = ConfigDict(extra="forbid")


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=500)
    description: Optional[str] = Field(default=None, max_length=5000)
    markdown_content: Optional[str] = Field(default=None, max_length=50000)
    status: Optional[ProjectStatus] = None
    tags: Optional[List[str]] = None
    start_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    team: Optional[List[str]] = None
    github_repo: Optional[str] = Field(default=None, max_length=2048)
    attachments: Optional[List[ProjectAttachment]] = None
    team_roles: Optional[Dict[str, str]] = None

    model_config = ConfigDict(extra="forbid")


class ProjectInDB(ProjectBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    created_by: str
    created_by_name: str
    team: List[str] = []
    is_favorite: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class ProjectResponse(ProjectBase):
    id: str = Field(alias="_id")
    created_by: str
    created_by_name: str
    team: List[str] = []
    is_favorite: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )

