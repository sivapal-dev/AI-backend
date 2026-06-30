from enum import Enum
from typing import Optional, List, Dict, Any
import re
from pydantic import BaseModel, Field, field_validator, ConfigDict
from datetime import datetime, timezone
from bson import ObjectId


class ElementType(str, Enum):
    STICKY = "sticky"
    SHAPE = "shape"
    CONNECTOR = "connector"
    TEXT = "text"
    DRAWING = "drawing"
    IMAGE = "image"
    TASK = "task"


class ShapeType(str, Enum):
    RECT = "rect"
    ELLIPSE = "ellipse"
    DIAMOND = "diamond"
    ARROW = "arrow"


class CanvasElementBase(BaseModel):
    type: ElementType
    x: float
    y: float
    width: float = 0
    height: float = 0
    rotation: float = 0
    data: Dict[str, Any] = {}
    connected_to: List[str] = []  # element IDs for connectors
    z_index: int = 0


def _validate_color(v: str) -> str:
    if not re.match(r"^#(?:[0-9a-fA-F]{3,4}){1,2}$", v) and not v.startswith(("rgb", "hsl", "rgba", "hsla")):
        raise ValueError("Color must be a valid hex, rgb, or hsl color string")
    return v


class StickyNoteData(BaseModel):
    text: str = Field(max_length=5000)
    color: str = Field(default="#fff176", max_length=50)

    @field_validator("color")
    @classmethod
    def check_color(cls, v: str) -> str:
        return _validate_color(v)


class ShapeData(BaseModel):
    shape_type: ShapeType
    fill: str = Field(default="#ffffff", max_length=50)
    stroke: str = Field(default="#000000", max_length=50)
    stroke_width: float = Field(default=2, ge=0, le=100)

    @field_validator("fill", "stroke")
    @classmethod
    def check_color(cls, v: str) -> str:
        return _validate_color(v)


class DrawingData(BaseModel):
    paths: List[List[Dict[str, float]]]  # list of points {x, y}
    stroke: str = Field(default="#000000", max_length=50)
    stroke_width: float = Field(default=2, ge=0, le=100)
    fill: Optional[str] = Field(default=None, max_length=50)

    @field_validator("stroke", "fill")
    @classmethod
    def check_color(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_color(v)


class TextData(BaseModel):
    text: str = Field(max_length=5000)
    font_size: int = Field(default=14, ge=1, le=500)
    color: str = Field(default="#000000", max_length=50)
    font_family: str = Field(default="Inter, sans-serif", max_length=100)

    @field_validator("color")
    @classmethod
    def check_color(cls, v: str) -> str:
        return _validate_color(v)


class ImageData(BaseModel):
    url: str = Field(max_length=2048)
    width: int = Field(default=200, ge=1, le=10000)
    height: int = Field(default=150, ge=1, le=10000)


class TaskData(BaseModel):
    task_id: str = Field(max_length=50)
    title: str = Field(max_length=500)
    status: str = Field(max_length=100)
    assignee: Optional[str] = Field(default=None, max_length=200)


class CanvasElementCreate(CanvasElementBase):
    pass


class CanvasElementUpdate(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    rotation: Optional[float] = None
    data: Optional[Dict[str, Any]] = None
    connected_to: Optional[List[str]] = None
    z_index: Optional[int] = None


class CanvasElementInDB(CanvasElementBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    whiteboard_id: str


class WhiteboardBase(BaseModel):
    name: str = Field(max_length=200)
    description: Optional[str] = Field(default="", max_length=1000)
    project_id: Optional[str] = Field(default=None, max_length=50)  # null = personal whiteboard
    elements: List[CanvasElementBase] = []
    background_color: str = Field(default="#ffffff", max_length=50)
    grid_size: int = Field(default=20, ge=1, le=100)  # snap to grid
    is_public: bool = False

    @field_validator("project_id")
    @classmethod
    def check_project_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.strip() != "":
            if not ObjectId.is_valid(v):
                raise ValueError("project_id must be a valid 24-character hex string")
        return v

    @field_validator("background_color")
    @classmethod
    def check_color(cls, v: str) -> str:
        return _validate_color(v)


class WhiteboardCreate(WhiteboardBase):
    model_config = ConfigDict(extra="forbid")


class WhiteboardUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    background_color: Optional[str] = Field(default=None, max_length=50)
    grid_size: Optional[int] = Field(default=None, ge=1, le=100)
    elements: Optional[List[CanvasElementBase]] = None

    model_config = ConfigDict(extra="forbid")


class WhiteboardInDB(WhiteboardBase):
    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )


class WhiteboardResponse(WhiteboardBase):
    id: str = Field(alias="_id")
    created_by: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str}
    )

