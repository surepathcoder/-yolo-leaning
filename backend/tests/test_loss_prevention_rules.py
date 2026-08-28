"""Milestone 18 -- exit without a recorded checkout.

Pure zone geometry: no product model, no per-shop training. ``vision/`` has
no database dependency, so these tests build ``RuleContext``/``ZoneObservation``
directly rather than going through the API -- the same style the package's own
design intends for its rules.
"""

from __future__ import annotations

from datetime import UTC, datetime

from vision.events.rules import ExitWithoutCheckoutRule, RuleContext
from vision.events.types import EventType
from vision.types import BoundingBox, TrackedObject
from vision.zones import ZoneDefinition, ZoneEventKind, ZoneKind, ZoneObservation

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

FORBIDDEN_WORDS = (
    "theft",
    "thief",
    "steal",
    "stolen",
    "shoplift",
    "criminal",
    "guilty",
    "suspect",
    "culprit",
    "dishonest",
)


def _track(track_id: int = 1, *, class_name: str = "person") -> TrackedObject:
    return TrackedObject(
        track_id=track_id,
        class_id=0,
        class_name=class_name,
        camera_id="cam-1",
        session_id="session-1",
        bbox=BoundingBox(0.4, 0.4, 0.5, 0.6),
        confidence=0.9,
        first_seen=NOW,
        last_seen=NOW,
    )


#: The rule only ever reads `zone.kind` -- geometry is not exercised here --
#: but `ZoneDefinition` validates its polygon on construction, so this has to
#: be a real triangle.
_PLACEHOLDER_POLYGON = [(0.1, 0.1), (0.2, 0.1), (0.15, 0.2)]


def _zones(*, checkout: bool) -> dict[str, ZoneDefinition]:
    zones: dict[str, ZoneDefinition] = {
        "exit-1": ZoneDefinition(
            zone_id="exit-1", name="Front door", polygon=_PLACEHOLDER_POLYGON, kind=ZoneKind.EXIT
        ),
    }
    if checkout:
        zones["checkout-1"] = ZoneDefinition(
            zone_id="checkout-1",
            name="Till",
            polygon=_PLACEHOLDER_POLYGON,
            kind=ZoneKind.CHECKOUT,
        )
    return zones


def _observation(
    track: TrackedObject,
    *,
    zone_id: str,
    zone_name: str,
    zone_kind: ZoneKind,
    kind: ZoneEventKind,
    class_name: str = "person",
) -> ZoneObservation:
    return ZoneObservation(
        kind=kind,
        zone_id=zone_id,
        zone_name=zone_name,
        zone_kind=zone_kind,
        track_key=track.track_key,
        track_id=track.track_id,
        class_name=class_name,
        confidence=0.9,
        timestamp=NOW,
        camera_id="cam-1",
    )


def _context(track: TrackedObject, zones, observations) -> RuleContext:
    return RuleContext(
        camera_id="cam-1",
        store_id="store-1",
        now=NOW,
        tracks=[track],
        zones=zones,
        zone_observations=observations,
    )


def test_fires_when_exit_is_reached_with_no_checkout_visit():
    rule = ExitWithoutCheckoutRule()
    track = _track()
    context = _context(
        track,
        _zones(checkout=True),
        [
            _observation(
                track,
                zone_id="exit-1",
                zone_name="Front door",
                zone_kind=ZoneKind.EXIT,
                kind=ZoneEventKind.ENTERED,
            )
        ],
    )

    events = rule.evaluate(context)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == EventType.POSSIBLE_EXIT_WITHOUT_PAYMENT
    assert event.is_inference is True
    assert event.track_key == track.track_key
    assert event.zone_id == "exit-1"
    assert not any(word in event.summary.lower() for word in FORBIDDEN_WORDS)


def test_silent_when_checkout_was_visited_first():
    rule = ExitWithoutCheckoutRule()
    track = _track()
    zones = _zones(checkout=True)

    checkout_events = rule.evaluate(
        _context(
            track,
            zones,
            [
                _observation(
                    track,
                    zone_id="checkout-1",
                    zone_name="Till",
                    zone_kind=ZoneKind.CHECKOUT,
                    kind=ZoneEventKind.EXITED,
                )
            ],
        )
    )
    assert checkout_events == []

    exit_events = rule.evaluate(
        _context(
            track,
            zones,
            [
                _observation(
                    track,
                    zone_id="exit-1",
                    zone_name="Front door",
                    zone_kind=ZoneKind.EXIT,
                    kind=ZoneEventKind.ENTERED,
                )
            ],
        )
    )
    assert exit_events == []


def test_silent_with_no_checkout_zone_configured():
    """A store that has not drawn a checkout zone must not be punished for it."""
    rule = ExitWithoutCheckoutRule()
    track = _track()
    context = _context(
        track,
        _zones(checkout=False),
        [
            _observation(
                track,
                zone_id="exit-1",
                zone_name="Front door",
                zone_kind=ZoneKind.EXIT,
                kind=ZoneEventKind.ENTERED,
            )
        ],
    )

    assert rule.evaluate(context) == []


def test_ignores_non_person_classes():
    rule = ExitWithoutCheckoutRule()
    track = _track(class_name="backpack")
    context = _context(
        track,
        _zones(checkout=True),
        [
            _observation(
                track,
                zone_id="exit-1",
                zone_name="Front door",
                zone_kind=ZoneKind.EXIT,
                kind=ZoneEventKind.ENTERED,
                class_name="backpack",
            )
        ],
    )

    assert rule.evaluate(context) == []


def test_fires_once_per_track():
    rule = ExitWithoutCheckoutRule()
    track = _track()
    context = _context(
        track,
        _zones(checkout=True),
        [
            _observation(
                track,
                zone_id="exit-1",
                zone_name="Front door",
                zone_kind=ZoneKind.EXIT,
                kind=ZoneEventKind.ENTERED,
            )
        ],
    )

    assert len(rule.evaluate(context)) == 1
    # The same track re-evaluated against the same exit observation must not
    # raise a second alert for one departure.
    assert rule.evaluate(context) == []


def test_a_departed_track_is_forgotten_so_a_return_re_evaluates():
    rule = ExitWithoutCheckoutRule()
    track = _track()
    zones = _zones(checkout=True)
    checkout_observation = [
        _observation(
            track,
            zone_id="checkout-1",
            zone_name="Till",
            zone_kind=ZoneKind.CHECKOUT,
            kind=ZoneEventKind.EXITED,
        )
    ]
    rule.evaluate(_context(track, zones, checkout_observation))
    assert rule._checkout_seen

    # The track leaves the frame: an empty `tracks` list is what the pipeline
    # sends once a track is no longer active.
    empty_context = RuleContext(
        camera_id="cam-1", store_id="store-1", now=NOW, tracks=[], zones=zones
    )
    rule.evaluate(empty_context)
    assert rule._checkout_seen == {}


def test_reset_clears_state():
    rule = ExitWithoutCheckoutRule()
    track = _track()
    zones = _zones(checkout=True)
    rule.evaluate(
        _context(
            track,
            zones,
            [
                _observation(
                    track,
                    zone_id="checkout-1",
                    zone_name="Till",
                    zone_kind=ZoneKind.CHECKOUT,
                    kind=ZoneEventKind.EXITED,
                )
            ],
        )
    )
    assert rule._checkout_seen

    rule.reset()
    assert rule._checkout_seen == {}
