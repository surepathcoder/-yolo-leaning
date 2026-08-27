"""Vision engine -- core value types."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from vision.types import (
    MAX_TRAJECTORY_POINTS,
    BoundingBox,
    Detection,
    Frame,
    ReferencePoint,
    TrackedObject,
    new_session_id,
)


# ------------------------------------------------------------- bounding box
def test_bbox_basic_geometry():
    box = BoundingBox(0.2, 0.1, 0.6, 0.5)
    assert box.width == pytest.approx(0.4)
    assert box.height == pytest.approx(0.4)
    assert box.area == pytest.approx(0.16)
    assert box.center == pytest.approx((0.4, 0.3))
    assert box.bottom_center == pytest.approx((0.4, 0.5))
    assert box.top_center == pytest.approx((0.4, 0.1))


def test_bbox_clamps_to_frame():
    """A detector may report a box past the frame edge for a partly visible
    object. Clamping keeps the detection; rejecting it would lose real data."""
    box = BoundingBox(-0.3, -0.1, 1.4, 1.2)
    assert (box.x1, box.y1, box.x2, box.y2) == (0.0, 0.0, 1.0, 1.0)


def test_bbox_normalises_inverted_corners():
    box = BoundingBox(0.8, 0.9, 0.2, 0.3)
    assert box.x1 < box.x2 and box.y1 < box.y2


def test_bbox_iou_identical_is_one():
    box = BoundingBox(0.1, 0.1, 0.5, 0.5)
    assert box.iou(box) == pytest.approx(1.0)


def test_bbox_iou_disjoint_is_zero():
    a = BoundingBox(0.0, 0.0, 0.2, 0.2)
    b = BoundingBox(0.5, 0.5, 0.7, 0.7)
    assert a.iou(b) == 0.0


def test_bbox_iou_half_overlap():
    a = BoundingBox(0.0, 0.0, 0.2, 0.1)
    b = BoundingBox(0.1, 0.0, 0.3, 0.1)
    # intersection 0.1*0.1 = 0.01; union 0.02 + 0.02 - 0.01 = 0.03
    assert a.iou(b) == pytest.approx(1 / 3)


def test_bbox_touching_edges_have_zero_iou():
    a = BoundingBox(0.0, 0.0, 0.2, 0.2)
    b = BoundingBox(0.2, 0.0, 0.4, 0.2)
    assert a.iou(b) == 0.0


def test_bbox_pixel_round_trip():
    box = BoundingBox.from_pixels(100, 50, 300, 250, width=1000, height=500)
    assert box.x1 == pytest.approx(0.1)
    assert box.y2 == pytest.approx(0.5)
    assert box.to_pixels(1000, 500) == (100, 50, 300, 250)


def test_bbox_from_pixels_rejects_zero_size_frame():
    with pytest.raises(ValueError):
        BoundingBox.from_pixels(0, 0, 10, 10, width=0, height=100)


def test_bbox_from_xywh():
    box = BoundingBox.from_xywh(0.5, 0.5, 0.2, 0.4)
    assert box.x1 == pytest.approx(0.4)
    assert box.y2 == pytest.approx(0.7)


def test_bbox_reference_points():
    box = BoundingBox(0.2, 0.2, 0.4, 0.8)
    assert box.reference(ReferencePoint.BOTTOM_CENTER) == pytest.approx((0.3, 0.8))
    assert box.reference(ReferencePoint.TOP_CENTER) == pytest.approx((0.3, 0.2))
    assert box.reference(ReferencePoint.CENTER) == pytest.approx((0.3, 0.5))


def test_bbox_contains_point():
    box = BoundingBox(0.2, 0.2, 0.6, 0.6)
    assert box.contains_point(0.4, 0.4) is True
    assert box.contains_point(0.7, 0.4) is False
    assert box.contains_point(0.2, 0.2) is True  # boundary is inclusive


def test_bbox_is_hashable_and_immutable():
    box = BoundingBox(0.1, 0.1, 0.2, 0.2)
    assert hash(box) == hash(BoundingBox(0.1, 0.1, 0.2, 0.2))
    with pytest.raises(dataclasses.FrozenInstanceError):
        box.x1 = 0.5  # type: ignore[misc]


# -------------------------------------------------------------------- frame
def test_frame_dimensions():
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    frame = Frame(image=image, index=7, timestamp=datetime.now(UTC), camera_id="c1")
    assert frame.width == 640
    assert frame.height == 480
    assert frame.shape == (640, 480)
    assert frame.is_empty is False


def test_empty_frame_is_detected():
    frame = Frame(
        image=np.zeros((0, 0, 3), dtype=np.uint8),
        index=0,
        timestamp=datetime.now(UTC),
        camera_id="c1",
    )
    assert frame.is_empty is True


# ---------------------------------------------------------------- detection
def test_detection_serialises_without_losing_precision():
    detection = Detection(
        class_id=0,
        class_name="person",
        confidence=0.87654,
        bbox=BoundingBox(0.1, 0.2, 0.3, 0.4),
        timestamp=datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
        camera_id="cam-4",
    )
    payload = detection.to_dict()
    assert payload["class_name"] == "person"
    assert payload["confidence"] == pytest.approx(0.8765, abs=1e-4)
    assert payload["bbox"] == [0.1, 0.2, 0.3, 0.4]
    assert payload["timestamp"].startswith("2026-05-01T09:30")


def test_detection_with_track_id_returns_a_copy():
    detection = Detection(
        class_id=0,
        class_name="person",
        confidence=0.9,
        bbox=BoundingBox(0.1, 0.1, 0.2, 0.2),
        timestamp=datetime.now(UTC),
        camera_id="c",
    )
    tracked = detection.with_track_id(42)
    assert tracked.track_id == 42
    assert detection.track_id is None  # original untouched


# ------------------------------------------------------------ tracked object
def _track(**overrides) -> TrackedObject:
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    defaults: dict = {
        "track_id": 42,
        "class_id": 0,
        "class_name": "person",
        "camera_id": "cam-4",
        "session_id": "sess01",
        "bbox": BoundingBox(0.2, 0.3, 0.3, 0.8),
        "confidence": 0.9,
        "first_seen": base,
        "last_seen": base,
    }
    defaults.update(overrides)
    return TrackedObject(**defaults)


def test_track_key_includes_camera_and_session():
    """Trackers restart numbering at 1, so the session is what stops
    yesterday's track 42 merging with today's."""
    track = _track()
    assert track.track_key == "cam-4:sess01:42"


def test_track_duration():
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    track = _track(first_seen=base, last_seen=base + timedelta(seconds=84))
    assert track.duration_seconds == pytest.approx(84.0)


def test_track_update_records_trajectory():
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    track = _track(first_seen=base, last_seen=base)
    for i in range(1, 6):
        track.update(
            BoundingBox(0.2 + i * 0.05, 0.3, 0.3 + i * 0.05, 0.8),
            0.85,
            base + timedelta(seconds=i),
        )
    assert track.frame_count == 6
    assert len(track.trajectory) == 5
    assert track.missed_frames == 0


def test_track_keeps_best_confidence():
    track = _track(confidence=0.5)
    track.update(track.bbox, 0.95, track.last_seen + timedelta(seconds=1))
    track.update(track.bbox, 0.60, track.last_seen + timedelta(seconds=2))
    assert track.best_confidence == pytest.approx(0.95)
    assert track.confidence == pytest.approx(0.60)


def test_trajectory_is_capped():
    """An unbounded path would grow all day on a loitering track."""
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    track = _track(first_seen=base, last_seen=base)
    for i in range(1, 600):
        track.update(track.bbox, 0.8, base + timedelta(seconds=i))
    assert len(track.trajectory) <= MAX_TRAJECTORY_POINTS


def test_previous_reference_needs_two_samples():
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    track = _track(first_seen=base, last_seen=base)
    assert track.previous_reference() is None
    track.update(BoundingBox(0.3, 0.3, 0.4, 0.8), 0.9, base + timedelta(seconds=1))
    assert track.previous_reference() is None  # only one recorded point yet
    track.update(BoundingBox(0.4, 0.3, 0.5, 0.8), 0.9, base + timedelta(seconds=2))
    assert track.previous_reference() == pytest.approx((0.35, 0.8))


def test_displacement_and_path_length():
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    track = _track(first_seen=base, last_seen=base)
    # Out and back: path length is large, displacement is ~zero.
    for i, x in enumerate([0.3, 0.4, 0.5, 0.4, 0.3], start=1):
        track.update(BoundingBox(x, 0.3, x + 0.1, 0.8), 0.9, base + timedelta(seconds=i))
    assert track.displacement() == pytest.approx(0.0, abs=1e-6)
    assert track.path_length() > 0.3


def test_track_state_dict_is_per_track():
    """Rule state lives on the track so it dies with the track."""
    a, b = _track(track_id=1), _track(track_id=2)
    a.state["zone:z1:entered_at"] = "x"
    assert "zone:z1:entered_at" not in b.state


def test_track_dict_has_no_identifying_information():
    """Privacy: a serialised track must carry no personal identity."""
    payload = _track().to_dict()
    assert payload["track_id"] == 42
    assert "name" not in payload
    assert "face" not in payload
    assert "identity" not in payload


def test_session_ids_are_unique():
    assert len({new_session_id() for _ in range(100)}) == 100
