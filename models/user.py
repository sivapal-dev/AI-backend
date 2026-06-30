from enum import Enum
from typing import Optional, List, Dict, Annotated, Any
from datetime import datetime, timezone
from pydantic import BaseModel, EmailStr, Field, field_validator, ConfigDict, AfterValidator
from bson import ObjectId


class PyObjectId(ObjectId):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid ObjectId")
        return ObjectId(v)

    @classmethod
    def __get_pydantic_json_schema__(cls, field_schema):
        field_schema.update(type="string")


def validate_object_id_str(v: Any) -> str:
    if not v:
        return v
    v_str = str(v)
    if not ObjectId.is_valid(v_str):
        raise ValueError("Invalid ObjectId format")
    return v_str


ObjectIdStr = Annotated[str, AfterValidator(validate_object_id_str)]


class UserRole(str, Enum):
    ADMIN = "admin"
    HR = "hr"
    TEAM_LEAD = "team_lead"
    DEVELOPER = "developer"
    SENIOR_FULLSTACK_DEVELOPER = "senior_fullstack_developer"
    JUNIOR_DEVELOPER = "junior_developer"
    QA = "qa"
    DESIGNER = "designer"
    DEVOPS = "devops"
    EMPLOYEE = "employee"


class NotificationPreferences(BaseModel):
    task_assigned: bool = True
    task_updated: bool = True
    task_commented: bool = True
    bug_assigned: bool = True
    bug_updated: bool = True
    bug_reported: bool = True
    meeting_scheduled: bool = True
    meeting_reminder: bool = True
    meeting_cancelled: bool = True
    project_invite: bool = True
    project_deadline: bool = True
    ai_task_confirmation: bool = True
    ai_task_assigned: bool = True
    ai_checkin: bool = True
    ai_admin_alert: bool = True
    system: bool = True


class LeaveBalance(BaseModel):
    annual_total: int = 18
    annual_used: int = 0
    annual_pending: int = 0
    emergency_total: int = 10
    emergency_used: int = 0
    emergency_pending: int = 0


class UserSettings(BaseModel):
    email_notifications: bool = True
    weekly_digest: bool = False
    notifications: NotificationPreferences = NotificationPreferences()
    ams_enabled: bool = True


class EmailCredentials(BaseModel):
    """Stores encrypted IMAP credentials for company email inbox"""
    email_address: str
    encrypted_password: str
    imap_host: str = "imap.hostinger.com"
    imap_port: int = 993
    is_connected: bool = False


class UserBase(BaseModel):
    email: EmailStr
    name: str
    role: UserRole = UserRole.DEVELOPER
    position: Optional[str] = None  # organizational position (e.g., "team lead", "hr", etc.)
    avatar: Optional[str] = Field(default=None, max_length=2048)
    email_verified: bool = False
    settings: UserSettings = UserSettings()
    leave_balance: LeaveBalance = LeaveBalance()

    @field_validator("avatar")
    @classmethod
    def validate_avatar_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not v.startswith(("http://", "https://", "/", "data:image/")):
            raise ValueError("Avatar must be a valid URL, absolute path, or base64 data URI")
        return v


class UserCreate(UserBase):
    name: str = Field(max_length=200)
    position: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class UserInDB(UserBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    verification_token: Optional[str] = Field(default=None, max_length=512)
    verification_token_expires: Optional[datetime] = None
    welcome_token: Optional[str] = Field(default=None, max_length=512)
    welcome_token_expires: Optional[datetime] = None
    email_credentials: Optional[EmailCredentials] = None
    github_access_token: Optional[str] = Field(default=None, max_length=1024)
    github_username: Optional[str] = Field(default=None, max_length=200)
    emergency_holiday_selections: Dict[int, List[str]] = Field(default_factory=dict)  # {year: [holiday_names]}
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_login: Optional[datetime] = None
    last_seen: Optional[datetime] = None

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class UserUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[UserRole] = None
    position: Optional[str] = None
    avatar: Optional[str] = Field(default=None, max_length=2048)
    settings: Optional[UserSettings] = None
    leave_balance: Optional[LeaveBalance] = None

    @field_validator("avatar")
    @classmethod
    def validate_avatar_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not v.startswith(("http://", "https://", "/", "data:image/")):
            raise ValueError("Avatar must be a valid URL, absolute path, or base64 data URI")
        return v

    model_config = ConfigDict(extra="forbid")


class UserResponse(UserBase):
    id: str = Field(alias="_id")
    created_at: datetime
    updated_at: datetime
    last_login: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    has_email_connected: bool = False
    github_username: Optional[str] = None

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class OTPRequest(BaseModel):
    email: EmailStr
    name: Optional[str] = Field(default=None, max_length=200)  # For new users

    model_config = ConfigDict(extra="forbid")


class OTPVerify(BaseModel):
    email: EmailStr
    otp: str

    model_config = ConfigDict(extra="forbid")


class WelcomeVerify(BaseModel):
    email: EmailStr
    welcome_token: str

    model_config = ConfigDict(extra="forbid")


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenData(BaseModel):
    email: Optional[str] = None
    user_id: Optional[str] = None
