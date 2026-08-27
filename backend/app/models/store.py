"""Stores and their operating hours."""

from __future__ import annotations

import uuid
from datetime import time
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import GUID, Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.camera import Camera
    from app.models.organization import Organization


class Store(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A physical location belonging to an organization."""

    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("organization_id", "code", name="uq_store_org_code"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Short human reference used in alerts and reports, e.g. "DSM-01".
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    address: Mapped[str | None] = mapped_column(String(400), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Overrides the organization default. All operating-hours comparisons and
    #: report bucketing use this zone, not the server's.
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Store-level overrides of event rules, e.g.
    #: ``{"loitering_threshold_seconds": 120}``.
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    organization: Mapped[Organization] = relationship(back_populates="stores")
    cameras: Mapped[list[Camera]] = relationship(
        back_populates="store", cascade="all, delete-orphan"
    )
    operating_hours: Mapped[list[StoreOperatingHours]] = relationship(
        back_populates="store", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Store {self.code} {self.name}>"


class StoreOperatingHours(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Opening hours for one weekday.

    ``day_of_week`` follows Python's ``date.weekday()``: Monday = 0 .. Sunday = 6.

    An overnight shift is expressed by ``closes_at < opens_at`` (e.g. 20:00 to
    04:00); :mod:`app.services.operating_hours` handles the wrap.  A day with
    no row at all is treated as *closed*, which makes after-hours detection
    fail safe: an unconfigured store never silently suppresses alerts.
    """

    __tablename__ = "store_operating_hours"
    __table_args__ = (UniqueConstraint("store_id", "day_of_week", name="uq_hours_store_day"),)

    store_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opens_at: Mapped[time | None] = mapped_column(Time, nullable=True)
    closes_at: Mapped[time | None] = mapped_column(Time, nullable=True)

    store: Mapped[Store] = relationship(back_populates="operating_hours")

    def __repr__(self) -> str:
        if self.is_closed:
            return f"<Hours day={self.day_of_week} closed>"
        return f"<Hours day={self.day_of_week} {self.opens_at}-{self.closes_at}>"
