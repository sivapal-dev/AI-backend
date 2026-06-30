from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict


class CustomFieldType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    SELECT = "select"
    MULTI_SELECT = "multi_select"
    DATE = "date"
    CHECKBOX = "checkbox"
    URL = "url"


def _clean_options(options: List[str]) -> List[str]:
    if not options:
        return []
    cleaned = []
    for opt in options:
        opt_trimmed = opt.strip()
        if not opt_trimmed:
            continue
        if len(opt_trimmed) > 100:
            raise ValueError(f"Custom field option length exceeds limit of 100 characters: '{opt_trimmed[:20]}...'")
        if opt_trimmed not in cleaned:
            cleaned.append(opt_trimmed)
    if len(cleaned) > 100:
        raise ValueError("Custom field cannot have more than 100 options")
    return cleaned


class CustomFieldDefinition(BaseModel):
    id: str
    name: str
    field_type: CustomFieldType
    options: List[str] = []
    required: bool = False
    default_value: Optional[Any] = None
    description: Optional[str] = None

    @field_validator("options")
    @classmethod
    def validate_options(cls, v: List[str]) -> List[str]:
        return _clean_options(v)


class CustomFieldValue(BaseModel):
    field_id: str
    value: Any


class CustomFieldCreate(BaseModel):
    name: str = Field(max_length=200)
    field_type: CustomFieldType
    options: List[str] = []
    required: bool = False
    default_value: Optional[Any] = None
    description: Optional[str] = Field(default=None, max_length=5000)

    @field_validator("options")
    @classmethod
    def validate_options(cls, v: List[str]) -> List[str]:
        return _clean_options(v)

    model_config = ConfigDict(extra="forbid")


class CustomFieldUpdate(BaseModel):
    name: Optional[str] = None
    options: Optional[List[str]] = None
    required: Optional[bool] = None
    default_value: Optional[Any] = None
    description: Optional[str] = None

    @field_validator("options")
    @classmethod
    def validate_options(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        return _clean_options(v)

    model_config = ConfigDict(extra="forbid")
