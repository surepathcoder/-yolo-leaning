"""Event rules.

A rule reads observations and decides whether something worth reporting has
happened. Rules never look at pixels — that already happened — and never
persist anything.

The separation from detection is the point of the architecture:

    Detection answers   "what do we see?"
    Rules answer        "what does that mean, given how this store is configured?"

Every rule below is deliberately conservative about repeating itself. A person
loitering for ten minutes should produce one event, not six hundred; a
dashboard that cries wolf is one that gets ignored.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from vision.counting import CrossingObservation, Direction
from vision.events.schedule import OperatingSchedule
from vision.events.types import EventType, Severity, VisionEvent
from vision.types import TrackedObject
from vision.zones import ZoneDefinition, ZoneEventKind, ZoneKind, ZoneObservation


@dataclass
class RuleContext:
    """Everything a rule needs about the world, gathered once per frame."""

    camera_id: str
    store_id: str | None
    now: datetime
    tracks: list[TrackedObject] = field(default_factory=list)
    zone_observations: list[ZoneObservation] = field(default_factory=list)
    crossings: list[CrossingObservation] = field(default_factory=list)
    zones: dict[str, ZoneDefinition] = field(default_factory=dict)
    #: Live dwell time per ``(zone_id, track_key)``, from the zone engine.
    dwell_seconds: dict[tuple[str, str], float] = field(default_factory=dict)
    zone_occupancy: dict[str, int] = field(default_factory=dict)
    schedule: OperatingSchedule | None = None

    @property
    def is_after_hours(self) -> bool:
        """Whether the store is closed right now.

        With no schedule configured this is ``False``: an unconfigured store
        should not have every daytime detection flagged as an intrusion. The
        *schedule itself* fails safe by treating unconfigured days as closed;
        having no schedule at all is a different situation, and is handled by
        not running the rule.
        """
        return self.schedule is not None and self.schedule.is_after_hours(self.now)


class Rule(ABC):
    """A named rule that turns observations into events."""

    name: str = "rule"

    @abstractmethod
    def evaluate(self, context: RuleContext) -> list[VisionEvent]:
        """Return any events this rule fires for the current frame."""

    def reset(self) -> None:  # noqa: B027 - optional hook; stateless rules need none
        """Drop per-run state. Called when a pipeline restarts."""


# ---------------------------------------------------------------------------
# Zone entry / exit
# ---------------------------------------------------------------------------


class ZoneTransitionRule(Rule):
    """Reports confirmed zone entries and exits.

    Severity depends on the zone's purpose, not on the movement: walking into
    an entrance zone is routine (``INFO``); walking into a restricted
    warehouse is not (``HIGH``).
    """

    name = "zone.transition"

    #: Base severity per zone kind, during opening hours.
    SEVERITY_BY_KIND = {
        ZoneKind.RESTRICTED: Severity.HIGH,
        ZoneKind.WAREHOUSE: Severity.MEDIUM,
        ZoneKind.CHECKOUT: Severity.INFO,
        ZoneKind.ENTRANCE: Severity.INFO,
        ZoneKind.EXIT: Severity.INFO,
        ZoneKind.SHELF: Severity.INFO,
        ZoneKind.CUSTOM: Severity.LOW,
    }

    def evaluate(self, context: RuleContext) -> list[VisionEvent]:
        events: list[VisionEvent] = []
        for observation in context.zone_observations:
            is_person = observation.class_name == "person"
            entering = observation.kind == ZoneEventKind.ENTERED

            if entering and observation.zone_kind == ZoneKind.RESTRICTED:
                event_type = EventType.RESTRICTED_AREA_ENTRY
            elif entering:
                event_type = (
                    EventType.PERSON_ENTERED_ZONE if is_person else EventType.OBJECT_ENTERED_ZONE
                )
            else:
                event_type = (
                    EventType.PERSON_EXITED_ZONE if is_person else EventType.OBJECT_EXITED_ZONE
                )

            severity = (
                self.SEVERITY_BY_KIND.get(observation.zone_kind, Severity.LOW)
                if entering
                else Severity.INFO
            )

            subject = observation.class_name or "Object"
            if entering:
                summary = (
                    f"{subject.capitalize()} (track {observation.track_id}) entered "
                    f"zone '{observation.zone_name}'."
                )
            else:
                dwell = observation.dwell_seconds or 0.0
                summary = (
                    f"{subject.capitalize()} (track {observation.track_id}) left "
                    f"zone '{observation.zone_name}' after {dwell:.0f} seconds."
                )

            events.append(
                VisionEvent(
                    event_type=event_type,
                    severity=severity,
                    camera_id=context.camera_id,
                    store_id=context.store_id,
                    zone_id=observation.zone_id,
                    occurred_at=observation.timestamp,
                    track_key=observation.track_key,
                    track_id=observation.track_id,
                    object_class=observation.class_name,
                    confidence=observation.confidence,
                    duration_seconds=observation.dwell_seconds,
                    rule_name=self.name,
                    summary=summary,
                    metadata={
                        "zone_name": observation.zone_name,
                        "zone_kind": observation.zone_kind.value,
                    },
                )
            )
        return events


# ---------------------------------------------------------------------------
# Line crossing
# ---------------------------------------------------------------------------


class LineCrossingRule(Rule):
    """Reports directional line crossings.

    Always ``INFO``: a person walking through a door is the most ordinary
    thing a shop camera sees. These events exist to be counted, not to alarm.
    """

    name = "counting.line_crossed"

    def evaluate(self, context: RuleContext) -> list[VisionEvent]:
        return [
            VisionEvent(
                event_type=EventType.LINE_CROSSED,
                severity=Severity.INFO,
                camera_id=context.camera_id,
                store_id=context.store_id,
                line_id=crossing.line_id,
                occurred_at=crossing.timestamp,
                track_key=crossing.track_key,
                track_id=crossing.track_id,
                object_class=crossing.class_name,
                confidence=crossing.confidence,
                rule_name=self.name,
                summary=(
                    f"{crossing.class_name.capitalize()} (track {crossing.track_id}) crossed "
                    f"'{crossing.line_name}' "
                    f"({'in' if crossing.direction == Direction.IN else 'out'})."
                ),
                metadata={
                    "line_name": crossing.line_name,
                    "direction": crossing.direction.value,
                },
            )
            for crossing in context.crossings
        ]


# ---------------------------------------------------------------------------
# Loitering
# ---------------------------------------------------------------------------


class LoiteringRule(Rule):
    """Fires when an object stays in a zone beyond a threshold.

    The event type is ``LOITERING`` and the summary is factual: "remained in
    zone X for N seconds". It does not say "suspicious". Someone standing
    still for two minutes might be a shoplifter, a customer comparing prices,
    a parent waiting for a child, or a member of staff. The system reports the
    duration and lets a person judge.

    Fires **once per track per zone** per visit, plus optional repeats at a
    configured interval for a genuinely prolonged stay. Without that, a ten
    minute stay at 10 FPS would produce six thousand events.
    """

    name = "behaviour.loitering"

    def __init__(
        self,
        default_threshold_seconds: float = 60.0,
        *,
        repeat_interval_seconds: float | None = 300.0,
        severity: Severity = Severity.MEDIUM,
    ) -> None:
        self.default_threshold_seconds = default_threshold_seconds
        self.repeat_interval_seconds = repeat_interval_seconds
        self.severity = severity
        #: ``(zone_id, track_key)`` -> when we last reported it.
        self._reported: dict[tuple[str, str], datetime] = {}

    def reset(self) -> None:
        self._reported.clear()

    def _threshold_for(self, zone: ZoneDefinition | None) -> float:
        if zone is not None and zone.loitering_threshold_seconds is not None:
            return float(zone.loitering_threshold_seconds)
        return self.default_threshold_seconds

    def evaluate(self, context: RuleContext) -> list[VisionEvent]:
        events: list[VisionEvent] = []
        tracks_by_key = {track.track_key: track for track in context.tracks}

        for (zone_id, track_key), dwell in context.dwell_seconds.items():
            zone = context.zones.get(zone_id)
            threshold = self._threshold_for(zone)
            if dwell < threshold:
                continue

            key = (zone_id, track_key)
            last_reported = self._reported.get(key)
            if last_reported is not None:
                if self.repeat_interval_seconds is None:
                    continue
                elapsed = (context.now - last_reported).total_seconds()
                if elapsed < self.repeat_interval_seconds:
                    continue

            track = tracks_by_key.get(track_key)
            self._reported[key] = context.now
            events.append(
                VisionEvent(
                    event_type=EventType.LOITERING,
                    severity=self.severity,
                    camera_id=context.camera_id,
                    store_id=context.store_id,
                    zone_id=zone_id,
                    occurred_at=context.now,
                    track_key=track_key,
                    track_id=track.track_id if track else None,
                    object_class=track.class_name if track else None,
                    confidence=track.confidence if track else None,
                    duration_seconds=dwell,
                    rule_name=self.name,
                    # Neutral wording: a duration, not a judgement.
                    summary=(
                        f"{(track.class_name if track else 'Object').capitalize()} "
                        f"(track {track.track_id if track else '?'}) has remained in zone "
                        f"'{zone.name if zone else zone_id}' for {dwell:.0f} seconds "
                        f"(threshold {threshold:.0f}s)."
                    ),
                    metadata={
                        "zone_name": zone.name if zone else zone_id,
                        "threshold_seconds": threshold,
                        "dwell_seconds": round(dwell, 1),
                        "repeat": last_reported is not None,
                    },
                )
            )

        self._forget_departed(context)
        return events

    def _forget_departed(self, context: RuleContext) -> None:
        """Clear state for tracks that have left, so a return re-triggers."""
        live = set(context.dwell_seconds.keys())
        for key in list(self._reported):
            if key not in live:
                del self._reported[key]


class DwellThresholdRule(LoiteringRule):
    """Same mechanics as loitering, for zones with a business dwell target.

    Separate from loitering because the meaning differs: a long dwell at a
    checkout is a queueing problem, not a security one, and conflating them
    would put queue data in the security operator's alert list.
    """

    name = "behaviour.dwell_threshold"

    def __init__(self, default_threshold_seconds: float = 120.0) -> None:
        super().__init__(
            default_threshold_seconds,
            repeat_interval_seconds=None,
            severity=Severity.INFO,
        )

    def _threshold_for(self, zone: ZoneDefinition | None) -> float:
        if zone is not None and zone.dwell_threshold_seconds is not None:
            return float(zone.dwell_threshold_seconds)
        return self.default_threshold_seconds

    def evaluate(self, context: RuleContext) -> list[VisionEvent]:
        # Only zones that explicitly configure a dwell target participate.
        filtered = RuleContext(
            camera_id=context.camera_id,
            store_id=context.store_id,
            now=context.now,
            tracks=context.tracks,
            zones=context.zones,
            dwell_seconds={
                key: value
                for key, value in context.dwell_seconds.items()
                if (zone := context.zones.get(key[0])) is not None
                and zone.dwell_threshold_seconds is not None
            },
            schedule=context.schedule,
        )
        events = super().evaluate(filtered)
        for event in events:
            event.event_type = EventType.DWELL_THRESHOLD_EXCEEDED
            event.rule_name = self.name
        return events


# ---------------------------------------------------------------------------
# After hours
# ---------------------------------------------------------------------------


class AfterHoursRule(Rule):
    """Fires when a person is present in a monitored zone while closed.

    Only zones with ``after_hours_monitoring`` participate. A shop's own front
    window will see people on the street all night; alerting on those would
    make the feature useless within a day.

    Severity is ``HIGH`` rather than ``CRITICAL``: a cleaner, a stock delivery
    or the owner fetching something are all common, and the system does not
    know which. The event says a person was present outside opening hours,
    which is exactly what it observed.
    """

    name = "schedule.after_hours"

    def __init__(
        self,
        *,
        cooldown_seconds: float = 120.0,
        severity: Severity = Severity.HIGH,
        classes: frozenset[str] = frozenset({"person"}),
    ) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.severity = severity
        self.classes = classes
        self._reported: dict[tuple[str, str], datetime] = {}

    def reset(self) -> None:
        self._reported.clear()

    def evaluate(self, context: RuleContext) -> list[VisionEvent]:
        if context.schedule is None or not context.is_after_hours:
            return []

        events: list[VisionEvent] = []
        monitored = {
            zone_id for zone_id, zone in context.zones.items() if zone.after_hours_monitoring
        }
        if not monitored:
            return []

        tracks_by_key = {track.track_key: track for track in context.tracks}

        for zone_id, track_key in context.dwell_seconds:
            if zone_id not in monitored:
                continue
            track = tracks_by_key.get(track_key)
            if track is None or track.class_name not in self.classes:
                continue

            key = (zone_id, track_key)
            last = self._reported.get(key)
            if last is not None and (context.now - last).total_seconds() < self.cooldown_seconds:
                continue
            self._reported[key] = context.now

            zone = context.zones.get(zone_id)
            local = context.schedule.local_time(context.now)
            events.append(
                VisionEvent(
                    event_type=EventType.AFTER_HOURS_ACTIVITY,
                    severity=self.severity,
                    camera_id=context.camera_id,
                    store_id=context.store_id,
                    zone_id=zone_id,
                    occurred_at=context.now,
                    track_key=track_key,
                    track_id=track.track_id,
                    object_class=track.class_name,
                    confidence=track.confidence,
                    rule_name=self.name,
                    summary=(
                        f"Person (track {track.track_id}) detected in zone "
                        f"'{zone.name if zone else zone_id}' at "
                        f"{local:%H:%M} local time, outside opening hours."
                    ),
                    metadata={
                        "zone_name": zone.name if zone else zone_id,
                        "local_time": local.isoformat(),
                        "timezone": context.schedule.timezone_name,
                    },
                )
            )

        for key in list(self._reported):
            if key not in context.dwell_seconds:
                del self._reported[key]
        return events


# ---------------------------------------------------------------------------
# Occupancy
# ---------------------------------------------------------------------------


class OccupancyRule(Rule):
    """Fires when a zone holds more objects than its configured maximum.

    Useful for crowding and for queue management, and for spotting a group
    entering a restricted area together.
    """

    name = "zone.occupancy"

    def __init__(self, *, cooldown_seconds: float = 60.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._reported: dict[str, datetime] = {}

    def reset(self) -> None:
        self._reported.clear()

    def evaluate(self, context: RuleContext) -> list[VisionEvent]:
        events: list[VisionEvent] = []
        for zone_id, count in context.zone_occupancy.items():
            zone = context.zones.get(zone_id)
            if zone is None or zone.max_occupancy is None:
                continue
            if count <= zone.max_occupancy:
                self._reported.pop(zone_id, None)
                continue

            last = self._reported.get(zone_id)
            if last is not None and (context.now - last).total_seconds() < self.cooldown_seconds:
                continue
            self._reported[zone_id] = context.now

            events.append(
                VisionEvent(
                    event_type=EventType.OCCUPANCY_THRESHOLD_EXCEEDED,
                    severity=Severity.MEDIUM,
                    camera_id=context.camera_id,
                    store_id=context.store_id,
                    zone_id=zone_id,
                    occurred_at=context.now,
                    rule_name=self.name,
                    summary=(
                        f"Zone '{zone.name}' holds {count} objects, above its "
                        f"configured maximum of {zone.max_occupancy}."
                    ),
                    metadata={
                        "zone_name": zone.name,
                        "occupancy": count,
                        "max_occupancy": zone.max_occupancy,
                    },
                )
            )
        return events


def default_rules(
    *,
    loitering_threshold_seconds: float = 60.0,
    dwell_threshold_seconds: float = 120.0,
) -> list[Rule]:
    """The rule set every camera pipeline runs unless configured otherwise."""
    return [
        ZoneTransitionRule(),
        LineCrossingRule(),
        LoiteringRule(loitering_threshold_seconds),
        DwellThresholdRule(dwell_threshold_seconds),
        AfterHoursRule(),
        OccupancyRule(),
    ]
