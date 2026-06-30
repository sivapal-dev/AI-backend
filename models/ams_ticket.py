from enum import Enum
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from bson import ObjectId
from pydantic import BaseModel, EmailStr, Field, field_validator, ConfigDict
from config import get_settings


class AmsTicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AmsTicketStatus(str, Enum):
    OPEN = "open"
    ANALYZING = "analyzing"
    FIXING = "fixing"
    REVIEW_READY = "review_ready"
    COMPLETED = "completed"
    FAILED = "failed"


class AmsAutomationStatus(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AmsApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AmsTimelineEvent(BaseModel):
    stage: str
    status: str
    message: str
    duration_ms: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AmsAnalysisArtifact(BaseModel):
    root_cause: Optional[str] = None
    affected_module: Optional[str] = None
    candidate_files: List[str] = Field(default_factory=list)
    fix_strategy: Optional[str] = None
    confidence: Optional[float] = None


class AmsPatchArtifact(BaseModel):
    file_path: Optional[str] = None
    old_code: Optional[str] = None
    new_code: Optional[str] = None
    applied: bool = False
    changed_files: List[str] = Field(default_factory=list)
    summary: Optional[str] = None


class AmsDocumentationArtifact(BaseModel):
    fix_summary: Optional[str] = None
    changelog_entry: Optional[str] = None
    internal_ticket_comment: Optional[str] = None


class AmsPullRequestArtifact(BaseModel):
    provider: Optional[str] = None
    status: Optional[str] = None
    branch_name: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    commit_sha: Optional[str] = None
    commit_message: Optional[str] = None
    base_branch: Optional[str] = None
    url: Optional[str] = None


class AmsNotificationArtifact(BaseModel):
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    recipients: List[EmailStr] = Field(default_factory=list)
    sent_at: Optional[datetime] = None


class AmsTicketBase(BaseModel):
    project_id: str
    title: str = Field(max_length=500)
    description: str = Field(max_length=10000)
    priority: AmsTicketPriority = AmsTicketPriority.MEDIUM
    steps_to_reproduce: str = Field(default="", max_length=5000)
    expected_result: str = Field(default="", max_length=5000)
    actual_result: str = Field(default="", max_length=5000)
    module_hint: str = Field(default="", max_length=200)
    affected_platform: str = Field(default="", max_length=200)
    stakeholder_emails: List[EmailStr] = Field(default_factory=list)
    automation_enabled: Optional[bool] = None

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, v: str) -> str:
        if not ObjectId.is_valid(v):
            raise ValueError("project_id must be a valid 24-character hex string")
        return v

    @field_validator("stakeholder_emails")
    @classmethod
    def validate_stakeholder_emails(cls, v: List[EmailStr]) -> List[EmailStr]:
        if len(v) > 50:
            raise ValueError("A ticket cannot have more than 50 stakeholder emails")
        return v


class AmsTicketCreate(AmsTicketBase):
    pass


class AmsTicketInDB(AmsTicketBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    ticket_key: str
    reporter_id: str
    reporter_name: str
    linked_bug_id: Optional[str] = None
    status: AmsTicketStatus = AmsTicketStatus.OPEN
    automation_status: AmsAutomationStatus = AmsAutomationStatus.IDLE
    approval_status: AmsApprovalStatus = AmsApprovalStatus.PENDING
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    approval_notes: Optional[str] = None
    ai_provider: str = Field(default_factory=lambda: getattr(get_settings(), "openrouter_default_provider", "openrouter"))
    ai_model: str = Field(default_factory=lambda: getattr(get_settings(), "openrouter_default_model", "poolside/laguna-m.1:free"))
    changelog_path: Optional[str] = None
    analysis: Optional[AmsAnalysisArtifact] = None
    patch: Optional[AmsPatchArtifact] = None
    documentation: Optional[AmsDocumentationArtifact] = None
    notification: Optional[AmsNotificationArtifact] = None
    pull_request: Optional[AmsPullRequestArtifact] = None
    timeline: List[AmsTimelineEvent] = Field(default_factory=list)
    last_run_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    failed_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class AmsTicketResponse(AmsTicketInDB):
    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )

