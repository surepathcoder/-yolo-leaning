"""Vision engine -- zone geometry and occupancy.

A wrong point-in-polygon result sends a guard to the wrong aisle, so the
geometry is tested exhaustively, including the cases installers actually
create: concave shapes, points exactly on a boundary, and self-touching
outlines.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from vision.types import BoundingBox, ReferencePoint, TrackedObject
from vision.zones import (
    GeometryError,
    ZoneDefinition,
    ZoneEngine,
    ZoneEventKind,
    ZoneKind,
    bbox_polygon_overlap_ratio,
    distance_point_to_segment,
    normalise_line,
    normalise_polygon,
    point_in_polygon,
    points_in_polygon,
    polygon_area,
    polygon_centroid,
    segments_intersect,
    side_of_line,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

SQUARE = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
#: An L-shape. Installers draw these constantly -- around a counter, or an
#: aisle that turns a corner.
L_SHAPE = [(0.0, 0.0), (0.6, 0.0), (0.6, 0.3), (1.0, 0.3), (1.0, 1.0), (0.0, 1.0)]


def track(x: float, y: float, *, track_id: int = 1, class_name: str = "person") -> TrackedObject:
    """A track whose bottom-centre reference point is exactly ``(x, y)``."""
    return TrackedObject(
        track_id=track_id,
        class_id=0,
        class_name=class_name,
        camera_id="cam1",
        session_id="s1",
        bbox=BoundingBox(x - 0.03, y - 0.2, x + 0.03, y),
        confidence=0.9,
        first_seen=BASE,
        last_seen=BASE,
    )


# ------------------------------------------------------------ normalisation
def test_normalise_polygon_accepts_lists_and_tuples():
    assert normalise_polygon([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]) == [
        (0.0, 0.0),
        (1.0, 0.0),
        (1.0, 1.0),
    ]


def test_normalise_polygon_drops_a_repeated_closing_point():
    polygon = normalise_polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    assert len(polygon) == 4


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        [(0, 0)],
        [(0, 0), (1, 1)],
        [(0, 0), (0.5, 0.5), (1, 1)],  # collinear -> zero area
    ],
)
def test_normalise_polygon_rejects_degenerate_shapes(bad):
    """A zone that is really a line matches nothing; failing loudly at
    configuration time beats silently never firing."""
    with pytest.raises(GeometryError):
        normalise_polygon(bad)


def test_normalise_polygon_rejects_non_finite_coordinates():
    with pytest.raises(GeometryError):
        normalise_polygon([(0, 0), (1, 0), (float("nan"), 1)])


def test_normalise_line_rejects_identical_endpoints():
    with pytest.raises(GeometryError):
        normalise_line((0.5, 0.5), (0.5, 0.5))


# -------------------------------------------------------------------- area
def test_polygon_area_of_a_unit_square():
    assert polygon_area([(0, 0), (1, 0), (1, 1), (0, 1)]) == pytest.approx(1.0)


def test_polygon_area_is_winding_independent():
    clockwise = [(0, 0), (0, 1), (1, 1), (1, 0)]
    anticlockwise = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert polygon_area(clockwise) == pytest.approx(polygon_area(anticlockwise))


def test_polygon_centroid_of_a_square():
    assert polygon_centroid([(0, 0), (1, 0), (1, 1), (0, 1)]) == pytest.approx((0.5, 0.5))


# ------------------------------------------------------- point in polygon
@pytest.mark.parametrize(
    "point,expected",
    [
        ((0.5, 0.5), True),
        ((0.21, 0.21), True),
        ((0.1, 0.5), False),
        ((0.9, 0.5), False),
        ((0.5, 0.1), False),
        ((0.5, 0.9), False),
    ],
)
def test_point_in_square(point, expected):
    assert point_in_polygon(point, SQUARE) is expected


def test_boundary_points_count_as_inside():
    """Inclusive boundaries stop a person standing on the line from
    flickering in and out with every frame of detector jitter."""
    for point in [(0.2, 0.5), (0.8, 0.5), (0.5, 0.2), (0.5, 0.8), (0.2, 0.2)]:
        assert point_in_polygon(point, SQUARE) is True


@pytest.mark.parametrize(
    "point,expected",
    [
        ((0.3, 0.1), True),  # in the wide lower-left arm
        ((0.8, 0.1), False),  # in the notch -- outside
        ((0.8, 0.5), True),  # in the upper part
        ((0.3, 0.5), True),
        ((0.61, 0.1), False),
    ],
)
def test_point_in_concave_polygon(point, expected):
    """A convex-only test would report people inside zones they are not in."""
    assert point_in_polygon(point, L_SHAPE) is expected


def test_point_in_polygon_handles_a_star():
    """Self-intersecting outlines are drawn by accident; do not crash."""
    star = [
        (0.5, 0.0),
        (0.61, 0.35),
        (0.98, 0.35),
        (0.68, 0.57),
        (0.79, 0.91),
        (0.5, 0.70),
        (0.21, 0.91),
        (0.32, 0.57),
        (0.02, 0.35),
        (0.39, 0.35),
    ]
    assert point_in_polygon((0.5, 0.4), star) is True
    assert point_in_polygon((0.05, 0.95), star) is False


def test_vectorised_matches_scalar_point_in_polygon():
    rng = np.random.default_rng(42)
    points = rng.random((500, 2))
    vector = points_in_polygon(points, L_SHAPE)
    scalar = np.array([point_in_polygon(tuple(p), L_SHAPE) for p in points])
    assert (vector == scalar).all()


def test_points_in_polygon_handles_empty_input():
    assert points_in_polygon(np.zeros((0, 2)), SQUARE).size == 0


# --------------------------------------------------------------- line maths
def test_side_of_line_signs_are_opposite():
    a, b = (0.5, 0.0), (0.5, 1.0)
    assert side_of_line((0.2, 0.5), a, b) * side_of_line((0.8, 0.5), a, b) < 0


def test_point_on_the_line_is_zero():
    assert side_of_line((0.5, 0.5), (0.5, 0.0), (0.5, 1.0)) == pytest.approx(0.0)


def test_segments_intersect_when_crossing():
    assert segments_intersect((0.0, 0.5), (1.0, 0.5), (0.5, 0.0), (0.5, 1.0)) is True


def test_segments_do_not_intersect_when_apart():
    assert segments_intersect((0.0, 0.1), (0.4, 0.1), (0.5, 0.0), (0.5, 1.0)) is False


def test_segments_are_bounded_not_infinite_lines():
    """Walking past the end of a doorway line must not count as entering."""
    # The movement is at y = 0.9; the line spans only y in [0.0, 0.5].
    assert segments_intersect((0.3, 0.9), (0.7, 0.9), (0.5, 0.0), (0.5, 0.5)) is False


def test_touching_endpoint_counts_as_intersecting():
    assert segments_intersect((0.0, 0.5), (0.5, 0.5), (0.5, 0.0), (0.5, 1.0)) is True


def test_distance_point_to_segment_is_bounded():
    """Distance to a segment, not to the infinite line through it."""
    assert distance_point_to_segment((0.5, 0.5), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(0.5)
    # Beyond the end: the nearest point is the endpoint.
    assert distance_point_to_segment((2.0, 0.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)


def test_bbox_polygon_overlap_ratio():
    assert bbox_polygon_overlap_ratio((0.3, 0.3, 0.7, 0.7), SQUARE) == pytest.approx(1.0)
    assert bbox_polygon_overlap_ratio((0.85, 0.85, 0.95, 0.95), SQUARE) == pytest.approx(0.0)
    half = bbox_polygon_overlap_ratio((0.0, 0.3, 0.4, 0.7), SQUARE)
    assert 0.3 < half < 0.7


def test_overlap_ratio_is_deterministic():
    """Shelf occupancy is compared against thresholds over time -- a value
    that jitters between identical frames would create phantom stock events."""
    first = bbox_polygon_overlap_ratio((0.1, 0.1, 0.6, 0.6), L_SHAPE)
    second = bbox_polygon_overlap_ratio((0.1, 0.1, 0.6, 0.6), L_SHAPE)
    assert first == second


# -------------------------------------------------------------- zone engine
def make_zone(**overrides) -> ZoneDefinition:
    defaults: dict = {
        "zone_id": "z1",
        "name": "Warehouse",
        "polygon": SQUARE,
        "kind": ZoneKind.RESTRICTED,
        "entry_debounce_seconds": 0.0,
        "exit_debounce_seconds": 0.0,
    }
    defaults.update(overrides)
    return ZoneDefinition(**defaults)


def test_entry_and_exit_are_reported_once_each():
    engine = ZoneEngine([make_zone()], camera_id="cam1")

    outside = engine.update([track(0.05, 0.5)], BASE)
    assert outside == []

    entered = engine.update([track(0.5, 0.5)], BASE + timedelta(seconds=1))
    assert len(entered) == 1
    assert entered[0].kind == ZoneEventKind.ENTERED

    still_inside = engine.update([track(0.55, 0.5)], BASE + timedelta(seconds=2))
    assert still_inside == []

    exited = engine.update([track(0.95, 0.5)], BASE + timedelta(seconds=3))
    assert len(exited) == 1
    assert exited[0].kind == ZoneEventKind.EXITED
    assert exited[0].dwell_seconds == pytest.approx(2.0)


def test_entry_debounce_suppresses_boundary_flicker():
    """A person on the boundary must not generate dozens of events a minute."""
    engine = ZoneEngine(
        [make_zone(entry_debounce_seconds=1.0, exit_debounce_seconds=1.0)], camera_id="cam1"
    )
    events = []
    # Alternate in/out every 0.2 s for 2 seconds -- pure jitter.
    for step in range(10):
        x = 0.5 if step % 2 == 0 else 0.05
        events += engine.update([track(x, 0.5)], BASE + timedelta(seconds=step * 0.2))
    assert events == []


def test_entry_is_confirmed_after_the_debounce_window():
    engine = ZoneEngine([make_zone(entry_debounce_seconds=1.0)], camera_id="cam1")
    assert engine.update([track(0.5, 0.5)], BASE) == []
    assert engine.update([track(0.5, 0.5)], BASE + timedelta(seconds=0.5)) == []
    events = engine.update([track(0.5, 0.5)], BASE + timedelta(seconds=1.1))
    assert len(events) == 1
    # The reported time is when they entered, not when we became sure.
    assert events[0].timestamp == BASE


def test_exit_debounce_bridges_a_brief_occlusion():
    engine = ZoneEngine(
        [make_zone(entry_debounce_seconds=0.0, exit_debounce_seconds=2.0)], camera_id="cam1"
    )
    engine.update([track(0.5, 0.5)], BASE)
    engine.update([track(0.5, 0.5)], BASE + timedelta(seconds=1))
    # Briefly appears outside, then back in: not an exit.
    engine.update([track(0.05, 0.5)], BASE + timedelta(seconds=2))
    events = engine.update([track(0.5, 0.5)], BASE + timedelta(seconds=2.5))
    assert events == []
    assert engine.is_inside("z1", "cam1:s1:1") is True


def test_a_track_that_vanishes_inside_a_zone_still_exits():
    """Otherwise occupancy drifts upward all day."""
    engine = ZoneEngine([make_zone()], camera_id="cam1")
    engine.update([track(0.5, 0.5)], BASE)
    assert engine.occupancy()["z1"].count == 1

    events = engine.update([], BASE + timedelta(seconds=5))
    assert len(events) == 1
    assert events[0].kind == ZoneEventKind.EXITED
    assert engine.occupancy()["z1"].count == 0


def test_zone_only_reacts_to_configured_classes():
    engine = ZoneEngine([make_zone(trigger_classes=frozenset({"person"}))], camera_id="cam1")
    assert engine.update([track(0.5, 0.5, class_name="car")], BASE) == []
    assert len(engine.update([track(0.5, 0.5, class_name="person")], BASE)) == 1


def test_occupancy_counts_multiple_tracks():
    engine = ZoneEngine([make_zone()], camera_id="cam1")
    engine.update(
        [track(0.4, 0.5, track_id=1), track(0.6, 0.5, track_id=2), track(0.05, 0.5, track_id=3)],
        BASE,
    )
    assert engine.occupancy()["z1"].count == 2


def test_dwell_seconds_reports_live_duration():
    engine = ZoneEngine([make_zone()], camera_id="cam1")
    engine.update([track(0.5, 0.5)], BASE)
    dwell = engine.dwell_seconds("z1", "cam1:s1:1", BASE + timedelta(seconds=84))
    assert dwell == pytest.approx(84.0)
    assert engine.dwell_seconds("z1", "cam1:s1:999", BASE) is None


def test_reference_point_choice_changes_the_result():
    """A person leaning over a boundary has not entered it."""
    zone_feet = make_zone(reference_point=ReferencePoint.BOTTOM_CENTER)
    zone_body = make_zone(zone_id="z2", reference_point=ReferencePoint.CENTER)
    engine = ZoneEngine([zone_feet, zone_body], camera_id="cam1")

    # Feet at y = 0.85 (below the zone); centre at y = 0.75 (inside it).
    leaning = TrackedObject(
        track_id=1,
        class_id=0,
        class_name="person",
        camera_id="cam1",
        session_id="s1",
        bbox=BoundingBox(0.45, 0.65, 0.55, 0.85),
        confidence=0.9,
        first_seen=BASE,
        last_seen=BASE,
    )
    events = engine.update([leaning], BASE)
    zones_entered = {e.zone_id for e in events}
    assert "z2" in zones_entered
    assert "z1" not in zones_entered


def test_multiple_zones_are_evaluated_independently():
    left = make_zone(zone_id="left", name="Left", polygon=[(0, 0), (0.5, 0), (0.5, 1), (0, 1)])
    right = make_zone(zone_id="right", name="Right", polygon=[(0.5, 0), (1, 0), (1, 1), (0.5, 1)])
    engine = ZoneEngine([left, right], camera_id="cam1")
    events = engine.update([track(0.25, 0.5)], BASE)
    assert len(events) == 1
    assert events[0].zone_id == "left"


def test_set_zones_discards_state_for_removed_zones():
    engine = ZoneEngine([make_zone()], camera_id="cam1")
    engine.update([track(0.5, 0.5)], BASE)
    assert engine.is_inside("z1", "cam1:s1:1") is True

    engine.set_zones([make_zone(zone_id="z9", name="Other")])
    assert engine.is_inside("z1", "cam1:s1:1") is False


def test_inactive_zones_are_ignored():
    engine = ZoneEngine([make_zone(is_active=False)], camera_id="cam1")
    assert engine.update([track(0.5, 0.5)], BASE) == []


def test_prune_drops_stale_presence_state():
    engine = ZoneEngine([make_zone()], camera_id="cam1")
    engine.update([track(0.05, 0.5)], BASE)
    removed = engine.prune(BASE + timedelta(seconds=600), max_idle_seconds=300)
    assert removed == 1


def test_observation_serialises_cleanly():
    engine = ZoneEngine([make_zone()], camera_id="cam1")
    events = engine.update([track(0.5, 0.5)], BASE)
    payload = events[0].to_dict()
    assert payload["kind"] == "ENTERED"
    assert payload["zone_name"] == "Warehouse"
    assert payload["track_id"] == 1
    assert payload["dwell_seconds"] is None
