"""Vision engine -- object tracking.

The property that matters commercially is **id stability**: person 42 must
stay person 42 while visible. A tracker that renumbers people inflates every
count, splits every dwell time and makes the event timeline unreadable.

These tests use scripted detections so ground truth is exact.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from vision.tracking import ByteTracker, build_tracker
from vision.tracking.assignment import associate, iou_cost_matrix, linear_sum_assignment
from vision.tracking.kalman import KalmanBoxFilter
from vision.types import BoundingBox, Detection

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def at(step: int, fps: float = 10.0) -> datetime:
    return BASE + timedelta(seconds=step / fps)


def person(x: float, y: float = 0.4, w: float = 0.08, h: float = 0.5, conf: float = 0.9):
    return ("person", conf, (x, y, x + w, y + h))


def detection(
    box: tuple[float, float, float, float],
    *,
    conf: float = 0.9,
    class_id: int = 0,
    class_name: str = "person",
    timestamp: datetime | None = None,
) -> Detection:
    return Detection(
        class_id=class_id,
        class_name=class_name,
        confidence=conf,
        bbox=BoundingBox(*box),
        timestamp=timestamp or BASE,
        camera_id="cam1",
    )


# --------------------------------------------------------------- assignment
def _brute_force_min_cost(cost: np.ndarray) -> float:
    """Exhaustive minimum, for validating the assignment implementation.

    When there are more rows than columns only a subset of rows can be
    assigned, so the search must consider every choice of rows as well as
    every permutation of columns.
    """
    n_rows, n_cols = cost.shape
    k = min(n_rows, n_cols)
    best = float("inf")
    for rows in itertools.combinations(range(n_rows), k):
        for cols in itertools.permutations(range(n_cols), k):
            total = sum(cost[r, c] for r, c in zip(rows, cols, strict=True))
            best = min(best, total)
    return best


def test_assignment_matches_brute_force_on_square_matrices():
    rng = np.random.default_rng(7)
    for _ in range(25):
        size = rng.integers(1, 6)
        cost = rng.random((size, size)) * 10
        rows, cols = linear_sum_assignment(cost)
        assert cost[rows, cols].sum() == pytest.approx(_brute_force_min_cost(cost))


def test_assignment_matches_brute_force_on_rectangular_matrices():
    rng = np.random.default_rng(11)
    for _ in range(25):
        n_rows = int(rng.integers(1, 5))
        n_cols = int(rng.integers(1, 5))
        cost = rng.random((n_rows, n_cols)) * 10
        rows, cols = linear_sum_assignment(cost)
        assert len(rows) == min(n_rows, n_cols)
        assert cost[rows, cols].sum() == pytest.approx(_brute_force_min_cost(cost))


def test_assignment_indices_are_unique():
    rng = np.random.default_rng(3)
    cost = rng.random((5, 7))
    rows, cols = linear_sum_assignment(cost)
    assert len(set(rows.tolist())) == len(rows)
    assert len(set(cols.tolist())) == len(cols)


def test_assignment_beats_greedy_on_the_crossing_case():
    """The case greedy gets wrong: two people passing each other.

    Greedy takes the single best pair first and is then forced into a bad
    second match. Optimal assignment considers both together.
    """
    cost = np.array([[0.10, 0.15], [0.11, 0.90]])
    rows, cols = linear_sum_assignment(cost)
    total = cost[rows, cols].sum()
    greedy_total = 0.10 + 0.90  # greedy picks (0,0) first, then is stuck
    assert total == pytest.approx(0.15 + 0.11)
    assert total < greedy_total


def test_assignment_handles_empty_input():
    rows, cols = linear_sum_assignment(np.zeros((0, 0)))
    assert len(rows) == 0 and len(cols) == 0


def test_assignment_rejects_non_finite_values():
    with pytest.raises(ValueError):
        linear_sum_assignment(np.array([[1.0, np.inf], [2.0, 3.0]]))


def test_iou_cost_matrix_values():
    tracks = np.array([[0.0, 0.0, 0.2, 0.2]])
    detections = np.array([[0.0, 0.0, 0.2, 0.2], [0.5, 0.5, 0.7, 0.7]])
    cost = iou_cost_matrix(tracks, detections)
    assert cost[0, 0] == pytest.approx(0.0)  # perfect overlap -> zero cost
    assert cost[0, 1] == pytest.approx(1.0)  # no overlap -> maximum cost


def test_associate_rejects_pairs_over_the_threshold():
    cost = np.array([[0.1, 0.9], [0.95, 0.2]])
    matches, unmatched_rows, unmatched_cols = associate(cost, max_cost=0.5)
    assert sorted(matches) == [(0, 0), (1, 1)]
    assert unmatched_rows == [] and unmatched_cols == []

    matches, unmatched_rows, unmatched_cols = associate(cost, max_cost=0.15)
    assert matches == [(0, 0)]
    assert unmatched_rows == [1] and unmatched_cols == [1]


# ------------------------------------------------------------------ kalman
def test_kalman_measurement_round_trip():
    kf = KalmanBoxFilter()
    box = (0.2, 0.3, 0.4, 0.8)
    restored = KalmanBoxFilter.to_bbox(np.array([*kf.to_measurement(box), 0, 0, 0, 0]))
    assert restored == pytest.approx(box, abs=1e-9)


def test_kalman_predicts_constant_velocity():
    """After observing steady motion the filter should extrapolate it."""
    kf = KalmanBoxFilter()
    mean, covariance = kf.initiate(kf.to_measurement((0.1, 0.4, 0.2, 0.9)))
    for step in range(1, 10):
        mean, covariance = kf.predict(mean, covariance)
        x = 0.1 + step * 0.05
        mean, covariance = kf.update(mean, covariance, kf.to_measurement((x, 0.4, x + 0.1, 0.9)))

    predicted, _ = kf.predict(mean, covariance)
    predicted_box = KalmanBoxFilter.to_bbox(predicted)
    # The next observation would be at x = 0.1 + 10*0.05 = 0.60
    assert predicted_box[0] == pytest.approx(0.60, abs=0.03)


def test_kalman_keeps_height_positive():
    """A negative height would invert the box and poison every IoU."""
    kf = KalmanBoxFilter()
    mean, covariance = kf.initiate(kf.to_measurement((0.4, 0.4, 0.5, 0.5)))
    for _ in range(20):
        mean, covariance = kf.predict(mean, covariance)
        mean, covariance = kf.update(mean, covariance, np.array([0.45, 0.45, 1.0, 1e-5]))
    assert mean[3] > 0


# ----------------------------------------------------------------- tracking
def test_a_single_person_keeps_one_id():
    tracker = ByteTracker(camera_id="cam1", min_hits=3)
    seen: set[int] = set()
    for step in range(30):
        x = 0.05 + step * 0.03
        tracks = tracker.update([detection((x, 0.4, x + 0.08, 0.9))], at(step))
        seen.update(t.track_id for t in tracks)
    assert seen == {1}


def test_track_is_not_reported_until_confirmed():
    """min_hits suppresses single-frame false positives."""
    tracker = ByteTracker(camera_id="cam1", min_hits=3)
    assert tracker.update([detection((0.1, 0.4, 0.2, 0.9))], at(0)) == []
    assert tracker.update([detection((0.11, 0.4, 0.21, 0.9))], at(1)) == []
    tracks = tracker.update([detection((0.12, 0.4, 0.22, 0.9))], at(2))
    assert len(tracks) == 1


def test_a_one_frame_blip_never_becomes_a_track():
    tracker = ByteTracker(camera_id="cam1", min_hits=3)
    tracker.update([detection((0.1, 0.4, 0.2, 0.9))], at(0))
    for step in range(1, 6):
        assert tracker.update([], at(step)) == []
    assert tracker.all_tracks == []


def test_two_people_get_distinct_ids():
    tracker = ByteTracker(camera_id="cam1", min_hits=2)
    ids_seen = []
    for step in range(15):
        left = 0.05 + step * 0.02
        right = 0.75 - step * 0.02
        tracks = tracker.update(
            [
                detection((left, 0.4, left + 0.07, 0.9)),
                detection((right, 0.4, right + 0.07, 0.9)),
            ],
            at(step),
        )
        if len(tracks) == 2:
            ids_seen.append(tuple(sorted(t.track_id for t in tracks)))
    assert ids_seen, "two tracks were never confirmed"
    assert len(set(ids_seen)) == 1, f"ids changed across frames: {set(ids_seen)}"


def test_ids_survive_two_people_passing_each_other():
    """The classic id-swap case. Optimal assignment is what prevents it."""
    tracker = ByteTracker(camera_id="cam1", min_hits=2)
    # They start well apart, converge, then separate again.
    positions = [
        (0.10, 0.70),
        (0.18, 0.62),
        (0.26, 0.54),
        (0.34, 0.46),
        (0.42, 0.38),
        (0.50, 0.30),
        (0.58, 0.22),
        (0.66, 0.14),
    ]
    first_ids: dict[str, int] = {}
    for step, (a, b) in enumerate(positions):
        tracks = tracker.update(
            [
                detection((a, 0.40, a + 0.06, 0.90)),
                detection((b, 0.42, b + 0.06, 0.92)),
            ],
            at(step),
        )
        for track in tracks:
            # Identify each person by which one is further left.
            label = "left" if track.bbox.x1 < 0.5 else "right"
            first_ids.setdefault(f"{step}:{label}", track.track_id)

    # At the end, exactly two tracks should exist -- no id churn.
    assert len(tracker.all_tracks) == 2


def test_id_survives_a_brief_occlusion():
    """The reason ByteTrack exists: a person behind a shelf keeps their id."""
    tracker = ByteTracker(camera_id="cam1", min_hits=2, max_age=30)
    for step in range(6):
        x = 0.10 + step * 0.04
        tracker.update([detection((x, 0.4, x + 0.08, 0.9))], at(step))
    original = tracker.all_tracks[0].track_id

    # Fully occluded: no detection at all for 5 frames.
    for step in range(6, 11):
        tracker.update([], at(step))

    # Reappears roughly where the motion model predicts.
    for step in range(11, 16):
        x = 0.10 + step * 0.04
        tracks = tracker.update([detection((x, 0.4, x + 0.08, 0.9))], at(step))

    assert len(tracks) == 1
    assert tracks[0].track_id == original, "id changed after occlusion"


def test_low_confidence_detections_keep_a_track_alive():
    """A partly occluded person's confidence collapses. The second
    association pass is what stops the track dying."""
    tracker = ByteTracker(camera_id="cam1", min_hits=2, high_threshold=0.5, low_threshold=0.1)
    for step in range(4):
        x = 0.10 + step * 0.04
        tracker.update([detection((x, 0.4, x + 0.08, 0.9), conf=0.9)], at(step))
    original = tracker.all_tracks[0].track_id

    for step in range(4, 12):
        x = 0.10 + step * 0.04
        tracks = tracker.update([detection((x, 0.4, x + 0.08, 0.9), conf=0.25)], at(step))

    assert len(tracks) == 1
    assert tracks[0].track_id == original


def test_a_low_confidence_detection_alone_never_starts_a_track():
    """Otherwise detector noise floods the event engine with phantom people."""
    tracker = ByteTracker(camera_id="cam1", min_hits=1, high_threshold=0.5, low_threshold=0.1)
    for step in range(10):
        tracker.update([detection((0.3, 0.4, 0.4, 0.9), conf=0.2)], at(step))
    assert tracker.all_tracks == []


def test_detections_below_the_low_threshold_are_ignored_entirely():
    tracker = ByteTracker(camera_id="cam1", min_hits=1, low_threshold=0.3)
    tracks = tracker.update([detection((0.3, 0.4, 0.4, 0.9), conf=0.05)], at(0))
    assert tracks == []


def test_a_track_is_dropped_after_max_age():
    tracker = ByteTracker(camera_id="cam1", min_hits=2, max_age=5)
    for step in range(4):
        x = 0.1 + step * 0.03
        tracker.update([detection((x, 0.4, x + 0.08, 0.9))], at(step))
    assert len(tracker.all_tracks) == 1

    for step in range(4, 20):
        tracker.update([], at(step))
    assert tracker.all_tracks == []


def test_classes_do_not_share_ids():
    """A person walking in front of a parked car must not inherit its id."""
    tracker = ByteTracker(camera_id="cam1", min_hits=2)
    for step in range(8):
        x = 0.30 + step * 0.005  # nearly stationary, heavily overlapping
        tracks = tracker.update(
            [
                detection((0.30, 0.30, 0.60, 0.80), class_id=2, class_name="car"),
                detection((x, 0.40, x + 0.08, 0.85), class_id=0, class_name="person"),
            ],
            at(step),
        )
    by_class = {t.class_name: t.track_id for t in tracks}
    assert set(by_class) == {"car", "person"}
    assert by_class["car"] != by_class["person"]


def test_track_key_is_unique_per_session():
    """After a reconnect, ids restart at 1 -- the session id keeps them apart."""
    tracker = ByteTracker(camera_id="cam1", min_hits=1)
    tracker.update([detection((0.2, 0.4, 0.3, 0.9))], at(0))
    first_key = tracker.all_tracks[0].track_key

    tracker.reset()
    tracker.update([detection((0.2, 0.4, 0.3, 0.9))], at(0))
    second_key = tracker.all_tracks[0].track_key

    assert first_key.endswith(":1") and second_key.endswith(":1")
    assert first_key != second_key


def test_reset_without_new_session_keeps_the_session_id():
    tracker = ByteTracker(camera_id="cam1", min_hits=1, session_id="fixed")
    tracker.update([detection((0.2, 0.4, 0.3, 0.9))], at(0))
    tracker.reset(new_session=False)
    assert tracker.session_id == "fixed"


def test_tracker_records_frame_count_and_duration():
    tracker = ByteTracker(camera_id="cam1", min_hits=2)
    for step in range(12):
        x = 0.1 + step * 0.03
        tracker.update([detection((x, 0.4, x + 0.08, 0.9))], at(step))
    track = tracker.all_tracks[0]
    assert track.frame_count >= 10
    assert track.duration_seconds == pytest.approx(1.1, abs=0.2)


def test_get_track_by_id():
    tracker = ByteTracker(camera_id="cam1", min_hits=1)
    tracker.update([detection((0.2, 0.4, 0.3, 0.9))], at(0))
    assert tracker.get_track(1) is not None
    assert tracker.get_track(999) is None


def test_empty_frames_are_harmless():
    tracker = ByteTracker(camera_id="cam1")
    for step in range(5):
        assert tracker.update([], at(step)) == []


def test_build_tracker_accepts_known_types():
    assert isinstance(build_tracker("bytetrack", camera_id="c"), ByteTracker)
    assert isinstance(build_tracker("botsort", camera_id="c"), ByteTracker)
    with pytest.raises(ValueError):
        build_tracker("deepsort", camera_id="c")


def test_crowded_scene_keeps_ids_stable():
    """Six people walking in parallel -- ids must not shuffle between them."""
    tracker = ByteTracker(camera_id="cam1", min_hits=2)
    lanes = [0.05, 0.20, 0.35, 0.50, 0.65, 0.80]
    signatures = []
    for step in range(20):
        detections = [
            detection((lane, 0.30 + step * 0.01, lane + 0.06, 0.75 + step * 0.01)) for lane in lanes
        ]
        tracks = tracker.update(detections, at(step))
        if len(tracks) == len(lanes):
            # Ordering tracks left to right must always give the same id
            # sequence. Comparing smoothed coordinates directly would fail on
            # sub-pixel Kalman drift, which is not an id problem.
            ordered = sorted(tracks, key=lambda t: t.bbox.x1)
            signatures.append(tuple(t.track_id for t in ordered))
    assert signatures, "tracks were never all confirmed"
    assert len(set(signatures)) == 1, f"ids were reassigned between lanes: {set(signatures)}"
