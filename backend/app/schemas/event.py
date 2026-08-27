"""Event and alert schemas.

The one thing these schemas must never do is let an inference read as a
measurement. ``is_inference``, ``confidence_level``, ``rule_name`` and
``source_event_ids`` are all surfaced so the UI can render the two
differently, and ``EventRead`` carries an explicit ``kind`` discriminator so a
client cannot forget to check.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import (
    INFERENCE_EVENT_TYPES,
    AlertStatus,
    ConfidenceLevel,
    EventSeverity,
    EventType,
    EvidenceKind,
)
from app.schemas.common import APISchema


class EvidenceRead(APISchema):
    """An evidence artefact. The URL is authorised per request, never public."""

    id: uuid.UUID
    kind: EvidenceKind
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    duration_seconds: float | None
    bbox: list | None
    captured_at: datetime
    expires_at: datetime | None
    #: Relative API path. Requires evidence:view and is audited on access.
    url: str | None = None


class EventRead(APISchema):
    id: uuid.UUID
    organization_id: uuid.UUID
    store_id: uuid.UUID | None
    camera_id: uuid.UUID | None
    zone_id: uuid.UUID | None
    shelf_id: uuid.UUID | None
    product_id: uuid.UUID | None

    event_type: EventType
    severity: EventSeverity

    #: True when this is a conclusion drawn from other events rather than
    #: something the camera measured.
    is_inference: bool
    confidence: float | None
    confidence_level: ConfidenceLevel | None
    rule_name: str | None
    source_event_ids: list[str]

    track_key: str | None
    track_id: int | None
    object_class: str | None

    occurred_at: datetime
    ended_at: datetime | None
    duration_seconds: float | None

    summary: str
    event_metadata: dict
    ai_explanation: str | None = None
    edge_device_id: str | None = None
    created_at: datetime
    evidence: list[EvidenceRead] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def kind(self) -> Literal["observation", "inference"]:
        """Explicit discriminator so a client cannot forget to check.

        A UI that renders an inference with the same weight as a measurement
        turns a guess into evidence, which is the failure this product exists
        to avoid.
        """
        return "inference" if self.is_inference else "observation"


class EventSummary(APISchema):
    """An event as it appears on the timeline."""

    id: uuid.UUID
    event_type: EventType
    severity: EventSeverity
    is_inference: bool
    confidence_level: ConfidenceLevel | None
    camera_id: uuid.UUID | None
    store_id: uuid.UUID | None
    zone_id: uuid.UUID | None
    track_id: int | None
    occurred_at: datetime
    duration_seconds: float | None
    summary: str
    has_evidence: bool = False


class EventDetail(EventRead):
    """A single event with the observations behind it, when it is an inference."""

    source_events: list[EventSummary] = Field(default_factory=list)
    alert_id: uuid.UUID | None = None


class EventFilters(BaseModel):
    """Timeline filters. Applied in PostgreSQL, never in the browser."""

    model_config = ConfigDict(extra="forbid")

    store_id: uuid.UUID | None = None
    camera_id: uuid.UUID | None = None
    zone_id: uuid.UUID | None = None
    event_types: list[EventType] | None = None
    severities: list[EventSeverity] | None = None
    track_key: str | None = Field(default=None, max_length=120)
    product_id: uuid.UUID | None = None
    is_inference: bool | None = None
    since: datetime | None = None
    until: datetime | None = None


class EventTypeInfo(BaseModel):
    """Describes one event type, for building filter UIs."""

    value: str
    label: str
    is_inference: bool


def event_type_catalog() -> list[EventTypeInfo]:
    return [
        EventTypeInfo(
            value=event_type.value,
            label=event_type.value.replace("_", " ").capitalize(),
            is_inference=event_type in INFERENCE_EVENT_TYPES,
        )
        for event_type in EventType
    ]


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


class AlertRead(APISchema):
    id: uuid.UUID
    organization_id: uuid.UUID
    store_id: uuid.UUID | None
    event_id: uuid.UUID
    title: str
    message: str
    severity: EventSeverity
    status: AlertStatus
    acknowledged_by_id: uuid.UUID | None
    acknowledged_at: datetime | None
    resolved_by_id: uuid.UUID | None
    resolved_at: datetime | None
    resolution_notes: str | None
    was_actionable: bool | None
    delivery_status: dict
    created_at: datetime
    event: EventSummary | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def occurrences(self) -> int:
        """How many times this incident recurred while the alert was open.

        Repeats within the grouping window update this instead of creating
        another alert, so a person loitering for twenty minutes is one row
        with a count, not forty rows.
        """
        try:
            return int(self.delivery_status.get("occurrences", 1))
        except (AttributeError, TypeError, ValueError):
            return 1


class AlertSummaryResponse(BaseModel):
    by_status: dict[str, int]
    open_by_severity: dict[str, int]
    open_total: int
    high_priority_open: int


class AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveAlertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str | None = Field(default=None, max_length=4000)
    #: The operator's verdict on whether the alert was worth raising. This is
    #: the only ground truth available for measuring false positives, so the
    #: UI asks for it every time.
    was_actionable: bool | None = None
    dismiss: bool = False


class FalsePositiveReport(BaseModel):
    judged: int
    actionable: int
    not_actionable: int
    false_positive_rate: float | None


class TodaySummary(BaseModel):
    window_hours: int
    events: int
    inferences: int
    high_severity: int
    line_crossings: int


def build_event_summary(event) -> EventSummary:
    return EventSummary(
        id=event.id,
        event_type=event.event_type,
        severity=event.severity,
        is_inference=event.is_inference,
        confidence_level=event.confidence_level,
        camera_id=event.camera_id,
        store_id=event.store_id,
        zone_id=event.zone_id,
        track_id=event.track_id,
        occurred_at=event.occurred_at,
        duration_seconds=event.duration_seconds,
        summary=event.summary,
        has_evidence=bool(event.evidence),
    )


def build_evidence_read(evidence) -> EvidenceRead:
    return EvidenceRead(
        id=evidence.id,
        kind=evidence.kind,
        mime_type=evidence.mime_type,
        size_bytes=evidence.size_bytes,
        width=evidence.width,
        height=evidence.height,
        duration_seconds=evidence.duration_seconds,
        bbox=evidence.bbox,
        captured_at=evidence.captured_at,
        expires_at=evidence.expires_at,
        url=f"/api/v1/evidence/{evidence.id}",
    )
