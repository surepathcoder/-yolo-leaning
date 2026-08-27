"""The event engine.

Every observation the vision engine makes passes through here on its way to
becoming something a person sees. The engine:

* runs the configured rules over one frame's observations;
* suppresses duplicates, so a stuck camera cannot flood the dashboard;
* hands finished events to registered sinks.

It deliberately does **not** persist anything or talk to the network. Sinks do
that, which is what lets the same engine run on an edge box that is offline
(queue sink) and in the cloud (database sink).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from vision.events.rules import Rule, RuleContext, default_rules
from vision.events.types import EventType, Severity, VisionEvent


class EventSink(ABC):
    """Somewhere finished events go."""

    @abstractmethod
    def emit(self, event: VisionEvent) -> None:
        """Handle one event. Must not raise on ordinary failure."""

    def flush(self) -> None:  # noqa: B027 - optional hook; most sinks buffer nothing
        """Push any buffered events. Called on shutdown."""


class CollectingSink(EventSink):
    """Keeps events in memory. For tests and short-lived tooling."""

    def __init__(self, max_events: int = 10_000) -> None:
        self.events: deque[VisionEvent] = deque(maxlen=max_events)

    def emit(self, event: VisionEvent) -> None:
        self.events.append(event)

    def clear(self) -> None:
        self.events.clear()

    def of_type(self, event_type: EventType) -> list[VisionEvent]:
        return [event for event in self.events if event.event_type == event_type]

    def __len__(self) -> int:
        return len(self.events)


class CallbackSink(EventSink):
    """Calls a function per event.

    Exceptions from the callback are swallowed on purpose: a failing sink --
    a dropped database connection, say -- must never stop the vision pipeline.
    Losing an event is bad; losing the camera is worse.
    """

    def __init__(self, callback: Callable[[VisionEvent], None], name: str = "callback") -> None:
        self.callback = callback
        self.name = name
        self.failures = 0
        self.last_error: str | None = None

    def emit(self, event: VisionEvent) -> None:
        try:
            self.callback(event)
        # A failing sink -- a dropped connection, say -- must never stop the
        # pipeline. Losing an event is bad; losing the camera is worse.
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]


@dataclass(slots=True)
class EngineStats:
    events_emitted: int = 0
    events_suppressed: int = 0
    rule_failures: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None

    def record(self, event: VisionEvent) -> None:
        self.events_emitted += 1
        self.by_type[event.event_type.value] = self.by_type.get(event.event_type.value, 0) + 1
        self.by_severity[event.severity.value] = self.by_severity.get(event.severity.value, 0) + 1

    def to_dict(self) -> dict:
        return {
            "events_emitted": self.events_emitted,
            "events_suppressed": self.events_suppressed,
            "rule_failures": self.rule_failures,
            "by_type": dict(self.by_type),
            "by_severity": dict(self.by_severity),
            "last_error": self.last_error,
        }


def dedupe_key(event: VisionEvent) -> str:
    """A stable identity for "the same thing happening again".

    Deliberately excludes the timestamp: two identical events a second apart
    are the same occurrence seen twice, which is exactly what needs
    suppressing. It includes the track, so two different people entering the
    same zone are two events.
    """
    parts = [
        event.event_type.value,
        event.camera_id,
        event.zone_id or "",
        event.line_id or "",
        event.shelf_id or "",
        event.track_key or "",
        event.metadata.get("direction", ""),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


class EventEngine:
    """Runs rules and dispatches the resulting events.

    ``suppression_window_seconds`` is the one piece of tuning that matters.
    Too short and a flickering detection floods the timeline; too long and a
    genuine second incident is silently dropped. The default of 5 seconds is
    below any human reaction time but well above frame-to-frame jitter.
    """

    def __init__(
        self,
        rules: list[Rule] | None = None,
        sinks: list[EventSink] | None = None,
        *,
        suppression_window_seconds: float = 5.0,
        min_severity: Severity = Severity.INFO,
    ) -> None:
        self.rules = rules if rules is not None else default_rules()
        self.sinks = sinks or []
        self.suppression_window_seconds = suppression_window_seconds
        self.min_severity = min_severity
        self.stats = EngineStats()
        self._recent: dict[str, datetime] = {}

    # ------------------------------------------------------------------ sinks
    def add_sink(self, sink: EventSink) -> None:
        self.sinks.append(sink)

    def remove_sink(self, sink: EventSink) -> None:
        if sink in self.sinks:
            self.sinks.remove(sink)

    # ------------------------------------------------------------------ rules
    def add_rule(self, rule: Rule) -> None:
        self.rules.append(rule)

    def reset(self) -> None:
        """Clear rule and suppression state, e.g. after a reconnect."""
        for rule in self.rules:
            rule.reset()
        self._recent.clear()

    # ----------------------------------------------------------------- process
    def process(self, context: RuleContext) -> list[VisionEvent]:
        """Run every rule over one frame and dispatch what they produce.

        A rule that raises is recorded and skipped rather than allowed to stop
        the others: one broken rule must not blind the whole camera.
        """
        produced: list[VisionEvent] = []

        for rule in self.rules:
            try:
                produced.extend(rule.evaluate(context))
            # One broken rule must not blind the whole camera.
            except Exception as exc:
                self.stats.rule_failures += 1
                self.stats.last_error = f"{rule.name}: {type(exc).__name__}: {exc}"[:200]

        return self.dispatch(produced, context.now)

    def dispatch(self, events: list[VisionEvent], now: datetime) -> list[VisionEvent]:
        """Filter, deduplicate and send events to the sinks."""
        accepted: list[VisionEvent] = []
        for event in events:
            if event.severity_rank < self.min_severity_rank:
                continue
            if self._is_duplicate(event, now):
                self.stats.events_suppressed += 1
                continue
            accepted.append(event)
            self.stats.record(event)
            for sink in self.sinks:
                sink.emit(event)

        self._prune(now)
        return accepted

    @property
    def min_severity_rank(self) -> int:
        from vision.events.types import SEVERITY_ORDER

        return SEVERITY_ORDER[self.min_severity]

    def _is_duplicate(self, event: VisionEvent, now: datetime) -> bool:
        if self.suppression_window_seconds <= 0:
            return False
        key = dedupe_key(event)
        last = self._recent.get(key)
        if last is not None and (now - last).total_seconds() < self.suppression_window_seconds:
            return True
        self._recent[key] = now
        return False

    def _prune(self, now: datetime) -> None:
        """Drop suppression entries that can no longer suppress anything."""
        if len(self._recent) < 500:
            return
        cutoff = now - timedelta(seconds=self.suppression_window_seconds * 4)
        self._recent = {key: seen for key, seen in self._recent.items() if seen >= cutoff}

    def flush(self) -> None:
        for sink in self.sinks:
            sink.flush()
