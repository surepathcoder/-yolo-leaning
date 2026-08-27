"""Core value types for the vision engine.

Everything here is a plain dataclass with no dependency on the web framework,
the ORM or the database.  The vision engine must be importable and runnable on
an edge device with nothing but Python, OpenCV and PyTorch installed.

**Coordinates are normalised to 0..1** throughout, relative to the frame.
Pixel coordinates would be wrong the moment a camera's resolution changes or
the pipeline switches from a main stream to a sub stream -- both of which
happen routinely with real NVR installations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import numpy as np


class ReferencePoint(StrEnum):
    """Which point of a bounding box is tested against zones and lines.

    ``BOTTOM_CENTER`` is the default for people: it approximates where they
    actually stand on the floor, so a person leaning over a boundary is not
    counted as having crossed it.  ``CENTER`` suits objects on shelves, where
    there is no floor contact to reason about.
    """

    CENTER = "center"
    BOTTOM_CENTER = "bottom_center"
    TOP_CENTER = "top_center"
    CENTROID = "centroid"


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """An axis-aligned box in normalised coordinates.

    Invariants: ``0 <= x1 <= x2 <= 1`` and ``0 <= y1 <= y2 <= 1``.  Values are
    clamped on construction rather than rejected, because a detector can
    legitimately report a box slightly outside the frame for a partially
    visible object, and dropping those would lose real detections.
    """

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        # frozen=True, so write through object.__setattr__.
        x1, x2 = sorted((float(self.x1), float(self.x2)))
        y1, y2 = sorted((float(self.y1), float(self.y2)))
        object.__setattr__(self, "x1", min(max(x1, 0.0), 1.0))
        object.__setattr__(self, "y1", min(max(y1, 0.0), 1.0))
        object.__setattr__(self, "x2", min(max(x2, 0.0), 1.0))
        object.__setattr__(self, "y2", min(max(y2, 0.0), 1.0))

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def bottom_center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, self.y2)

    @property
    def top_center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, self.y1)

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height > 0 else 0.0

    def reference(
        self, point: ReferencePoint = ReferencePoint.BOTTOM_CENTER
    ) -> tuple[float, float]:
        """The point used for zone and line tests."""
        if point == ReferencePoint.BOTTOM_CENTER:
            return self.bottom_center
        if point == ReferencePoint.TOP_CENTER:
            return self.top_center
        return self.center

    def iou(self, other: BoundingBox) -> float:
        """Intersection over union with another box."""
        inter_x1 = max(self.x1, other.x1)
        inter_y1 = max(self.y1, other.y1)
        inter_x2 = min(self.x2, other.x2)
        inter_y2 = min(self.y2, other.y2)
        if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
            return 0.0
        intersection = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
        union = self.area + other.area - intersection
        return intersection / union if union > 0 else 0.0

    def intersection_area(self, other: BoundingBox) -> float:
        inter_x1 = max(self.x1, other.x1)
        inter_y1 = max(self.y1, other.y1)
        inter_x2 = min(self.x2, other.x2)
        inter_y2 = min(self.y2, other.y2)
        if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
            return 0.0
        return (inter_x2 - inter_x1) * (inter_y2 - inter_y1)

    def contains_point(self, x: float, y: float) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Convert to integer pixel coordinates for drawing or cropping."""
        return (
            round(self.x1 * width),
            round(self.y1 * height),
            round(self.x2 * width),
            round(self.y2 * height),
        )

    @classmethod
    def from_pixels(
        cls, x1: float, y1: float, x2: float, y2: float, width: int, height: int
    ) -> BoundingBox:
        """Build from pixel coordinates in a frame of the given size."""
        if width <= 0 or height <= 0:
            raise ValueError("frame width and height must be positive")
        return cls(x1 / width, y1 / height, x2 / width, y2 / height)

    @classmethod
    def from_xywh(cls, x: float, y: float, w: float, h: float) -> BoundingBox:
        """Build from a centre-point/width/height box (YOLO's native form)."""
        return cls(x - w / 2, y - h / 2, x + w / 2, y + h / 2)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded video frame plus the metadata the pipeline needs.

    ``timestamp`` is wall-clock time for the frame, not the moment it was
    processed: events must be attributable to when they happened, even if the
    pipeline is running behind.
    """

    image: np.ndarray
    index: int
    timestamp: datetime
    camera_id: str
    #: Source-reported FPS, where the source knows it.
    source_fps: float | None = None

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def shape(self) -> tuple[int, int]:
        return (self.width, self.height)

    @property
    def is_empty(self) -> bool:
        return self.image is None or self.image.size == 0


@dataclass(frozen=True, slots=True)
class Detection:
    """One object detected in one frame.

    This is an **observation**: it says a model matched a pattern at a location
    with a confidence.  It carries no interpretation and no identity.
    """

    class_id: int
    class_name: str
    confidence: float
    bbox: BoundingBox
    timestamp: datetime
    camera_id: str
    #: Assigned by the tracker; ``None`` before tracking runs.
    track_id: int | None = None
    frame_index: int | None = None
    #: Which model produced this, for comparing model versions.
    model_version: str | None = None

    def with_track_id(self, track_id: int) -> Detection:
        """Return a copy carrying a track id (Detection is immutable)."""
        return Detection(
            class_id=self.class_id,
            class_name=self.class_name,
            confidence=self.confidence,
            bbox=self.bbox,
            timestamp=self.timestamp,
            camera_id=self.camera_id,
            track_id=track_id,
            frame_index=self.frame_index,
            model_version=self.model_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "class_name": self.class_name,
            "confidence": round(self.confidence, 4),
            "bbox": [round(v, 5) for v in self.bbox.as_tuple()],
            "timestamp": self.timestamp.isoformat(),
            "camera_id": self.camera_id,
            "track_id": self.track_id,
            "frame_index": self.frame_index,
            "model_version": self.model_version,
        }


@dataclass(slots=True)
class TrackPoint:
    """One sample of a track's position."""

    timestamp: datetime
    x: float
    y: float
    bbox: BoundingBox
    confidence: float


