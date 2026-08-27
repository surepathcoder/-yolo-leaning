"""Directional line crossing and people counting.

Counting one person exactly once is harder than it looks, and both failure
directions are commercially fatal: a counter that reports 400 visitors when 80
came through is useless, and one that quietly misses half of them is worse,
because nobody notices.

The mechanism is a **hysteresis band** around the line, ``min_travel`` wide:

* While a track's reference point is inside the band, nothing is decided. This
  is where a person standing in a doorway lives, and where detector jitter
  happens.
* A side is only *confirmed* once the track is clearly outside the band.
* A crossing is counted when the confirmed side flips — and only if the path
  between the two confirmed positions actually intersects the drawn line
  segment, so walking past the end of a doorway line does not count.

A simpler "did this frame's movement cross the line" test fails in a way that
is easy to miss: with slow movement or a high frame rate, both the previous
and current points sit within the band, the crossing is discarded as jitter,
and by the next frame the track is already on the far side so it never
crosses again. The person is simply never counted.

A cooldown then prevents a hesitant person -- stepping in, pausing, stepping
back -- from registering several times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from vision.types import ReferencePoint, TrackedObject
from vision.zones.geometry import (
    Point,
    distance_point_to_segment,
    normalise_line,
    segments_intersect,
    side_of_line,
)


class Direction(StrEnum):
    IN = "IN"
    OUT = "OUT"


@dataclass(frozen=True, slots=True)
class CrossingObservation:
    """One track crossed one line in one direction."""

    line_id: str
    line_name: str
    direction: Direction
    track_key: str
    track_id: int
    class_name: str
    confidence: float
    timestamp: datetime
    camera_id: str

    def to_dict(self) -> dict:
        return {
            "line_id": self.line_id,
            "line_name": self.line_name,
            "direction": self.direction.value,
            "track_key": self.track_key,
            "track_id": self.track_id,
            "class_name": self.class_name,
            "confidence": round(self.confidence, 4),
            "timestamp": self.timestamp.isoformat(),
            "camera_id": self.camera_id,
        }


@dataclass
class LineDefinition:
    """A counting line as the vision engine sees it."""

    line_id: str
    name: str
    point_a: Point
    point_b: Point
    #: Swap which side counts as IN without redrawing the line.
    invert_direction: bool = False
    reference_point: ReferencePoint = ReferencePoint.BOTTOM_CENTER
    trigger_classes: frozenset[str] = frozenset({"person"})
    #: A track cannot be counted twice within this window.
    cooldown_seconds: float = 3.0
    #: Minimum perpendicular distance the track must travel away from the line
    #: for a crossing to count, in normalised units. Roughly 1% of the frame.
    min_travel: float = 0.01
    is_active: bool = True

    def __post_init__(self) -> None:
        self.point_a, self.point_b = normalise_line(self.point_a, self.point_b)
        if isinstance(self.trigger_classes, (list, set, tuple)):
            self.trigger_classes = frozenset(self.trigger_classes or {"person"})

    def accepts(self, class_name: str) -> bool:
        return not self.trigger_classes or class_name in self.trigger_classes

    def direction_for(self, previous: Point, current: Point) -> Direction:
        """Which way a track went, from the sign change of the cross product."""
        before = side_of_line(previous, self.point_a, self.point_b)
        direction = Direction.IN if before < 0 else Direction.OUT
        if self.invert_direction:
            direction = Direction.OUT if direction == Direction.IN else Direction.IN
        return direction


@dataclass
class LineCounts:
    """Running totals for one line."""

    line_id: str
    line_name: str
    entered: int = 0
    exited: int = 0

    @property
    def net(self) -> int:
        """Entries minus exits: an estimate of people currently inside.

        An estimate, not a measurement. People leave through unmonitored doors,
        and a missed crossing biases this permanently. The pipeline clamps it
        at zero and the dashboard labels it as approximate.
        """
        return self.entered - self.exited

    def to_dict(self) -> dict:
        return {
            "line_id": self.line_id,
            "line_name": self.line_name,
            "entered": self.entered,
            "exited": self.exited,
            "net": self.net,
        }


@dataclass
class _TrackLineState:
    """Per-track hysteresis state for one line."""

    #: Sign of the last side the track was *confirmed* on (outside the band).
    #: ``None`` until it has been clearly on one side at least once.
    confirmed_side: int | None = None
    #: Where it was when that side was confirmed. The crossing test uses this
    #: rather than the previous frame, so a slow approach still yields a path
    #: segment that genuinely spans the line.
    confirmed_point: Point | None = None
    last_crossing_at: datetime | None = None
    last_direction: Direction | None = None
    crossings: int = 0


class LineCounter:
    """Counts directional crossings of virtual lines. One per camera."""

    def __init__(self, lines: list[LineDefinition], camera_id: str = "camera") -> None:
        self.camera_id = camera_id
        self.lines = {line.line_id: line for line in lines if line.is_active}
        self.counts = {
            line.line_id: LineCounts(line_id=line.line_id, line_name=line.name)
            for line in self.lines.values()
        }
        self._state: dict[tuple[str, str], _TrackLineState] = {}

    def set_lines(self, lines: list[LineDefinition]) -> None:
        """Replace the line set, preserving counts for lines that remain."""
        self.lines = {line.line_id: line for line in lines if line.is_active}
        for line in self.lines.values():
            self.counts.setdefault(
                line.line_id, LineCounts(line_id=line.line_id, line_name=line.name)
            )
        for line_id in list(self.counts):
            if line_id not in self.lines:
                del self.counts[line_id]
        self._state = {k: v for k, v in self._state.items() if k[0] in self.lines}

    def update(self, tracks: list[TrackedObject], timestamp: datetime) -> list[CrossingObservation]:
        """Check every track against every line. Returns confirmed crossings."""
        observations: list[CrossingObservation] = []
        for line in self.lines.values():
            for track in tracks:
                if not line.accepts(track.class_name):
                    continue
                observation = self._check(line, track, timestamp)
                if observation is not None:
                    observations.append(observation)
        return observations

    def _check(
        self, line: LineDefinition, track: TrackedObject, timestamp: datetime
    ) -> CrossingObservation | None:
        current = track.reference(line.reference_point)
        key = (line.line_id, track.track_key)
        state = self._state.setdefault(key, _TrackLineState())

        distance = distance_point_to_segment(current, line.point_a, line.point_b)
        if distance < line.min_travel:
            # Inside the hysteresis band: undecided. Nothing is confirmed and
            # nothing is discarded -- the track simply has not committed yet.
            return None

        signed = side_of_line(current, line.point_a, line.point_b)
        side = 1 if signed > 0 else -1

        if state.confirmed_side is None:
            state.confirmed_side = side
            state.confirmed_point = current
            return None

        if side == state.confirmed_side:
            state.confirmed_point = current
            return None

        # The side flipped. Require the path between the two confirmed
        # positions to intersect the *segment*, so movement beyond the end of
        # a finite line is not a crossing.
        origin = state.confirmed_point or current
        crossed = segments_intersect(origin, current, line.point_a, line.point_b)
        state.confirmed_side = side
        state.confirmed_point = current
        if not crossed:
            return None

        if state.last_crossing_at is not None:
            elapsed = (timestamp - state.last_crossing_at).total_seconds()
            if elapsed < line.cooldown_seconds:
                return None

        direction = line.direction_for(origin, current)
        state.last_crossing_at = timestamp
        state.last_direction = direction
        state.crossings += 1

        counts = self.counts[line.line_id]
        if direction == Direction.IN:
            counts.entered += 1
        else:
            counts.exited += 1

        return CrossingObservation(
            line_id=line.line_id,
            line_name=line.name,
            direction=direction,
            track_key=track.track_key,
            track_id=track.track_id,
            class_name=track.class_name,
            confidence=track.confidence,
            timestamp=timestamp,
            camera_id=self.camera_id,
        )

    # ------------------------------------------------------------- accessors
    def totals(self) -> dict[str, LineCounts]:
        return dict(self.counts)

    def current_occupancy(self) -> int:
        """Estimated people inside, summed across lines and clamped at zero.

        Clamped because a missed entry would otherwise produce a negative
        headcount, which is obviously wrong and undermines trust in every other
        number on the dashboard.
        """
        return max(sum(counts.net for counts in self.counts.values()), 0)

    def reset_counts(self) -> None:
        """Zero the counters -- called at the start of each business day."""
        for counts in self.counts.values():
            counts.entered = 0
            counts.exited = 0

    def prune(self, now: datetime, max_idle_seconds: float = 300.0) -> int:
        """Forget crossing state for tracks not seen recently."""
        stale = [
            key
            for key, state in self._state.items()
            if state.last_crossing_at is not None
            and (now - state.last_crossing_at).total_seconds() > max_idle_seconds
        ]
        for key in stale:
            del self._state[key]
        return len(stale)


@dataclass
class OccupancyTracker:
    """Aggregates people counts across a store's entrance lines.

    Kept separate from :class:`LineCounter` because a store's headcount is a
    store-level fact spanning several cameras, while a line counter belongs to
    one camera.
    """

    peak_occupancy: int = 0
    peak_at: datetime | None = None
    _per_camera: dict[str, int] = field(default_factory=dict)

    def report(self, camera_id: str, occupancy: int, timestamp: datetime) -> int:
        self._per_camera[camera_id] = max(occupancy, 0)
        total = self.total()
        if total > self.peak_occupancy:
            self.peak_occupancy = total
            self.peak_at = timestamp
        return total

    def total(self) -> int:
        return sum(self._per_camera.values())

    def reset(self) -> None:
        self._per_camera.clear()
        self.peak_occupancy = 0
        self.peak_at = None
