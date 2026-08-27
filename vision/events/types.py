"""The event vocabulary of the vision engine.

Mirrors the backend's enums without importing them: ``vision/`` must stay
runnable with no database and no web framework. The pipeline translates
between the two, and a test asserts the vocabularies have not drifted apart.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    # presence
    PERSON_DETECTED = "PERSON_DETECTED"
    OBJECT_DETECTED = "OBJECT_DETECTED"
    VEHICLE_DETECTED = "VEHICLE_DETECTED"

    # zones
    PERSON_ENTERED_ZONE = "PERSON_ENTERED_ZONE"
    PERSON_EXITED_ZONE = "PERSON_EXITED_ZONE"
    OBJECT_ENTERED_ZONE = "OBJECT_ENTERED_ZONE"
    OBJECT_EXITED_ZONE = "OBJECT_EXITED_ZONE"
    RESTRICTED_AREA_ENTRY = "RESTRICTED_AREA_ENTRY"

    # counting
    LINE_CROSSED = "LINE_CROSSED"
    OCCUPANCY_THRESHOLD_EXCEEDED = "OCCUPANCY_THRESHOLD_EXCEEDED"

    # behaviour over time
    LOITERING = "LOITERING"
    DWELL_THRESHOLD_EXCEEDED = "DWELL_THRESHOLD_EXCEEDED"
    AFTER_HOURS_ACTIVITY = "AFTER_HOURS_ACTIVITY"

    # inventory
    PRODUCT_REMOVED = "PRODUCT_REMOVED"
    PRODUCT_RETURNED = "PRODUCT_RETURNED"
    LOW_STOCK = "LOW_STOCK"
    SHELF_EMPTY = "SHELF_EMPTY"
    SHELF_CHANGED = "SHELF_CHANGED"

    # inferences -- conclusions, not measurements
    POSSIBLE_INVENTORY_DISCREPANCY = "POSSIBLE_INVENTORY_DISCREPANCY"
    POTENTIAL_SECURITY_CONCERN = "POTENTIAL_SECURITY_CONCERN"

    # system
    CAMERA_ONLINE = "CAMERA_ONLINE"
    CAMERA_OFFLINE = "CAMERA_OFFLINE"
    PIPELINE_ERROR = "PIPELINE_ERROR"


INFERENCE_EVENT_TYPES = frozenset(
    {
        EventType.POSSIBLE_INVENTORY_DISCREPANCY,
        EventType.POTENTIAL_SECURITY_CONCERN,
    }
)


class Severity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class ConfidenceLevel(StrEnum):
    """Qualitative confidence attached to an inference.

    Coarse on purpose: a decimal score would imply a precision the system does
    not have when combining a detection confidence, a time window and a
    threshold into one judgement.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(slots=True)
class VisionEvent:
    """Something the event engine concluded from observations.

    ``is_inference`` is the field that matters most.

    * ``False`` -- a measurement. "Track 42 entered zone 3 at 14:42:10." The
      camera saw this.
    * ``True`` -- a conclusion drawn from other events. ``source_event_ids``
      and ``rule_name`` record what it rests on, so a human can audit the
      reasoning and disagree with it.

    Nothing downstream may collapse the two: that would let a guess be read as
    evidence, which is exactly the failure this product exists to avoid.
    """

    event_type: EventType
    severity: Severity
    camera_id: str
    occurred_at: datetime

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str | None = None
    zone_id: str | None = None
    line_id: str | None = None
    shelf_id: str | None = None
    product_id: str | None = None

    track_key: str | None = None
    track_id: int | None = None
    object_class: str | None = None

    #: Model/geometry confidence for an observation (0..1).
    confidence: float | None = None
    #: Qualitative confidence -- required on inferences.
    confidence_level: ConfidenceLevel | None = None

    is_inference: bool = False
    rule_name: str | None = None
    source_event_ids: list[str] = field(default_factory=list)

    ended_at: datetime | None = None
    duration_seconds: float | None = None

    #: Neutral, factual sentence. Never asserts intent.
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.is_inference and self.confidence_level is None:
            # An inference without a stated confidence would read as a fact.
            self.confidence_level = ConfidenceLevel.LOW
        if self.event_type in INFERENCE_EVENT_TYPES:
            self.is_inference = True

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER[self.severity]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "camera_id": self.camera_id,
            "store_id": self.store_id,
            "zone_id": self.zone_id,
            "line_id": self.line_id,
            "shelf_id": self.shelf_id,
            "product_id": self.product_id,
            "track_key": self.track_key,
            "track_id": self.track_id,
            "object_class": self.object_class,
            "confidence": round(self.confidence, 4) if self.confidence is not None else None,
            "confidence_level": (self.confidence_level.value if self.confidence_level else None),
            "is_inference": self.is_inference,
            "rule_name": self.rule_name,
            "source_event_ids": list(self.source_event_ids),
            "occurred_at": self.occurred_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": (
                round(self.duration_seconds, 2) if self.duration_seconds is not None else None
            ),
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }
