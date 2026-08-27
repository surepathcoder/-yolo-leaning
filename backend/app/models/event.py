"""The central event table, its evidence, and alerts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
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

from app.db.base import GUID, Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from app.models.enums import (
    AlertStatus,
    ConfidenceLevel,
    EventSeverity,
    EventType,
    EvidenceKind,
)
from app.models.mixins import enum_column

if TYPE_CHECKING:
    from app.models.user import User


class Event(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Everything the event engine emits -- observations and inferences alike.

    The ``is_inference`` flag is the most important column in this table.

    * ``is_inference = False`` -- the system measured this.  "A person crossed
      into zone 3 at 14:42:10."  It is a fact about pixels.
    * ``is_inference = True`` -- the system *concluded* this from other rows.
      "A possible inventory discrepancy."  ``source_event_ids`` names the
      observations it rests on and ``rule_name`` names the rule that fired, so
      a human can audit the reasoning and disagree with it.

    The API and dashboard must render the two differently.  Collapsing them
    would let a guess be read as evidence, which is exactly the failure mode
    this product is designed to avoid.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_org_occurred", "organization_id", "occurred_at"),
        Index("ix_events_org_type_occurred", "organization_id", "event_type", "occurred_at"),
        Index("ix_events_org_severity_occurred", "organization_id", "severity", "occurred_at"),
        Index("ix_events_store_occurred", "store_id", "occurred_at"),
        Index("ix_events_camera_occurred", "camera_id", "occurred_at"),
        Index("ix_events_track", "track_key"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True
    )
    camera_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True
    )
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("zones.id", ondelete="SET NULL"), nullable=True, index=True
    )
    shelf_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("shelves.id", ondelete="SET NULL"), nullable=True, index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )

    event_type: Mapped[EventType] = mapped_column(
        enum_column(EventType), nullable=False, index=True
    )
    severity: Mapped[EventSeverity] = mapped_column(
        enum_column(EventSeverity), nullable=False, default=EventSeverity.INFO, index=True
    )

    # ------------------------------------------------- observation vs inference
    is_inference: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    #: Model/geometry confidence for observations (0..1). Null for inferences,
    #: which use the coarser ``confidence_level`` instead.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Qualitative confidence shown to users, mandatory on inferences.
    confidence_level: Mapped[ConfidenceLevel | None] = mapped_column(
        enum_column(ConfidenceLevel), nullable=True
    )
    #: Rule that produced this event, e.g. "loitering.zone_threshold".
    rule_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Ids of the events this inference was drawn from.
    source_event_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # ------------------------------------------------------------------ subject
    track_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    track_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    object_class: Mapped[str | None] = mapped_column(String(60), nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    #: Set on events that describe an interval (loitering, dwell).
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Neutral, factual summary, e.g.
    #: "Person (track 42) remained in zone 'Warehouse' for 84 seconds."
    summary: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    #: Type-specific payload: thresholds that fired, counts, direction, etc.
    event_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    #: Optional LLM-written narration of the structured fields above. The LLM
    #: never sees pixels and never adds facts -- see docs/EVENT_ENGINE.md.
    ai_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set when the pipeline was running on an edge box, so operators can tell
    #: locally-generated events from cloud-generated ones.
    edge_device_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: Idempotency key for edge sync: a replayed event must not duplicate.
    dedupe_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True, unique=True, index=True
    )

    evidence: Mapped[list[EventEvidence]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    alert: Mapped[Alert | None] = relationship(
        back_populates="event", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        kind = "inference" if self.is_inference else "observation"
        return f"<Event {self.event_type} {self.severity} ({kind})>"


class EventEvidence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A stored artefact supporting an event.

    Files live on disk (or object storage later), never in the database. Access
    is authorised per request and written to the audit log -- see
    ``docs/SECURITY.md``.  ``expires_at`` drives retention: evidence is deleted
    on schedule, not kept forever.
    """

    __tablename__ = "event_evidence"
    __table_args__ = (Index("ix_evidence_expiry", "expires_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )

    kind: Mapped[EvidenceKind] = mapped_column(enum_column(EvidenceKind), nullable=False)
    #: Path relative to EVIDENCE_STORAGE_PATH. Never an absolute path -- that
    #: would break when storage moves and invites path traversal.
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False, default="image/jpeg")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: SHA-256 of the file, so tampering is detectable.
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)

    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Bounding box highlighted in this artefact, normalised.
    bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)

    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    event: Mapped[Event] = relationship(back_populates="evidence")

    def __repr__(self) -> str:
        return f"<EventEvidence {self.kind} {self.storage_path}>"


class Alert(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An event escalated for human attention.

    Not every event is an alert.  Alerts are the subset a person is expected to
    look at, and they carry workflow state so a team can divide the work
    without treating the same incident twice.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_org_status_created", "organization_id", "status", "created_at"),
        Index("ix_alerts_org_severity", "organization_id", "severity"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    severity: Mapped[EventSeverity] = mapped_column(
        enum_column(EventSeverity), nullable=False, index=True
    )
    status: Mapped[AlertStatus] = mapped_column(
        enum_column(AlertStatus), nullable=False, default=AlertStatus.NEW, index=True
    )

    acknowledged_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: Operator's account of what actually happened -- the ground truth that
    #: makes it possible to measure the system's false-positive rate.
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Operator's verdict on whether the alert was correct. Null = not judged.
    was_actionable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    #: Delivery record per channel, ``{"EMAIL": "SENT", "SMS": "FAILED"}``.
    delivery_status: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    event: Mapped[Event] = relationship(back_populates="alert")
    acknowledged_by: Mapped[User | None] = relationship(foreign_keys=[acknowledged_by_id])
    resolved_by: Mapped[User | None] = relationship(foreign_keys=[resolved_by_id])

    def __repr__(self) -> str:
        return f"<Alert {self.severity} {self.status}>"
