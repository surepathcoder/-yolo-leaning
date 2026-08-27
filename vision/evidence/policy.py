"""Which events are worth keeping a picture of.

This mirrors the backend's alert policy without importing it. ``vision/`` must
stay runnable with no database and no web framework, so the two live in
different layers; a parametrised test
(``test_vision_evidence.py::test_should_capture_matches_should_alert``) asserts
they have not drifted apart, exactly as ``INFERENCE_EVENT_TYPES`` is mirrored
between ``vision/events/types.py`` and ``backend/app/models/enums.py``.

The rule: capture evidence for anything that would raise an alert. An alert
needs severity at least MEDIUM (which already includes HIGH and CRITICAL) and
an event type that is not high-volume noise. Line crossings are the classic
exclusion -- a busy shop produces thousands a day and every one is a person
walking through a door.
"""

from __future__ import annotations

from vision.events.types import SEVERITY_ORDER, EventType, Severity, VisionEvent

#: Minimum severity for an event to be worth an artefact. Mirrors
#: ``backend/app/services/alerts.py::ALERT_MIN_SEVERITY``.
EVIDENCE_MIN_SEVERITY: Severity = Severity.MEDIUM

#: Event types that never warrant evidence, however severe the rule thinks
#: they are. Mirrors ``alerts.py::NON_ALERTING_EVENT_TYPES``.
NON_CAPTURING_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.LINE_CROSSED,
        EventType.PERSON_DETECTED,
        EventType.OBJECT_DETECTED,
        EventType.PERSON_EXITED_ZONE,
        EventType.OBJECT_EXITED_ZONE,
    }
)

#: The event types evidence capture applies to, for callers that want the set
#: rather than the predicate.
EVIDENCE_EVENT_TYPES: frozenset[EventType] = frozenset(
    event_type for event_type in EventType if event_type not in NON_CAPTURING_EVENT_TYPES
)


def should_capture(event: VisionEvent) -> bool:
    """Whether this event warrants a snapshot and clip."""
    if event.event_type in NON_CAPTURING_EVENT_TYPES:
        return False
    return SEVERITY_ORDER[event.severity] >= SEVERITY_ORDER[EVIDENCE_MIN_SEVERITY]
