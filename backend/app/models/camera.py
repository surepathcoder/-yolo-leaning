"""Cameras and their streams."""

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
    UniqueConstraint,
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
from app.models.enums import CameraStatus, StreamProfile, VideoSourceKind
from app.models.mixins import enum_column

if TYPE_CHECKING:
    from app.models.store import Store
    from app.models.zone import Zone


class Camera(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A camera VisionGuard ingests video from.

    Credentials are **never** stored in the connection URL.  ``host``, ``port``
    and ``path`` are stored in the clear (they are not secret and are needed
    for diagnostics), while the username and password are encrypted at rest and
    are never returned by the API.  The playable URL is assembled in memory,
    only inside the vision layer.
    """

    __tablename__ = "cameras"
    __table_args__ = (
        UniqueConstraint("store_id", "name", name="uq_camera_store_name"),
        Index("ix_cameras_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Human location inside the store, e.g. "Aisle 3 / warehouse door".
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # ------------------------------------------------------------ connection
    source_kind: Mapped[VideoSourceKind] = mapped_column(
        enum_column(VideoSourceKind), nullable=False, default=VideoSourceKind.RTSP
    )
    #: RTSP host / IP. Null for FILE, WEBCAM and SYNTHETIC sources.
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True, default=554)
    #: Stream path, e.g. "/cam/realmonitor?channel=1&subtype=1".
    stream_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: FILE: path on disk. WEBCAM: device index as a string. SYNTHETIC: scenario name.
    source_locator: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Fernet ciphertext. Decrypted only in the vision layer, never serialised.
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---------------------------------------------------------------- video
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Requested processing FPS. The pipeline drops frames to hold this rate --
    #: it does not slow the camera down.
    target_fps: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    transport: Mapped[str] = mapped_column(String(8), nullable=False, default="tcp")

    # ------------------------------------------------------------- detection
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detection_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Null means "use the global YOLO_CONFIDENCE_THRESHOLD".
    confidence_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Null means the default model. Set to run a customer-specific model.
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("model_versions.id", ondelete="SET NULL"), nullable=True
    )
    #: COCO/custom class names this camera should report. Empty = all classes.
    detect_classes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: Per-camera rule overrides, e.g. ``{"loitering_threshold_seconds": 30}``.
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # ----------------------------------------------------------------- state
    status: Mapped[CameraStatus] = mapped_column(
        enum_column(CameraStatus), nullable=False, default=CameraStatus.OFFLINE, index=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: User-safe summary of the last failure. Never contains credentials.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Measured processing rate, updated by the pipeline heartbeat.
    measured_fps: Mapped[float | None] = mapped_column(Float, nullable=True)

    store: Mapped[Store] = relationship(back_populates="cameras")
    streams: Mapped[list[CameraStream]] = relationship(
        back_populates="camera", cascade="all, delete-orphan", lazy="selectin"
    )
    zones: Mapped[list[Zone]] = relationship(back_populates="camera", cascade="all, delete-orphan")

    @property
    def is_operational(self) -> bool:
        return self.is_enabled and self.status == CameraStatus.ONLINE

    def __repr__(self) -> str:
        return f"<Camera {self.name} {self.status}>"


class CameraStream(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One of a camera's stream profiles.

    Most NVRs expose a high-resolution main stream for recording and a low-cost
    sub stream.  VisionGuard prefers the sub stream for inference: at the 640px
    model input size the extra pixels are discarded anyway, and decoding costs
    several times less CPU.
    """

    __tablename__ = "camera_streams"
    __table_args__ = (UniqueConstraint("camera_id", "profile", name="uq_stream_camera_profile"),)

    camera_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile: Mapped[StreamProfile] = mapped_column(
        enum_column(StreamProfile), nullable=False, default=StreamProfile.SUB
    )
    stream_path: Mapped[str] = mapped_column(String(500), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    codec: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: The stream the pipeline actually reads.
    is_inference_source: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    camera: Mapped[Camera] = relationship(back_populates="streams")

    def __repr__(self) -> str:
        return f"<CameraStream {self.profile} {self.width}x{self.height}>"
