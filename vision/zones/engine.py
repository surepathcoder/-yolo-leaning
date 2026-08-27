"""Zone occupancy tracking.

Turns geometry into **observations**: this track entered that zone at this
time, and left it after this long. It draws no conclusions — loitering,
after-hours activity and restricted-area rules all live in the event engine
and read these observations.

The one piece of real subtlety is debouncing. A tracked person standing on a
zone boundary produces a reference point that flickers in and out with every
frame of detector jitter. Reported naively that is dozens of enter/exit events
per minute, which buries genuine ones. A zone entry is therefore only
*confirmed* once the object has been inside continuously for a short window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from vision.types import ReferencePoint, TrackedObject
from vision.zones.geometry import Point, normalise_polygon, point_in_polygon


class ZoneKind(StrEnum):
    """Mirrors the backend's ``ZoneType`` without importing it."""

    RESTRICTED = "RESTRICTED"
    ENTRANCE = "ENTRANCE"
    EXIT = "EXIT"
    SHELF = "SHELF"
    CHECKOUT = "CHECKOUT"
    WAREHOUSE = "WAREHOUSE"
    CUSTOM = "CUSTOM"


class ZoneEventKind(StrEnum):
    ENTERED = "ENTERED"
    EXITED = "EXITED"


@dataclass(frozen=True, slots=True)
class ZoneObservation:
    """A confirmed zone entry or exit for one track."""

    kind: ZoneEventKind
    zone_id: str
    zone_name: str
    zone_kind: ZoneKind
    track_key: str
    track_id: int
    class_name: str
    confidence: float
    timestamp: datetime
    camera_id: str
    #: Seconds spent inside; set on EXITED only.
    dwell_seconds: float | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "zone_id": self.zone_id,
            "zone_name": self.zone_name,
            "zone_kind": self.zone_kind.value,
            "track_key": self.track_key,
            "track_id": self.track_id,
            "class_name": self.class_name,
            "confidence": round(self.confidence, 4),
            "timestamp": self.timestamp.isoformat(),
            "camera_id": self.camera_id,
            "dwell_seconds": (
                round(self.dwell_seconds, 2) if self.dwell_seconds is not None else None
            ),
        }


@dataclass
class ZoneDefinition:
    """A zone as the vision engine sees it.

    Built from the database row by the pipeline; the vision engine never reads
    the database itself.
    """

    zone_id: str
    name: str
    polygon: list[Point]
    kind: ZoneKind = ZoneKind.CUSTOM
    reference_point: ReferencePoint = ReferencePoint.BOTTOM_CENTER
    #: Classes this zone reacts to. Empty means person only.
    trigger_classes: frozenset[str] = frozenset({"person"})
    #: Seconds of continuous presence before an entry is confirmed.
    entry_debounce_seconds: float = 1.0
    #: Seconds of continuous absence before an exit is confirmed. Longer than
    #: entry debounce because a person briefly occluded should not read as
    #: having left and re-entered.
    exit_debounce_seconds: float = 2.0
    loitering_threshold_seconds: float | None = None
    dwell_threshold_seconds: float | None = None
    after_hours_monitoring: bool = False
    max_occupancy: int | None = None
    is_active: bool = True

    def __post_init__(self) -> None:
        self.polygon = normalise_polygon(self.polygon)
        if isinstance(self.trigger_classes, (list, set, tuple)):
            self.trigger_classes = frozenset(self.trigger_classes or {"person"})

    def accepts(self, class_name: str) -> bool:
        return not self.trigger_classes or class_name in self.trigger_classes

    def contains(self, track: TrackedObject) -> bool:
        return point_in_polygon(track.reference(self.reference_point), self.polygon)


@dataclass
class _Presence:
    """Per-track, per-zone debounce state."""

    #: When the object was first seen inside, before confirmation.
    candidate_since: datetime | None = None
    #: When entry was confirmed. ``None`` means not currently inside.
    entered_at: datetime | None = None
    #: When it was first seen outside again, before confirming the exit.
    leaving_since: datetime | None = None
    last_seen: datetime | None = None


@dataclass
class ZoneOccupancy:
    """A snapshot of who is in a zone right now."""

    zone_id: str
    zone_name: str
    track_keys: set[str] = field(default_factory=set)

    @property
    def count(self) -> int:
        return len(self.track_keys)


