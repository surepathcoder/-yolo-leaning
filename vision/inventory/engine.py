"""Turning shelf readings into inventory observations.

A single occupancy reading is a measurement. This module holds the state that
turns a *sequence* of measurements into statements like "stock was removed
from shelf 12" -- and, just as importantly, decides when to stay quiet.

The failure mode this guards against is the phantom event. A detector that
misses one box for one frame, a shopper's shadow, a reflection off packaging:
each briefly changes coverage. Emitting on every change would bury a real
stockout under hundreds of meaningless ones, and an alert nobody trusts is an
alert nobody reads.

Three mechanisms prevent that:

*Sampling* -- shelves are read on an interval, not per frame. Stock changes on
a human timescale.

*Stability* -- a new coverage value must repeat before it is believed. One
anomalous reading changes nothing.

*Occlusion gating* -- readings taken while the shelf is blocked never move
state and never emit. This is the single most important rule here: without it
the system announces SHELF_EMPTY every time somebody stands at the shelf.

Nothing in this module concludes anything about *why* stock changed. Removal
by a customer, restocking by staff, and a detector error all produce the same
observation. Correlating that with the till is a separate concern, and the
answer it produces is explicitly labelled an inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from vision.inventory.occupancy import (
    OccupancyLevel,
    OccupancyReading,
    ShelfDefinition,
    estimate_occupancy,
)
from vision.types import Detection

#: How many consecutive agreeing readings confirm a new stable value.
DEFAULT_STABILITY_SAMPLES = 2

#: Two readings agree if they differ by less than this.
DEFAULT_STABILITY_TOLERANCE = 0.05

#: Minimum change in occupancy before a removal or return is reported. Below
#: this, the change is indistinguishable from detector noise.
DEFAULT_CHANGE_THRESHOLD = 0.10

#: A rise this large reads as restocking rather than an item being put back.
DEFAULT_RESTOCK_THRESHOLD = 0.35

#: Minimum seconds between observations of the same kind on the same shelf.
DEFAULT_COOLDOWN_SECONDS = 120.0


class InventoryObservationKind(StrEnum):
    """What was observed about a shelf.

    Mirrors the backend ``InventoryEventType`` without importing it. Every
    member describes a *measurement of the shelf*, never an actor or a motive:
    stock left the shelf, stock returned to it, the shelf is low. Who moved it
    and why is not something a camera can see.
    """

    PRODUCT_REMOVED = "PRODUCT_REMOVED"
    PRODUCT_RETURNED = "PRODUCT_RETURNED"
    LOW_STOCK = "LOW_STOCK"
    SHELF_EMPTY = "SHELF_EMPTY"
    SHELF_CHANGED = "SHELF_CHANGED"
    SHELF_RESTOCKED = "SHELF_RESTOCKED"


@dataclass(frozen=True, slots=True)
class InventoryObservation:
    """A confirmed change in a shelf's state."""

    kind: InventoryObservationKind
    shelf_id: str
    shelf_name: str
    camera_id: str
    timestamp: datetime
    occupancy_before: float | None
    occupancy_after: float
    level_before: OccupancyLevel
    level_after: OccupancyLevel
    confidence: float
    product_counts: dict[str, int] = field(default_factory=dict)
    #: Tracks near the shelf when the change was confirmed. Recorded as
    #: *context for a human*, not as attribution: presence at a shelf is not
    #: evidence that this person moved the stock, and the pipeline must never
    #: present it as such. See docs/PRIVACY.md.
    nearby_track_keys: tuple[str, ...] = ()

    @property
    def delta(self) -> float:
        """Change in occupancy. Negative means stock left the shelf."""
        if self.occupancy_before is None:
            return 0.0
        return self.occupancy_after - self.occupancy_before

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "shelf_id": self.shelf_id,
            "shelf_name": self.shelf_name,
            "camera_id": self.camera_id,
            "timestamp": self.timestamp.isoformat(),
            "occupancy_before": (
                round(self.occupancy_before, 4) if self.occupancy_before is not None else None
            ),
            "occupancy_after": round(self.occupancy_after, 4),
            "delta": round(self.delta, 4),
            "level_before": self.level_before.value,
            "level_after": self.level_after.value,
            "confidence": round(self.confidence, 4),
            "product_counts": dict(self.product_counts),
            "nearby_track_keys": list(self.nearby_track_keys),
        }


