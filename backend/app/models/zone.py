"""Zones, counting lines and the raw geometric events they produce."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import (
    GUID,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
)
from app.models.enums import CrossingDirection, ZoneEventType, ZoneType
from app.models.mixins import enum_column

if TYPE_CHECKING:
    from app.models.camera import Camera


class Zone(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A polygon drawn on a camera view.

    Coordinates are stored **normalised to 0..1** relative to the frame, not in
    pixels.  A zone drawn on a 1920x1080 preview therefore stays correct when
    the pipeline switches to a 640x360 sub stream, or when the camera's
    resolution changes -- a real occurrence when an NVR is reconfigured.

    Format: ``[[x1, y1], [x2, y2], ...]`` with at least 3 points.
    """

    __tablename__ = "zones"
    __table_args__ = (Index("ix_zones_camera_active", "camera_id", "is_active"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    zone_type: Mapped[ZoneType] = mapped_column(
        enum_column(ZoneType), nullable=False, default=ZoneType.CUSTOM, index=True
    )

    #: Normalised polygon vertices, ``[[x, y], ...]``.
    polygon: Mapped[list] = mapped_column(JSON, nullable=False)

    #: Which point of a bounding box is tested for containment.
    #: "bottom_center" (the feet) is the default because it approximates where
    #: a person actually stands on the floor; "center" suits objects on shelves.
    reference_point: Mapped[str] = mapped_column(
        String(20), nullable=False, default="bottom_center"
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Object classes this zone reacts to. Empty = person only.
    trigger_classes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    #: Seconds inside the zone before LOITERING fires. Null = store/global default.
    loitering_threshold_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Seconds of continuous presence before DWELL_THRESHOLD_EXCEEDED fires.
    dwell_threshold_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Fire AFTER_HOURS_ACTIVITY when presence is detected outside store hours.
    after_hours_monitoring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Maximum simultaneous tracks before OCCUPANCY_THRESHOLD_EXCEEDED.
    max_occupancy: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: UI colour, "#RRGGBB".
    color: Mapped[str] = mapped_column(String(9), nullable=False, default="#38bdf8")

    camera: Mapped[Camera] = relationship(back_populates="zones")

    def __repr__(self) -> str:
        return f"<Zone {self.name} {self.zone_type}>"


class CountingLine(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A virtual line whose crossings are counted directionally.

    Stored as two normalised points.  "IN" is defined as crossing from the
    negative to the positive side of the line's normal, which lets the UI show
    an arrow so the installer can see which way counts as entering.
    """

    __tablename__ = "counting_lines"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Normalised start point [x, y].
    point_a: Mapped[list] = mapped_column(JSON, nullable=False)
    #: Normalised end point [x, y].
    point_b: Mapped[list] = mapped_column(JSON, nullable=False)
    #: Swap which side counts as IN without redrawing the line.
    invert_direction: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    trigger_classes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reference_point: Mapped[str] = mapped_column(
        String(20), nullable=False, default="bottom_center"
    )
    #: A track cannot be counted twice within this window -- absorbs the
    #: jitter that makes a stationary person appear to cross repeatedly.
    cooldown_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    color: Mapped[str] = mapped_column(String(9), nullable=False, default="#a78bfa")

    def __repr__(self) -> str:
        return f"<CountingLine {self.name}>"


class ZoneEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A raw entry/exit observation for one track and one zone.

    This is an **observation**, not an interpretation.  It records that a track
    crossed a zone boundary and, once it leaves, how long it stayed.  Rules
    such as loitering read these rows; they do not replace them.
    """

    __tablename__ = "zone_events"
    __table_args__ = (
        Index("ix_zone_events_org_occurred", "organization_id", "occurred_at"),
        Index("ix_zone_events_zone_track", "zone_id", "track_key"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True
    )
    zone_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("zones.id", ondelete="CASCADE"), nullable=False, index=True
    )

    event_type: Mapped[ZoneEventType] = mapped_column(enum_column(ZoneEventType), nullable=False)
    #: Globally unique track reference: "<camera_id>:<session>:<track_id>".
    track_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    class_name: Mapped[str] = mapped_column(String(60), nullable=False, default="person")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    #: Set on the EXITED row: seconds between entering and leaving.
    dwell_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    def __repr__(self) -> str:
        return f"<ZoneEvent {self.event_type} track={self.track_id}>"


class LineCrossingEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A directional line crossing by one track."""

    __tablename__ = "line_crossing_events"
    __table_args__ = (
        Index("ix_line_crossings_org_occurred", "organization_id", "occurred_at"),
        Index("ix_line_crossings_line_track", "line_id", "track_key"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("counting_lines.id", ondelete="CASCADE"), nullable=False, index=True
    )

    direction: Mapped[CrossingDirection] = mapped_column(
        enum_column(CrossingDirection), nullable=False
    )
    track_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    class_name: Mapped[str] = mapped_column(String(60), nullable=False, default="person")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    def __repr__(self) -> str:
        return f"<LineCrossingEvent {self.direction} track={self.track_id}>"
