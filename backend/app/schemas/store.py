"""Store request and response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime, time

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import APISchema


class OperatingHoursEntry(BaseModel):
    """Opening hours for one weekday, in the store's local time."""

    model_config = ConfigDict(extra="forbid")

    day_of_week: int = Field(ge=0, le=6, description="0 = Monday .. 6 = Sunday")
    opens_at: time | None = None
    closes_at: time | None = None
    is_closed: bool = False

    @model_validator(mode="after")
    def _check_times(self) -> OperatingHoursEntry:
        if not self.is_closed and (self.opens_at is None or self.closes_at is None):
            raise ValueError("an open day needs both opens_at and closes_at")
        return self

    @property
    def is_overnight(self) -> bool:
        if self.is_closed or self.opens_at is None or self.closes_at is None:
            return False
        return self.closes_at < self.opens_at


class OperatingHoursRead(APISchema):
    day_of_week: int
    opens_at: time | None
    closes_at: time | None
    is_closed: bool


class StoreBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=2000)
    address: str | None = Field(default=None, max_length=400)
    city: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    #: IANA name. Every operating-hours comparison and report bucket uses this,
    #: never the server's timezone.
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("code")
    @classmethod
    def _normalise_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(f"'{value}' is not a known IANA timezone") from exc
        return value


class StoreCreate(StoreBase):
    model_config = ConfigDict(extra="forbid")

    #: Seed a standard week. Without hours, after-hours detection treats every
    #: day as closed, which is safe but noisy.
    use_default_hours: bool = True


class StoreUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    address: str | None = Field(default=None, max_length=400)
    city: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    settings: dict | None = None


class StoreRead(APISchema):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    code: str
    description: str | None
    address: str | None
    city: str | None
    region: str | None
    country: str | None
    latitude: float | None
    longitude: float | None
    timezone: str | None
    is_active: bool
    settings: dict
    created_at: datetime
    updated_at: datetime
    operating_hours: list[OperatingHoursRead] = Field(default_factory=list)


class StoreSummary(APISchema):
    """A store as it appears in a list or picker."""

    id: uuid.UUID
    name: str
    code: str
    city: str | None
    is_active: bool
    timezone: str | None


class SetOperatingHoursRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The whole week. Days omitted are treated as closed -- partial updates
    #: are how a store ends up with a Tuesday nobody remembers configuring.
    days: list[OperatingHoursEntry] = Field(min_length=0, max_length=7)

    @field_validator("days")
    @classmethod
    def _unique_days(cls, value: list[OperatingHoursEntry]) -> list[OperatingHoursEntry]:
        seen = {entry.day_of_week for entry in value}
        if len(seen) != len(value):
            raise ValueError("each day_of_week may appear at most once")
        return value