@dataclass
class _ShelfState:
    """Per-shelf memory between readings."""

    #: Last value confirmed stable. ``None`` until the shelf settles, which is
    #: why the first observation for a shelf has no ``occupancy_before``.
    stable_ratio: float | None = None
    stable_level: OccupancyLevel = OccupancyLevel.UNKNOWN
    #: Readings seen since the stable value, awaiting confirmation.
    pending: list[float] = field(default_factory=list)
    last_sampled_at: datetime | None = None
    #: Cooldown clock per observation kind.
    last_emitted: dict[InventoryObservationKind, datetime] = field(default_factory=dict)

    def due(self, now: datetime, interval_seconds: float) -> bool:
        if self.last_sampled_at is None:
            return True
        return (now - self.last_sampled_at).total_seconds() >= interval_seconds


class InventoryEngine:
    """Watches a set of shelves and reports confirmed changes.

    Holds no reference to a camera, a database or a clock: it is driven by
    whatever calls :meth:`update`, which makes it trivially testable with
    synthetic detections and a fabricated timeline.
    """

    def __init__(
        self,
        shelves: list[ShelfDefinition],
        camera_id: str = "camera",
        *,
        stability_samples: int = DEFAULT_STABILITY_SAMPLES,
        stability_tolerance: float = DEFAULT_STABILITY_TOLERANCE,
        change_threshold: float = DEFAULT_CHANGE_THRESHOLD,
        restock_threshold: float = DEFAULT_RESTOCK_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.camera_id = camera_id
        self.stability_samples = max(1, stability_samples)
        self.stability_tolerance = stability_tolerance
        self.change_threshold = change_threshold
        self.restock_threshold = restock_threshold
        self.cooldown_seconds = cooldown_seconds
        self._shelves: dict[str, ShelfDefinition] = {}
        self._state: dict[str, _ShelfState] = {}
        self.set_shelves(shelves)

    def set_shelves(self, shelves: list[ShelfDefinition]) -> None:
        """Replace the shelf set, preserving state for shelves that remain.

        A shelf whose polygon was edited keeps its history deliberately: the
        operator nudged a boundary, they did not install a new shelf.
        """
        self._shelves = {shelf.shelf_id: shelf for shelf in shelves}
        for shelf_id in list(self._state):
            if shelf_id not in self._shelves:
                del self._state[shelf_id]
        for shelf_id in self._shelves:
            self._state.setdefault(shelf_id, _ShelfState())

    @property
    def shelves(self) -> list[ShelfDefinition]:
        return list(self._shelves.values())

    def update(
        self,
        detections: list[Detection],
        timestamp: datetime,
        nearby_track_keys: tuple[str, ...] = (),
    ) -> list[InventoryObservation]:
        """Read every shelf that is due, and report confirmed changes."""
        observations: list[InventoryObservation] = []
        for shelf_id, shelf in self._shelves.items():
            if not shelf.is_monitored:
                continue
            state = self._state[shelf_id]
            if not state.due(timestamp, shelf.sample_interval_seconds):
                continue
            state.last_sampled_at = timestamp
            reading = estimate_occupancy(shelf, detections, timestamp)
            observations.extend(self._apply(shelf, state, reading, nearby_track_keys))
        return observations

    def read(self, detections: list[Detection], timestamp: datetime) -> list[OccupancyReading]:
        """Measure every monitored shelf now, ignoring the sample interval.

        For snapshot endpoints and calibration, which want a value on demand
        and must not disturb the event timeline.
        """
        return [
            estimate_occupancy(shelf, detections, timestamp)
            for shelf in self._shelves.values()
            if shelf.is_monitored
        ]

    def occupancy(self) -> dict[str, tuple[float | None, OccupancyLevel]]:
        """Current stable occupancy per shelf, for status displays."""
        return {
            shelf_id: (state.stable_ratio, state.stable_level)
            for shelf_id, state in self._state.items()
        }

    def _apply(
        self,
        shelf: ShelfDefinition,
        state: _ShelfState,
        reading: OccupancyReading,
        nearby_track_keys: tuple[str, ...],
    ) -> list[InventoryObservation]:
        # An occluded shelf is not evidence. Discard any partial confirmation
        # too: readings taken either side of somebody walking past should not
        # be stitched together into a "stable" sequence.
        if not reading.is_usable:
            state.pending.clear()
            return []

        if not self._is_stable(state, reading.occupancy_ratio):
            return []

        confirmed = sum(state.pending) / len(state.pending)
        state.pending.clear()

        previous_ratio = state.stable_ratio
        previous_level = state.stable_level
        new_level = shelf.level_for(confirmed)
        state.stable_ratio = confirmed
        state.stable_level = new_level

        # First stable reading establishes the baseline. There is no "before",
        # so there is no change to report.
        if previous_ratio is None:
            return []

        return self._observations_for(
            shelf=shelf,
            state=state,
            reading=reading,
            previous_ratio=previous_ratio,
            previous_level=previous_level,
            confirmed=confirmed,
            new_level=new_level,
            nearby_track_keys=nearby_track_keys,
        )

    def _is_stable(self, state: _ShelfState, ratio: float) -> bool:
        """Accumulate readings until ``stability_samples`` of them agree."""
        if state.pending and abs(ratio - state.pending[-1]) > self.stability_tolerance:
            # The value moved while we were confirming it. Start again from
            # here rather than averaging across a transition.
            state.pending = [ratio]
            return False
        state.pending.append(ratio)
        return len(state.pending) >= self.stability_samples

    def _observations_for(
        self,
        *,
        shelf: ShelfDefinition,
        state: _ShelfState,
        reading: OccupancyReading,
        previous_ratio: float,
        previous_level: OccupancyLevel,
        confirmed: float,
        new_level: OccupancyLevel,
        nearby_track_keys: tuple[str, ...],
    ) -> list[InventoryObservation]:
        delta = confirmed - previous_ratio
        observations: list[InventoryObservation] = []

        def emit(kind: InventoryObservationKind) -> None:
            if not self._cooled_down(state, kind, reading.timestamp):
                return
            state.last_emitted[kind] = reading.timestamp
            observations.append(
                InventoryObservation(
                    kind=kind,
                    shelf_id=shelf.shelf_id,
                    shelf_name=shelf.name,
                    camera_id=shelf.camera_id,
                    timestamp=reading.timestamp,
                    occupancy_before=previous_ratio,
                    occupancy_after=confirmed,
                    level_before=previous_level,
                    level_after=new_level,
                    confidence=reading.confidence,
                    product_counts=dict(reading.product_counts),
                    nearby_track_keys=nearby_track_keys,
                )
            )

        if delta <= -self.change_threshold:
            emit(InventoryObservationKind.PRODUCT_REMOVED)
        elif delta >= self.restock_threshold:
            emit(InventoryObservationKind.SHELF_RESTOCKED)
        elif delta >= self.change_threshold:
            emit(InventoryObservationKind.PRODUCT_RETURNED)

        # Level transitions are reported on entry only. A shelf that sits
        # empty all afternoon is one stockout, not one every sample.
        if new_level is not previous_level:
            if new_level is OccupancyLevel.EMPTY:
                emit(InventoryObservationKind.SHELF_EMPTY)
            elif new_level is OccupancyLevel.LOW:
                emit(InventoryObservationKind.LOW_STOCK)

        # Something moved, but not enough to name. Worth recording so the
        # shelf's history is continuous, and cheap to filter out downstream.
        if not observations and abs(delta) > self.stability_tolerance:
            emit(InventoryObservationKind.SHELF_CHANGED)

        return observations

    def _cooled_down(
        self,
        state: _ShelfState,
        kind: InventoryObservationKind,
        now: datetime,
    ) -> bool:
        last = state.last_emitted.get(kind)
        if last is None:
            return True
        return (now - last).total_seconds() >= self.cooldown_seconds

    def reset(self, shelf_id: str | None = None) -> None:
        """Forget stable state, for one shelf or all of them.

        Used when a pipeline reconnects: coverage measured before a camera
        dropped says nothing about the shelf now.
        """
        targets = [shelf_id] if shelf_id else list(self._state)
        for target in targets:
            if target in self._state:
                self._state[target] = _ShelfState()
