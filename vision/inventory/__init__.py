"""Shelf occupancy and inventory observation.

Measures how full a shelf is and reports confirmed changes. It never concludes
why stock moved -- see ``docs/PRIVACY.md``.
"""

from vision.inventory.engine import (
    InventoryEngine,
    InventoryObservation,
    InventoryObservationKind,
)
from vision.inventory.occupancy import (
    FULL_THRESHOLD,
    OccupancyLevel,
    OccupancyReading,
    ShelfDefinition,
    calibrate_baseline,
    estimate_occupancy,
)

__all__ = [
    "FULL_THRESHOLD",
    "InventoryEngine",
    "InventoryObservation",
    "InventoryObservationKind",
    "OccupancyLevel",
    "OccupancyReading",
    "ShelfDefinition",
    "calibrate_baseline",
    "estimate_occupancy",
]
