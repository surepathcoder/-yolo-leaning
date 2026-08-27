"""Vision engine -- line crossing and people counting.

The commercial requirement is simple to state and easy to get wrong: one
person walking through a door counts as exactly one. A counter that reports
400 visitors when 80 came through is worse than no counter, because managers
act on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vision.counting import (
    Direction,
    LineCounter,
    LineDefinition,
    OccupancyTracker,
)
from vision.tracking import ByteTracker
from vision.types import BoundingBox, Detection, TrackedObject
from vision.zones import GeometryError

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def at(step: int, fps: float = 10.0) -> datetime:
    return BASE + timedelta(seconds=step / fps)


def make_line(**overrides) -> LineDefinition:
    defaults: dict = {
        "line_id": "l1",
        "name": "Front door",
        "point_a": (0.5, 0.0),
        "point_b": (0.5, 1.0),
        "cooldown_seconds": 3.0,
    }
    defaults.update(overrides)
    return LineDefinition(**defaults)


def walk(
    counter: LineCounter,
    xs: list[float],
    *,
    track_id: int = 1,
    y: float = 0.8,
    fps: float = 10.0,
    class_name: str = "person",
) -> list:
    """Move one track through a sequence of x positions, collecting crossings.

    A real ``TrackedObject`` is used, with its trajectory built up by
    ``update``, because the crossing test reads the *previous* reference point
    from that trajectory.
    """
    track = TrackedObject(
        track_id=track_id,
        class_id=0,
        class_name=class_name,
        camera_id="cam1",
        session_id="s1",
        bbox=BoundingBox(xs[0] - 0.03, y - 0.4, xs[0] + 0.03, y),
        confidence=0.9,
        first_seen=BASE,
        last_seen=BASE,
    )
    observations = []
    for step, x in enumerate(xs):
        timestamp = at(step, fps)
        track.update(BoundingBox(x - 0.03, y - 0.4, x + 0.03, y), 0.9, timestamp)
        observations += counter.update([track], timestamp)
    return observations


# ------------------------------------------------------------ line validity
def test_line_rejects_identical_endpoints():
    with pytest.raises(GeometryError):
        make_line(point_a=(0.5, 0.5), point_b=(0.5, 0.5))


# ---------------------------------------------------------------- crossings
def test_one_person_crossing_counts_exactly_once():
    counter = LineCounter([make_line()], camera_id="cam1")
    observations = walk(counter, [0.20, 0.30, 0.40, 0.48, 0.55, 0.65, 0.75])
    assert len(observations) == 1
    totals = counter.totals()["l1"]
    assert totals.entered + totals.exited == 1


def test_direction_is_reported_and_is_reversible():
    """The convention is arbitrary but must be consistent -- the UI draws an
    arrow so the installer can see which way counts as entering."""
    counter = LineCounter([make_line()], camera_id="cam1")
    left_to_right = walk(counter, [0.2, 0.4, 0.6, 0.8])[0].direction

    inverted = LineCounter([make_line(invert_direction=True)], camera_id="cam1")
    inverted_direction = walk(inverted, [0.2, 0.4, 0.6, 0.8])[0].direction

    assert left_to_right != inverted_direction
    assert {left_to_right, inverted_direction} == {Direction.IN, Direction.OUT}


def test_opposite_directions_are_distinguished():
    counter = LineCounter([make_line()], camera_id="cam1")
    forward = walk(counter, [0.2, 0.4, 0.6, 0.8], track_id=1)[0]
    backward = walk(counter, [0.8, 0.6, 0.4, 0.2], track_id=2)[0]
    assert forward.direction != backward.direction

    totals = counter.totals()["l1"]
    assert totals.entered == 1
    assert totals.exited == 1
    assert totals.net == 0


def test_walking_alongside_a_line_never_counts():
    counter = LineCounter([make_line()], camera_id="cam1")
    # Parallel to the line and well clear of it.
    assert walk(counter, [0.1, 0.12, 0.14, 0.16, 0.18]) == []


def test_walking_past_the_end_of_a_line_does_not_count():
    """A finite line: someone walking beyond the doorway is not entering it."""
    # The line spans only the lower half of the frame.
    counter = LineCounter([make_line(point_a=(0.5, 0.0), point_b=(0.5, 0.4))], camera_id="cam1")
    assert walk(counter, [0.2, 0.4, 0.6, 0.8], y=0.9) == []


def test_jitter_on_the_line_does_not_count():
    """Someone standing in a doorway must not be counted repeatedly."""
    counter = LineCounter([make_line(cooldown_seconds=0.0, min_travel=0.02)], camera_id="cam1")
    # Wobble either side of x = 0.5 by less than min_travel.
    xs = [0.495, 0.505, 0.494, 0.506, 0.495, 0.505, 0.496, 0.504]
    assert walk(counter, xs) == []


def test_cooldown_prevents_double_counting_on_a_hesitant_crossing():
    """A person who steps in, hesitates, steps back and comes in again is one
    visitor, not three."""
    counter = LineCounter([make_line(cooldown_seconds=5.0)], camera_id="cam1")
    xs = [0.30, 0.45, 0.60, 0.45, 0.30, 0.45, 0.60]
    observations = walk(counter, xs)
    assert len(observations) == 1


def test_cooldown_expires_so_a_genuine_return_is_counted():
    counter = LineCounter([make_line(cooldown_seconds=1.0)], camera_id="cam1")
    # 10 FPS: the round trip below spans well over a second.
    xs = [0.3, 0.45, 0.6] + [0.6] * 15 + [0.45, 0.3]
    observations = walk(counter, xs)
    assert len(observations) == 2
    assert observations[0].direction != observations[1].direction


def test_two_people_are_counted_separately():
    counter = LineCounter([make_line()], camera_id="cam1")
    walk(counter, [0.2, 0.4, 0.6, 0.8], track_id=1)
    walk(counter, [0.2, 0.4, 0.6, 0.8], track_id=2)
    assert counter.totals()["l1"].entered + counter.totals()["l1"].exited == 2


def test_line_only_counts_configured_classes():
    counter = LineCounter([make_line(trigger_classes=frozenset({"person"}))], camera_id="cam1")
    assert walk(counter, [0.2, 0.4, 0.6, 0.8], class_name="car") == []
    assert len(walk(counter, [0.2, 0.4, 0.6, 0.8], class_name="person", track_id=2)) == 1


def test_a_fast_crossing_between_two_frames_is_still_counted():
    """At 5 FPS a walking person can jump the line entirely in one step."""
    counter = LineCounter([make_line()], camera_id="cam1")
    observations = walk(counter, [0.30, 0.70], fps=5.0)
    assert len(observations) == 1


def test_occupancy_is_clamped_at_zero():
    """A missed entry must not produce a negative headcount."""
    counter = LineCounter([make_line()], camera_id="cam1")
    # With the default orientation, left-to-right is OUT: an exit with no
    # matching entry, which is exactly the case a missed detection creates.
    observations = walk(counter, [0.2, 0.4, 0.6, 0.8])
    assert observations[0].direction is Direction.OUT
    assert counter.totals()["l1"].net < 0
    assert counter.current_occupancy() == 0


def test_net_occupancy_after_entries_and_exits():
    counter = LineCounter([make_line(invert_direction=True)], camera_id="cam1")
    walk(counter, [0.2, 0.4, 0.6, 0.8], track_id=1)
    walk(counter, [0.2, 0.4, 0.6, 0.8], track_id=2)
    walk(counter, [0.8, 0.6, 0.4, 0.2], track_id=3)
    assert counter.current_occupancy() == 1


def test_reset_counts_zeroes_the_day():
    counter = LineCounter([make_line()], camera_id="cam1")
    walk(counter, [0.2, 0.4, 0.6, 0.8])
    counter.reset_counts()
    totals = counter.totals()["l1"]
    assert totals.entered == 0 and totals.exited == 0


def test_set_lines_preserves_counts_for_surviving_lines():
    counter = LineCounter([make_line()], camera_id="cam1")
    walk(counter, [0.2, 0.4, 0.6, 0.8])
    before = counter.totals()["l1"].exited + counter.totals()["l1"].entered

    counter.set_lines([make_line(), make_line(line_id="l2", name="Side door")])
    assert counter.totals()["l1"].entered + counter.totals()["l1"].exited == before
    assert "l2" in counter.totals()


def test_set_lines_drops_removed_lines():
    counter = LineCounter([make_line(), make_line(line_id="l2", name="Side")], camera_id="cam1")
    counter.set_lines([make_line()])
    assert set(counter.totals()) == {"l1"}


def test_inactive_lines_are_ignored():
    counter = LineCounter([make_line(is_active=False)], camera_id="cam1")
    assert walk(counter, [0.2, 0.4, 0.6, 0.8]) == []


def test_diagonal_line_is_handled():
    counter = LineCounter([make_line(point_a=(0.0, 0.0), point_b=(1.0, 1.0))], camera_id="cam1")
    # Move horizontally across the diagonal at y = 0.5, so the crossing is at
    # x = 0.5.
    observations = walk(counter, [0.2, 0.4, 0.6, 0.8], y=0.5)
    assert len(observations) == 1


def test_prune_clears_stale_crossing_state():
    counter = LineCounter([make_line()], camera_id="cam1")
    walk(counter, [0.2, 0.4, 0.6, 0.8])
    assert counter.prune(BASE + timedelta(seconds=600), max_idle_seconds=300) == 1


def test_observation_serialises_cleanly():
    counter = LineCounter([make_line()], camera_id="cam1")
    payload = walk(counter, [0.2, 0.4, 0.6, 0.8])[0].to_dict()
    assert payload["line_name"] == "Front door"
    assert payload["direction"] in {"IN", "OUT"}
    assert payload["track_id"] == 1


# ---------------------------------------------------- end-to-end with tracker
def test_counting_through_the_real_tracker():
    """Detector -> tracker -> counter, with no hand-fed track objects."""
    tracker = ByteTracker(camera_id="cam1", min_hits=2)
    counter = LineCounter([make_line()], camera_id="cam1")

    observations = []
    for step in range(24):
        x = 0.10 + step * 0.03
        detection = Detection(
            class_id=0,
            class_name="person",
            confidence=0.92,
            bbox=BoundingBox(x, 0.40, x + 0.07, 0.90),
            timestamp=at(step),
            camera_id="cam1",
        )
        tracks = tracker.update([detection], at(step))
        observations += counter.update(tracks, at(step))

    assert len(observations) == 1, f"expected one crossing, got {len(observations)}"


def test_two_people_through_the_real_tracker():
    tracker = ByteTracker(camera_id="cam1", min_hits=2)
    counter = LineCounter([make_line()], camera_id="cam1")

    observations = []
    for step in range(24):
        left = 0.05 + step * 0.03
        right = 0.90 - step * 0.03
        detections = [
            Detection(
                class_id=0,
                class_name="person",
                confidence=0.92,
                bbox=BoundingBox(left, 0.35, left + 0.06, 0.80),
                timestamp=at(step),
                camera_id="cam1",
            ),
            Detection(
                class_id=0,
                class_name="person",
                confidence=0.92,
                bbox=BoundingBox(right, 0.45, right + 0.06, 0.95),
                timestamp=at(step),
                camera_id="cam1",
            ),
        ]
        tracks = tracker.update(detections, at(step))
        observations += counter.update(tracks, at(step))

    assert len(observations) == 2
    directions = {o.direction for o in observations}
    assert directions == {Direction.IN, Direction.OUT}


# ------------------------------------------------------- occupancy tracker
def test_occupancy_tracker_sums_cameras_and_records_the_peak():
    tracker = OccupancyTracker()
    tracker.report("cam1", 5, BASE)
    tracker.report("cam2", 3, BASE)
    assert tracker.total() == 8

    tracker.report("cam1", 12, BASE + timedelta(minutes=1))
    assert tracker.total() == 15
    assert tracker.peak_occupancy == 15
    assert tracker.peak_at == BASE + timedelta(minutes=1)

    tracker.report("cam1", 2, BASE + timedelta(minutes=2))
    assert tracker.total() == 5
    assert tracker.peak_occupancy == 15  # the peak is remembered


def test_occupancy_tracker_ignores_negative_reports():
    tracker = OccupancyTracker()
    tracker.report("cam1", -4, BASE)
    assert tracker.total() == 0


# ------------------------------------------------- slow / high-FPS crossings
#
# The regression that motivated the hysteresis band: with a "did this frame's
# movement cross the line" test, a slow walker at a high frame rate has both
# its previous and current points inside the jitter tolerance, the crossing is
# discarded as noise, and by the next frame it is already on the far side. The
# person is never counted at all -- silently.


def test_a_slow_crossing_is_counted():
    counter = LineCounter([make_line()], camera_id="cam1")
    # 1% of frame width per frame: a normal walk at ~25 FPS.
    xs = [0.40 + i * 0.01 for i in range(21)]  # 0.40 -> 0.60, straddling 0.50
    observations = walk(counter, xs, fps=25.0)
    assert len(observations) == 1, "a slow crossing must still be counted exactly once"


def test_a_very_slow_crossing_is_counted():
    counter = LineCounter([make_line()], camera_id="cam1")
    # 0.2% per frame -- someone shuffling through a doorway at 30 FPS.
    xs = [0.45 + i * 0.002 for i in range(51)]
    observations = walk(counter, xs, fps=30.0)
    assert len(observations) == 1


def test_stopping_inside_the_band_and_going_back_does_not_count():
    """Approaching the line, hesitating on it, then retreating is not a
    crossing -- the confirmed side never flips."""
    counter = LineCounter([make_line(min_travel=0.03)], camera_id="cam1")
    xs = [0.40, 0.44, 0.48, 0.50, 0.49, 0.47, 0.44, 0.40]
    assert walk(counter, xs) == []


def test_crossing_is_counted_once_no_matter_the_frame_rate():
    """The same physical walk must give the same count at any FPS."""
    counts = []
    for fps, step in ((5.0, 0.10), (10.0, 0.05), (30.0, 0.01)):
        counter = LineCounter([make_line()], camera_id="cam1")
        steps = int(0.6 / step) + 1
        xs = [0.20 + i * step for i in range(steps)]
        counts.append(len(walk(counter, xs, fps=fps)))
    assert counts == [1, 1, 1], f"count varied with frame rate: {counts}"
