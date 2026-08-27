"""Zone and counting-line schemas.

Coordinates are **normalised 0..1**, never pixels. A zone drawn on a 1920x1080
preview must stay correct when the pipeline switches to a 640x360 sub stream
or when an NVR is reconfigured to a different resolution -- both routine.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import ZoneType
from app.schemas.common import APISchema

#: A normalised point. The bounds are enforced here so a pixel coordinate
#: pasted by mistake fails immediately with a clear message rather than
#: producing a zone that quietly matches nothing.
NormalisedCoordinate = Annotated[float, Field(ge=0.0, le=1.0)]
Point = Annotated[list[NormalisedCoordinate], Field(min_length=2, max_length=2)]

REFERENCE_POINTS = ("center", "bottom_center", "top_center", "centroid")


class ZoneBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    zone_type: ZoneType = ZoneType.CUSTOM

    #: At least three points, normalised 0..1.
    polygon: list[Point] = Field(min_length=3, max_length=64)

    #: Which point of a bounding box is tested. "bottom_center" -- the feet --
    #: is the default for people, because it is where they actually stand.
    reference_point: str = Field(default="bottom_center")
    trigger_classes: list[str] = Field(default_factory=list, max_length=40)

    loitering_threshold_seconds: int | None = Field(default=None, ge=1, le=86400)
    dwell_threshold_seconds: int | None = Field(default=None, ge=1, le=86400)
    after_hours_monitoring: bool = False
    max_occupancy: int | None = Field(default=None, ge=1, le=1000)
    color: str = Field(default="#38bdf8", pattern="^#[0-9a-fA-F]{6}$")

    @field_validator("reference_point")
    @classmethod
    def _known_reference(cls, value: str) -> str:
        if value not in REFERENCE_POINTS:
            raise ValueError(f"reference_point must be one of {REFERENCE_POINTS}")
        return value


class ZoneCreate(ZoneBase):
    model_config = ConfigDict(extra="forbid")


class ZoneUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    zone_type: ZoneType | None = None
    polygon: list[Point] | None = Field(default=None, min_length=3, max_length=64)
    reference_point: str | None = None
    trigger_classes: list[str] | None = Field(default=None, max_length=40)
    loitering_threshold_seconds: int | None = Field(default=None, ge=1, le=86400)
    dwell_threshold_seconds: int | None = Field(default=None, ge=1, le=86400)
    after_hours_monitoring: bool | None = None
    max_occupancy: int | None = Field(default=None, ge=1, le=1000)
    is_active: bool | None = None
    color: str | None = Field(default=None, pattern="^#[0-9a-fA-F]{6}$")

    @field_validator("reference_point")
    @classmethod
    def _known_reference(cls, value: str | None) -> str | None:
        if value is not None and value not in REFERENCE_POINTS:
            raise ValueError(f"reference_point must be one of {REFERENCE_POINTS}")
        return value


class ZoneRead(APISchema):
    id: uuid.UUID
    organization_id: uuid.UUID
    store_id: uuid.UUID
    camera_id: uuid.UUID
    name: str
    description: str | None
    zone_type: ZoneType
    polygon: list
    reference_point: str
    trigger_classes: list
    loitering_threshold_seconds: int | None
    dwell_threshold_seconds: int | None
    after_hours_monitoring: bool
    max_occupancy: int | None
    is_active: bool
    color: str
    created_at: datetime
    updated_at: datetime


class LineBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    point_a: Point
    point_b: Point
    #: Swap which side counts as IN without redrawing. The UI shows an arrow
    #: so the installer can see which direction is which.
    invert_direction: bool = False
    reference_point: str = Field(default="bottom_center")
    trigger_classes: list[str] = Field(default_factory=list, max_length=40)
    #: A track cannot be counted twice within this window, so someone
    #: hesitating in a doorway is one visitor rather than three.
    cooldown_seconds: float = Field(default=3.0, ge=0, le=60)
    color: str = Field(default="#a78bfa", pattern="^#[0-9a-fA-F]{6}$")

    @field_validator("reference_point")
    @classmethod
    def _known_reference(cls, value: str) -> str:
        if value not in REFERENCE_POINTS:
            raise ValueError(f"reference_point must be one of {REFERENCE_POINTS}")
        return value


class LineCreate(LineBase):
    model_config = ConfigDict(extra="forbid")


class LineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    point_a: Point | None = None
    point_b: Point | None = None
    invert_direction: bool | None = None
    reference_point: str | None = None
    trigger_classes: list[str] | None = Field(default=None, max_length=40)
    cooldown_seconds: float | None = Field(default=None, ge=0, le=60)
    is_active: bool | None = None
    color: str | None = Field(default=None, pattern="^#[0-9a-fA-F]{6}$")


class LineRead(APISchema):
    id: uuid.UUID
    organization_id: uuid.UUID
    store_id: uuid.UUID
    camera_id: uuid.UUID
    name: str
    point_a: list
    point_b: list
    invert_direction: bool
    reference_point: str
    trigger_classes: list
    cooldown_seconds: float
    is_active: bool
    color: str
    created_at: datetime
    updated_at: datetime