#: Trajectory samples are capped: a full-resolution path would grow without
#: bound on a track that lingers, and adds no analytical value. A module
#: constant rather than a class attribute -- on a ``slots=True`` dataclass an
#: annotated class attribute silently becomes a field.
MAX_TRAJECTORY_POINTS = 240


@dataclass(slots=True)
class TrackedObject:
    """An object followed across frames.

    Identity here is **anonymous and local**: "person 42 on camera 4 during
    session X".  ``track_key`` includes the session id because trackers restart
    their numbering from 1 whenever a pipeline reconnects, and without it
    yesterday's track 42 would silently merge with today's.

    See ``docs/PRIVACY.md``: this is deliberately not a person's identity.
    """

    track_id: int
    class_id: int
    class_name: str
    camera_id: str
    session_id: str
    bbox: BoundingBox
    confidence: float
    first_seen: datetime
    last_seen: datetime
    frame_count: int = 1
    #: Consecutive frames in which the tracker did not match this object.
    missed_frames: int = 0
    is_active: bool = True
    best_confidence: float = 0.0
    trajectory: list[TrackPoint] = field(default_factory=list)
    #: Rule-engine state keyed by rule (zone entry times, cooldowns, ...).
    #: Kept on the track so state dies with the track rather than leaking.
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.best_confidence = max(self.best_confidence, self.confidence)

    @property
    def track_key(self) -> str:
        """Globally unique reference: ``<camera_id>:<session_id>:<track_id>``."""
        return f"{self.camera_id}:{self.session_id}:{self.track_id}"

    @property
    def duration_seconds(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds()

    def reference(
        self, point: ReferencePoint = ReferencePoint.BOTTOM_CENTER
    ) -> tuple[float, float]:
        return self.bbox.reference(point)

    def update(
        self, bbox: BoundingBox, confidence: float, timestamp: datetime, *, record: bool = True
    ) -> None:
        """Advance the track to a new observation."""
        self.bbox = bbox
        self.confidence = confidence
        self.best_confidence = max(self.best_confidence, confidence)
        self.last_seen = timestamp
        self.frame_count += 1
        self.missed_frames = 0
        self.is_active = True
        if record:
            self._record_point(timestamp)

    def _record_point(self, timestamp: datetime) -> None:
        x, y = self.bbox.bottom_center
        self.trajectory.append(
            TrackPoint(timestamp=timestamp, x=x, y=y, bbox=self.bbox, confidence=self.confidence)
        )
        if len(self.trajectory) > MAX_TRAJECTORY_POINTS:
            # Drop every other older sample rather than the oldest, so the
            # shape of the path is preserved as it is thinned.
            self.trajectory = self.trajectory[::2]

    def mark_missed(self) -> None:
        self.missed_frames += 1

    def previous_reference(
        self, point: ReferencePoint = ReferencePoint.BOTTOM_CENTER
    ) -> tuple[float, float] | None:
        """The reference point one sample ago, used for line-crossing tests."""
        if len(self.trajectory) < 2:
            return None
        return self.trajectory[-2].bbox.reference(point)

    def displacement(self) -> float:
        """Straight-line distance between the first and last recorded points."""
        if len(self.trajectory) < 2:
            return 0.0
        first, last = self.trajectory[0], self.trajectory[-1]
        return float(np.hypot(last.x - first.x, last.y - first.y))

    def path_length(self) -> float:
        """Total distance travelled along the trajectory."""
        if len(self.trajectory) < 2:
            return 0.0
        total = 0.0
        for previous, current in zip(self.trajectory, self.trajectory[1:], strict=False):
            total += float(np.hypot(current.x - previous.x, current.y - previous.y))
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "track_key": self.track_key,
            "class_name": self.class_name,
            "confidence": round(self.confidence, 4),
            "best_confidence": round(self.best_confidence, 4),
            "bbox": [round(v, 5) for v in self.bbox.as_tuple()],
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "duration_seconds": round(self.duration_seconds, 2),
            "frame_count": self.frame_count,
            "camera_id": self.camera_id,
        }


def new_session_id() -> str:
    """A short id identifying one continuous pipeline run."""
    return uuid.uuid4().hex[:12]


def utcnow() -> datetime:
    return datetime.now(UTC)
