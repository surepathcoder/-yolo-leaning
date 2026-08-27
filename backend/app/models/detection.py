"""Raw detection and tracking observations.

These two tables are the bottom of the evidence chain: everything the event
engine infers can be traced back to rows here.  They are also the highest-volume
tables in the system, so persistence is sampled rather than exhaustive -- see
``DetectionEvent`` below.
"""

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
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import GUID, Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    pass


class DetectionEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single object detected in a single frame.

    Writing every detection of every frame would mean roughly 10 rows/second
    per camera -- about 26 million rows per camera per month.  The pipeline
    therefore persists a detection only when it is *interesting*: the first
    sighting of a track, a detection referenced by an event, or a periodic
    sample for diagnostics.  The unsampled majority still flows through
    tracking and the rule engine in memory; it just is not stored.
    """

    __tablename__ = "detection_events"
    __table_args__ = (
        Index("ix_detections_org_detected", "organization_id", "detected_at"),
        Index("ix_detections_camera_detected", "camera_id", "detected_at"),
        Index("ix_detections_track", "track_key"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True
    )

    class_id: Mapped[int] = mapped_column(Integer, nullable=False)
    class_name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    # Bounding box, normalised 0..1 -- resolution-independent, like zones.
    bbox_x1: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y1: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_x2: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y2: Mapped[float] = mapped_column(Float, nullable=False)

    #: Null when the detection was not associated with a track.
    track_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    track_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Wall-clock time of the frame, from the pipeline (not insertion time).
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    frame_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inference_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Which model produced this. Essential when comparing model versions.
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("model_versions.id", ondelete="SET NULL"), nullable=True
    )

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.bbox_x1, self.bbox_y1, self.bbox_x2, self.bbox_y2)

    def __repr__(self) -> str:
        return f"<DetectionEvent {self.class_name} {self.confidence:.2f}>"


class TrackingObject(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The lifetime of one tracked object on one camera.

    Identity is anonymous by design: a track is "person 42 on camera 4 during
    session X", never a named individual.  See ``docs/PRIVACY.md``.

    A ``track_key`` includes a session id because trackers restart their
    numbering from 1 whenever a pipeline reconnects; without it, yesterday's
    track 42 and today's track 42 would collide.
    """

    __tablename__ = "tracking_objects"
    __table_args__ = (
        Index("ix_tracks_org_first_seen", "organization_id", "first_seen_at"),
        Index("ix_tracks_camera_active", "camera_id", "is_active"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: "<camera_id>:<session_id>:<track_id>" -- unique across restarts.
    track_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str] = mapped_column(String(40), nullable=False)

    class_name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    #: Highest confidence observed across the track's life.
    best_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Latest known position, normalised.
    last_bbox_x1: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_bbox_y1: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_bbox_x2: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_bbox_y2: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    #: Down-sampled path as ``[[t_offset_seconds, x, y], ...]`` using the
    #: reference point. Capped in length -- a full-resolution trajectory would
    #: bloat the row without adding analytical value.
    trajectory: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    @property
    def duration_seconds(self) -> float:
        return (self.last_seen_at - self.first_seen_at).total_seconds()

    def __repr__(self) -> str:
        return f"<TrackingObject {self.class_name} #{self.track_id}>"