class ZoneEngine:
    """Tracks which objects are inside which zones.

    One engine per camera. State is keyed by ``(zone_id, track_key)`` and is
    dropped when a track disappears, so memory does not grow across a long run.
    """

    def __init__(self, zones: list[ZoneDefinition], camera_id: str = "camera") -> None:
        self.camera_id = camera_id
        self.zones = {zone.zone_id: zone for zone in zones if zone.is_active}
        self._presence: dict[tuple[str, str], _Presence] = {}

    def set_zones(self, zones: list[ZoneDefinition]) -> None:
        """Replace the zone set, e.g. after an operator redraws one.

        Presence state for zones that no longer exist is discarded rather than
        kept: a zone that was deleted cannot legitimately emit an exit event.
        """
        self.zones = {zone.zone_id: zone for zone in zones if zone.is_active}
        self._presence = {
            key: value for key, value in self._presence.items() if key[0] in self.zones
        }

    def update(self, tracks: list[TrackedObject], timestamp: datetime) -> list[ZoneObservation]:
        """Evaluate every track against every zone. Returns confirmed changes."""
        observations: list[ZoneObservation] = []
        live_keys = {track.track_key for track in tracks}

        for zone in self.zones.values():
            for track in tracks:
                if not zone.accepts(track.class_name):
                    continue
                observation = self._evaluate(zone, track, timestamp)
                if observation is not None:
                    observations.append(observation)

        observations.extend(self._close_vanished_tracks(live_keys, timestamp))
        return observations

    def _evaluate(
        self, zone: ZoneDefinition, track: TrackedObject, timestamp: datetime
    ) -> ZoneObservation | None:
        key = (zone.zone_id, track.track_key)
        presence = self._presence.setdefault(key, _Presence())
        presence.last_seen = timestamp
        inside = zone.contains(track)

        if inside:
            presence.leaving_since = None
            if presence.entered_at is not None:
                return None  # already inside and confirmed

            if presence.candidate_since is None:
                presence.candidate_since = timestamp

            # Checked on the same call that starts the candidacy, so a zone
            # configured with zero debounce confirms immediately rather than
            # costing an extra frame.
            elapsed = (timestamp - presence.candidate_since).total_seconds()
            if elapsed >= zone.entry_debounce_seconds:
                presence.entered_at = presence.candidate_since
                presence.candidate_since = None
                return ZoneObservation(
                    kind=ZoneEventKind.ENTERED,
                    zone_id=zone.zone_id,
                    zone_name=zone.name,
                    zone_kind=zone.kind,
                    track_key=track.track_key,
                    track_id=track.track_id,
                    class_name=track.class_name,
                    confidence=track.confidence,
                    # Report when they actually entered, not when we confirmed
                    # it: the debounce is our uncertainty, not their behaviour.
                    timestamp=presence.entered_at,
                    camera_id=self.camera_id,
                )
            return None

        # --- outside ---------------------------------------------------------
        presence.candidate_since = None
        if presence.entered_at is None:
            return None

        if presence.leaving_since is None:
            presence.leaving_since = timestamp

        elapsed = (timestamp - presence.leaving_since).total_seconds()
        if elapsed >= zone.exit_debounce_seconds:
            entered_at = presence.entered_at
            left_at = presence.leaving_since
            presence.entered_at = None
            presence.leaving_since = None
            return ZoneObservation(
                kind=ZoneEventKind.EXITED,
                zone_id=zone.zone_id,
                zone_name=zone.name,
                zone_kind=zone.kind,
                track_key=track.track_key,
                track_id=track.track_id,
                class_name=track.class_name,
                confidence=track.confidence,
                timestamp=left_at,
                camera_id=self.camera_id,
                dwell_seconds=(left_at - entered_at).total_seconds(),
            )
        return None

    def _close_vanished_tracks(
        self, live_keys: set[str], timestamp: datetime
    ) -> list[ZoneObservation]:
        """Emit exits for tracks that disappeared while inside a zone.

        A person who walks out of frame from inside a restricted area has still
        left it. Without this the zone would show them present forever and the
        occupancy count would drift upward all day.
        """
        observations: list[ZoneObservation] = []
        for key in list(self._presence.keys()):
            zone_id, track_key = key
            if track_key in live_keys:
                continue
            presence = self._presence.pop(key)
            zone = self.zones.get(zone_id)
            if zone is None or presence.entered_at is None:
                continue
            last_seen = presence.last_seen or timestamp
            observations.append(
                ZoneObservation(
                    kind=ZoneEventKind.EXITED,
                    zone_id=zone.zone_id,
                    zone_name=zone.name,
                    zone_kind=zone.kind,
                    track_key=track_key,
                    track_id=int(track_key.rsplit(":", 1)[-1] or 0),
                    class_name="",
                    confidence=0.0,
                    timestamp=last_seen,
                    camera_id=self.camera_id,
                    dwell_seconds=(last_seen - presence.entered_at).total_seconds(),
                )
            )
        return observations

    # ------------------------------------------------------------- accessors
    def occupancy(self) -> dict[str, ZoneOccupancy]:
        """Current confirmed occupancy per zone."""
        result = {
            zone.zone_id: ZoneOccupancy(zone_id=zone.zone_id, zone_name=zone.name)
            for zone in self.zones.values()
        }
        for (zone_id, track_key), presence in self._presence.items():
            if presence.entered_at is not None and zone_id in result:
                result[zone_id].track_keys.add(track_key)
        return result

    def is_inside(self, zone_id: str, track_key: str) -> bool:
        presence = self._presence.get((zone_id, track_key))
        return presence is not None and presence.entered_at is not None

    def entered_at(self, zone_id: str, track_key: str) -> datetime | None:
        presence = self._presence.get((zone_id, track_key))
        return presence.entered_at if presence else None

    def dwell_seconds(self, zone_id: str, track_key: str, now: datetime) -> float | None:
        """How long a track has been inside, or ``None`` if it is not."""
        entered = self.entered_at(zone_id, track_key)
        if entered is None:
            return None
        return (now - entered).total_seconds()

    def prune(self, now: datetime, max_idle_seconds: float = 300.0) -> int:
        """Drop presence state for tracks not seen recently. Returns how many."""
        cutoff = now - timedelta(seconds=max_idle_seconds)
        stale = [
            key
            for key, presence in self._presence.items()
            if presence.last_seen is not None and presence.last_seen < cutoff
        ]
        for key in stale:
            del self._presence[key]
        return len(stale)
