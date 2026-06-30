from datetime import datetime, timezone
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict
from bson import ObjectId
import re
from models.user import UserRole


class WorkflowTransition(BaseModel):
    from_status: str
    to_status: str
    require_role: Optional[UserRole] = None  # e.g. "admin", "team_lead"


class WorkflowState(BaseModel):
    id: str
    label: str
    color: str = "bg-zinc-100"
    order: int = 0

    @field_validator("id")
    @classmethod
    def validate_state_id(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError("State ID must be alphanumeric and can only contain underscores or dashes")
        return v


class WorkflowBase(BaseModel):
    name: str
    states: List[WorkflowState]
    transitions: List[WorkflowTransition]
    default_state: str

    @model_validator(mode="after")
    def validate_workflow(self) -> "WorkflowBase":
        # Check uniqueness of state IDs
        state_ids = [s.id for s in self.states]
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("All state IDs must be unique within the workflow")
            
        # Check that default_state is a valid state
        if self.default_state not in state_ids:
            raise ValueError(f"default_state '{self.default_state}' must match one of the defined state IDs")
            
        # Check that all transitions reference valid states
        for t in self.transitions:
            if t.from_status not in state_ids:
                raise ValueError(f"Transition from_status '{t.from_status}' is not a defined state ID")
            if t.to_status not in state_ids:
                raise ValueError(f"Transition to_status '{t.to_status}' is not a defined state ID")
        return self


class WorkflowCreate(WorkflowBase):
    name: str = Field(max_length=200)

    model_config = ConfigDict(extra="forbid")


class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    states: Optional[List[WorkflowState]] = None
    transitions: Optional[List[WorkflowTransition]] = None
    default_state: Optional[str] = None

    @model_validator(mode="after")
    def validate_workflow_update(self) -> "WorkflowUpdate":
        if self.states:
            state_ids = [s.id for s in self.states]
            if len(state_ids) != len(set(state_ids)):
                raise ValueError("All state IDs must be unique within the workflow")
            if self.default_state and self.default_state not in state_ids:
                raise ValueError(f"default_state '{self.default_state}' must match one of the defined state IDs")
            if self.transitions:
                for t in self.transitions:
                    if t.from_status not in state_ids:
                        raise ValueError(f"Transition from_status '{t.from_status}' is not a defined state ID")
                    if t.to_status not in state_ids:
                        raise ValueError(f"Transition to_status '{t.to_status}' is not a defined state ID")
        return self

    model_config = ConfigDict(extra="forbid")


class WorkflowInDB(WorkflowBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    project_id: str
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class WorkflowResponse(WorkflowBase):
    id: str = Field(alias="_id")
    name: str
    states: List[WorkflowState]
    transitions: List[WorkflowTransition]
    default_state: str
    project_id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )
