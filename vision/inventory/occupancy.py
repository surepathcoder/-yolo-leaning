"""Shelf occupancy estimation.

The question this module answers is deliberately modest: **what fraction of a
shelf region currently has something on it?**  It does not count units, and it
does not identify which product is which beyond the detector's own class name.

CCTV at shelf-monitoring angles cannot reliably resolve individual items on a
packed shelf.  A system that reported "17 units" would be confidently wrong
most of the time, and every downstream number would inherit that error.  A
coverage ratio degrades honestly: when the view is bad the ratio is wrong by a
little, not by an invented integer.

Two properties matter more than accuracy here:

*Determinism* -- identical input must give an identical ratio.  Occupancy is
compared against thresholds over time, so a value that jitters between
identical frames would manufacture stock events out of noise.  Every estimate
below is computed on a fixed grid; nothing is sampled randomly.

*Knowing when not to answer* -- a person standing at a shelf hides it.  The
honest output then is ``UNKNOWN``, not "empty".  Occlusion is measured and
reported rather than silently folded into the ratio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import numpy as np

from vision.types import BoundingBox, Detection
from vision.zones.geometry import (
    Point,
    normalise_polygon,
    points_in_polygon,
    polygon_bounds,
)

#: Resolution of the sampling grid laid over a shelf's bounding box.  64x64
#: gives ~0.02% area resolution on a typical shelf, which is far finer than
#: the thresholds anyone sets, and costs well under a millisecond.
GRID_RESOLUTION = 64

#: Above this ratio a shelf reads as FULL rather than NORMAL.  Not a
#: configurable per-shelf threshold: unlike "low" and "empty" it triggers no
#: action, it only labels a display.
FULL_THRESHOLD = 0.85

#: Classes that hide a shelf rather than sit on it.  A person at the shelf is
#: the overwhelmingly common case; vehicles matter in warehouse loading bays.
DEFAULT_OCCLUSION_CLASSES = frozenset({"person", "car", "truck", "forklift"})


class OccupancyLevel(StrEnum):
    """Coarse shelf fullness.

    Mirrors the backend enum of the same name without importing it --
    ``vision/`` stays runnable with no database.  A test asserts the two have
    not drifted apart.
    """

    FULL = "FULL"
    NORMAL = "NORMAL"
    LOW = "LOW"
    EMPTY = "EMPTY"
    UNKNOWN = "UNKNOWN"


@dataclass
class ShelfDefinition:
    """A shelf as the vision engine sees it.

    Built from the database row by the pipeline; the vision engine never reads
    the database itself.
    """

    shelf_id: str
    name: str
    polygon: list[Point]
    camera_id: str = "camera"
    #: Detector classes that count as stock on this shelf.  Empty means "any
    #: class that is not an occluder" -- the right default for a generic model
    #: on an unlabelled shelf, where a box is a box.
    product_classes: frozenset[str] = frozenset()
    #: Classes that hide the shelf instead of stocking it.
    occlusion_classes: frozenset[str] = DEFAULT_OCCLUSION_CLASSES
    #: Coverage seen when this shelf is known to be full.  Real shelves never
    #: reach 1.0 -- gaps, shadows and the shelf edge itself all read as empty
    #: -- so raw coverage is divided by this to get a usable ratio.  ``None``
    #: means "not calibrated yet"; raw coverage is then reported as-is.
    baseline_occupancy: float | None = None
    #: Ratio at or below which stock is low enough to act on.
    low_threshold: float = 0.35
    #: Ratio at or below which the shelf reads as empty.
    empty_threshold: float = 0.12
    #: Fraction of the shelf an occluder must cover before readings are
    #: suppressed.  A shopper's arm reaching in covers very little and should
    #: not blind the shelf; a shopper standing squarely in front covers most.
    occlusion_threshold: float = 0.25
    #: Detections below this confidence are ignored entirely.
    min_confidence: float = 0.25
    #: Seconds between readings.  Shelves change on a human timescale; running
    #: this every frame would burn CPU to produce the same number repeatedly.
    sample_interval_seconds: float = 60.0
    is_active: bool = True
    monitoring_enabled: bool = True

    def __post_init__(self) -> None:
        self.polygon = normalise_polygon(self.polygon)
        if isinstance(self.product_classes, (list, set, tuple)):
            self.product_classes = frozenset(self.product_classes)
        if isinstance(self.occlusion_classes, (list, set, tuple)):
            self.occlusion_classes = frozenset(self.occlusion_classes)
        if not 0.0 <= self.empty_threshold <= self.low_threshold <= 1.0:
            raise ValueError(
                f"shelf {self.shelf_id}: thresholds must satisfy "
                f"0 <= empty ({self.empty_threshold}) <= low ({self.low_threshold}) <= 1"
            )
        if self.baseline_occupancy is not None and not 0.0 < self.baseline_occupancy <= 1.0:
            raise ValueError(
                f"shelf {self.shelf_id}: baseline_occupancy must be in (0, 1], "
                f"got {self.baseline_occupancy}"
            )

    @property
    def is_monitored(self) -> bool:
        return self.is_active and self.monitoring_enabled

    def is_product(self, class_name: str) -> bool:
        """Does this detection class count as stock on this shelf?"""
        if class_name in self.occlusion_classes:
            return False
        if not self.product_classes:
            return True
        return class_name in self.product_classes

    def is_occluder(self, class_name: str) -> bool:
        return class_name in self.occlusion_classes

    def level_for(self, ratio: float) -> OccupancyLevel:
        """Classify a ratio. Thresholds are inclusive at the low end."""
        if ratio <= self.empty_threshold:
            return OccupancyLevel.EMPTY
        if ratio <= self.low_threshold:
            return OccupancyLevel.LOW
        if ratio >= FULL_THRESHOLD:
            return OccupancyLevel.FULL
        return OccupancyLevel.NORMAL


@dataclass(frozen=True, slots=True)
class OccupancyReading:
    """One measurement of one shelf at one moment.

    This is an **observation**.  It reports coverage and how much of the view
    was blocked; it draws no conclusion about why the shelf looks the way it
    does.
    """

    shelf_id: str
    shelf_name: str
    camera_id: str
    timestamp: datetime
    #: Coverage after baseline correction, clamped to [0, 1].
    occupancy_ratio: float
    #: Coverage before baseline correction -- what was actually measured.
    raw_coverage: float
    level: OccupancyLevel
    #: How much to trust this reading: driven down by occlusion and by weak
    #: detections.  Not the detector's confidence in any one box.
    confidence: float
    is_occluded: bool
    #: Fraction of the shelf hidden by occluders.
    occluded_ratio: float
    detected_object_count: int
    #: Detections per class name, for shelves stocked with a known product.
    product_counts: dict[str, int] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        """Should this reading be allowed to move shelf state?

        An occluded shelf is not evidence about stock.  Treating it as such is
        how a system reports SHELF_EMPTY every time somebody stands in front
        of the shelf.
        """
        return not self.is_occluded and self.level is not OccupancyLevel.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "shelf_id": self.shelf_id,
            "shelf_name": self.shelf_name,
            "camera_id": self.camera_id,
            "timestamp": self.timestamp.isoformat(),
            "occupancy_ratio": round(self.occupancy_ratio, 4),
            "raw_coverage": round(self.raw_coverage, 4),
            "level": self.level.value,
            "confidence": round(self.confidence, 4),
            "is_occluded": self.is_occluded,
            "occluded_ratio": round(self.occluded_ratio, 4),
            "detected_object_count": self.detected_object_count,
            "product_counts": dict(self.product_counts),
        }


def _grid_over(polygon: list[Point]) -> tuple[np.ndarray, np.ndarray]:
    """Return the sample points inside ``polygon`` and their coordinates.

    The grid spans the polygon's bounding box; points outside the polygon are
    masked away, so an L-shaped or angled shelf is measured over its true area
    rather than its bounding rectangle.
    """
    x1, y1, x2, y2 = polygon_bounds(polygon)
    xs = np.linspace(x1, x2, GRID_RESOLUTION)
    ys = np.linspace(y1, y2, GRID_RESOLUTION)
    grid_x, grid_y = np.meshgrid(xs, ys)
    points = np.column_stack([grid_x.ravel(), grid_y.ravel()])
    return points, points_in_polygon(points, polygon)


def _covered_by(points: np.ndarray, boxes: list[BoundingBox]) -> np.ndarray:
    """Boolean mask: which sample points fall inside any of ``boxes``.

    Union semantics, not a sum.  Two detections overlapping the same shelf
    region describe one covered patch, and adding their areas would report a
    shelf as more than full.
    """
    covered = np.zeros(len(points), dtype=bool)
    if not boxes:
        return covered
    px, py = points[:, 0], points[:, 1]
    for box in boxes:
        covered |= (px >= box.x1) & (px <= box.x2) & (py >= box.y1) & (py <= box.y2)
    return covered


def estimate_occupancy(
    shelf: ShelfDefinition,
    detections: list[Detection],
    timestamp: datetime,
) -> OccupancyReading:
    """Measure how much of ``shelf`` is covered by stock.

    Coverage is the fraction of the shelf polygon's area that falls inside at
    least one product detection.  Occluders are measured separately over the
    same grid: they are not stock, and where they cover the shelf we do not
    know what is behind them.
    """
    products: list[BoundingBox] = []
    occluders: list[BoundingBox] = []
    product_counts: dict[str, int] = {}
    confidences: list[float] = []

    for detection in detections:
        if detection.confidence < shelf.min_confidence:
            continue
        if shelf.is_occluder(detection.class_name):
            occluders.append(detection.bbox)
        elif shelf.is_product(detection.class_name):
            products.append(detection.bbox)
            confidences.append(detection.confidence)
            product_counts[detection.class_name] = product_counts.get(detection.class_name, 0) + 1

    points, inside = _grid_over(shelf.polygon)
    shelf_cells = int(inside.sum())
    if shelf_cells == 0:
        # A polygon too thin to contain a single grid point. Degenerate rather
        # than empty -- say so instead of reporting 0.0 coverage.
        return OccupancyReading(
            shelf_id=shelf.shelf_id,
            shelf_name=shelf.name,
            camera_id=shelf.camera_id,
            timestamp=timestamp,
            occupancy_ratio=0.0,
            raw_coverage=0.0,
            level=OccupancyLevel.UNKNOWN,
            confidence=0.0,
            is_occluded=False,
            occluded_ratio=0.0,
            detected_object_count=0,
            product_counts={},
        )

    occluded_mask = _covered_by(points, occluders) & inside
    occluded_ratio = float(occluded_mask.sum()) / shelf_cells
    is_occluded = occluded_ratio >= shelf.occlusion_threshold

    # Product coverage is measured over the *visible* part of the shelf. A
    # shelf half hidden by a shopper, with the visible half fully stocked,
    # reads as stocked -- not as half empty.
    visible_mask = inside & ~occluded_mask
    visible_cells = int(visible_mask.sum())
    product_mask = _covered_by(points, products) & visible_mask
    raw_coverage = float(product_mask.sum()) / visible_cells if visible_cells else 0.0

    ratio = raw_coverage / shelf.baseline_occupancy if shelf.baseline_occupancy else raw_coverage
    ratio = min(max(ratio, 0.0), 1.0)

    level = OccupancyLevel.UNKNOWN if is_occluded else shelf.level_for(ratio)

    # Confidence blends how much of the shelf we could see with how strongly
    # the detector believed what it saw. An empty shelf has no detections to
    # average, and that is a clear observation, not a doubtful one.
    visibility = 1.0 - occluded_ratio
    detection_confidence = sum(confidences) / len(confidences) if confidences else 1.0
    confidence = 0.0 if is_occluded else round(visibility * detection_confidence, 6)

    return OccupancyReading(
        shelf_id=shelf.shelf_id,
        shelf_name=shelf.name,
        camera_id=shelf.camera_id,
        timestamp=timestamp,
        occupancy_ratio=ratio,
        raw_coverage=raw_coverage,
        level=level,
        confidence=confidence,
        is_occluded=is_occluded,
        occluded_ratio=occluded_ratio,
        detected_object_count=len(products),
        product_counts=product_counts,
    )


def calibrate_baseline(readings: list[OccupancyReading]) -> float | None:
    """Derive a ``baseline_occupancy`` from readings of a known-full shelf.

    Uses the median rather than the max: one frame where a passing reflection
    inflated coverage would otherwise become the baseline forever, and every
    subsequent reading would be scaled down against it.  Occluded readings are
    discarded -- they measure the occluder, not the shelf.
    """
    usable = [r.raw_coverage for r in readings if not r.is_occluded]
    if not usable:
        return None
    baseline = float(np.median(usable))
    return baseline if baseline > 0.0 else None
