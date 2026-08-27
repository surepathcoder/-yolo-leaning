"""Vision engine -- shelf occupancy and inventory observations.

The expensive failure here is not an inaccurate ratio, it is a confident wrong
event. A shelf that reports SHELF_EMPTY because a shopper stood in front of it
trains staff to ignore stock alerts entirely, so occlusion handling and the
anti-phantom machinery get the most attention below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vision.inventory import (
    InventoryEngine,
    InventoryObservationKind,
    OccupancyLevel,
    ShelfDefinition,
    calibrate_baseline,
    estimate_occupancy,
)
from vision.types import BoundingBox, Detection
from vision.zones.geometry import GeometryError

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

#: A shelf occupying the middle band of the frame.
SHELF = [(0.2, 0.4), (0.8, 0.4), (0.8, 0.6), (0.2, 0.6)]


def shelf(**overrides) -> ShelfDefinition:
    kwargs = {
        "shelf_id": "shelf-1",
        "name": "Aisle 3 top",
        "polygon": SHELF,
        "camera_id": "cam-1",
        "sample_interval_seconds": 60.0,
    }
    kwargs.update(overrides)
    return ShelfDefinition(**kwargs)


def detection(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    class_name: str = "bottle",
    confidence: float = 0.9,
    timestamp: datetime = BASE,
) -> Detection:
    return Detection(
        class_id=0,
        class_name=class_name,
        confidence=confidence,
        bbox=BoundingBox(x1, y1, x2, y2),
        timestamp=timestamp,
        camera_id="cam-1",
    )


def full_shelf(**kwargs) -> list[Detection]:
    """Detections covering the whole shelf band."""
    return [detection(0.2, 0.4, 0.8, 0.6, **kwargs)]


# --------------------------------------------------------------- occupancy


def test_empty_shelf_reads_zero():
    reading = estimate_occupancy(shelf(), [], BASE)
    assert reading.raw_coverage == 0.0
    assert reading.occupancy_ratio == 0.0
    assert reading.level is OccupancyLevel.EMPTY


def test_fully_covered_shelf_reads_full():
    reading = estimate_occupancy(shelf(), full_shelf(), BASE)
    assert reading.raw_coverage == pytest.approx(1.0)
    assert reading.level is OccupancyLevel.FULL


def test_half_covered_shelf_reads_about_half():
    reading = estimate_occupancy(shelf(), [detection(0.2, 0.4, 0.5, 0.6)], BASE)
    assert reading.raw_coverage == pytest.approx(0.5, abs=0.02)
    assert reading.level is OccupancyLevel.NORMAL


def test_detections_outside_the_shelf_are_ignored():
    away = [detection(0.0, 0.0, 0.15, 0.15)]
    assert estimate_occupancy(shelf(), away, BASE).raw_coverage == 0.0


def test_overlapping_detections_do_not_exceed_full():
    """Two boxes over the same patch describe one patch, not two."""
    doubled = [detection(0.2, 0.4, 0.8, 0.6), detection(0.2, 0.4, 0.8, 0.6)]
    assert estimate_occupancy(shelf(), doubled, BASE).raw_coverage == pytest.approx(1.0)


def test_low_confidence_detections_are_dropped():
    weak = [detection(0.2, 0.4, 0.8, 0.6, confidence=0.1)]
    assert estimate_occupancy(shelf(min_confidence=0.25), weak, BASE).raw_coverage == 0.0


def test_estimation_is_deterministic():
    """Identical input must give an identical ratio, or thresholds jitter."""
    dets = [detection(0.3, 0.4, 0.55, 0.6)]
    first = estimate_occupancy(shelf(), dets, BASE)
    second = estimate_occupancy(shelf(), dets, BASE)
    assert first.raw_coverage == second.raw_coverage


def test_baseline_scales_coverage():
    """A shelf that reads 0.6 when known-full is 100% full, not 60%."""
    dets = [detection(0.2, 0.4, 0.56, 0.6)]  # ~0.6 raw coverage
    uncalibrated = estimate_occupancy(shelf(), dets, BASE)
    calibrated = estimate_occupancy(shelf(baseline_occupancy=0.6), dets, BASE)
    assert uncalibrated.level is OccupancyLevel.NORMAL
    assert calibrated.occupancy_ratio == pytest.approx(1.0, abs=0.05)
    assert calibrated.level is OccupancyLevel.FULL


def test_baseline_correction_is_clamped():
    over = estimate_occupancy(shelf(baseline_occupancy=0.5), full_shelf(), BASE)
    assert over.occupancy_ratio == 1.0


def test_product_counts_are_reported_per_class():
    dets = [
        detection(0.25, 0.42, 0.35, 0.58, class_name="bottle"),
        detection(0.40, 0.42, 0.50, 0.58, class_name="bottle"),
        detection(0.60, 0.42, 0.70, 0.58, class_name="cup"),
    ]
    reading = estimate_occupancy(shelf(), dets, BASE)
    assert reading.product_counts == {"bottle": 2, "cup": 1}
    assert reading.detected_object_count == 3


def test_product_classes_filter_what_counts_as_stock():
    dets = [detection(0.2, 0.4, 0.8, 0.6, class_name="cup")]
    only_bottles = shelf(product_classes=frozenset({"bottle"}))
    assert estimate_occupancy(only_bottles, dets, BASE).raw_coverage == 0.0


def test_zero_area_polygon_is_rejected_at_construction():
    """A shelf with no area cannot be measured, so it cannot be built."""
    with pytest.raises(GeometryError):
        ShelfDefinition(
            shelf_id="s", name="collinear", polygon=[(0.1, 0.1), (0.2, 0.2), (0.3, 0.3)]
        )


def test_a_very_small_shelf_is_still_measured():
    """The grid scales to the polygon, so a small shelf is not a blind one."""
    tiny = ShelfDefinition(
        shelf_id="s", name="tiny", polygon=[(0.5, 0.5), (0.52, 0.5), (0.52, 0.52), (0.5, 0.52)]
    )
    assert estimate_occupancy(tiny, [], BASE).level is OccupancyLevel.EMPTY
    stocked = [detection(0.5, 0.5, 0.52, 0.52)]
    assert estimate_occupancy(tiny, stocked, BASE).level is OccupancyLevel.FULL


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.0, OccupancyLevel.EMPTY),
        (0.12, OccupancyLevel.EMPTY),
        (0.13, OccupancyLevel.LOW),
        (0.35, OccupancyLevel.LOW),
        (0.36, OccupancyLevel.NORMAL),
        (0.84, OccupancyLevel.NORMAL),
        (0.85, OccupancyLevel.FULL),
        (1.0, OccupancyLevel.FULL),
    ],
)
def test_level_thresholds(ratio, expected):
    assert shelf().level_for(ratio) is expected


def test_thresholds_must_be_ordered():
    with pytest.raises(ValueError, match="thresholds"):
        shelf(low_threshold=0.1, empty_threshold=0.5)


def test_baseline_must_be_a_fraction():
    with pytest.raises(ValueError, match="baseline_occupancy"):
        shelf(baseline_occupancy=0.0)


# --------------------------------------------------------------- occlusion


def test_person_in_front_of_shelf_reads_unknown_not_empty():
    """The single most important behaviour in this module."""
    blocked = [detection(0.2, 0.3, 0.8, 0.7, class_name="person")]
    reading = estimate_occupancy(shelf(), blocked, BASE)
    assert reading.is_occluded
    assert reading.level is OccupancyLevel.UNKNOWN
    assert reading.confidence == 0.0
    assert not reading.is_usable


def test_a_person_is_never_counted_as_stock():
    blocked = [detection(0.2, 0.3, 0.8, 0.7, class_name="person")]
    assert estimate_occupancy(shelf(), blocked, BASE).detected_object_count == 0


def test_small_occlusion_does_not_blind_the_shelf():
    """An arm reaching in must not suppress the reading."""
    dets = [*full_shelf(), detection(0.20, 0.45, 0.28, 0.55, class_name="person")]
    reading = estimate_occupancy(shelf(), dets, BASE)
    assert not reading.is_occluded
    assert reading.level is OccupancyLevel.FULL


def test_coverage_is_measured_over_the_visible_shelf_only():
    """Half hidden, visible half stocked => stocked, not half empty."""
    dets = [
        detection(0.2, 0.4, 0.5, 0.6),  # stock on the left half
        detection(0.5, 0.3, 0.8, 0.7, class_name="person"),  # right half hidden
    ]
    reading = estimate_occupancy(shelf(), dets, BASE)
    assert reading.occluded_ratio == pytest.approx(0.5, abs=0.03)
    assert reading.raw_coverage == pytest.approx(1.0, abs=0.05)


def test_occlusion_lowers_confidence():
    partly = [*full_shelf(), detection(0.2, 0.4, 0.4, 0.6, class_name="person")]
    reading = estimate_occupancy(shelf(occlusion_threshold=0.9), partly, BASE)
    assert not reading.is_occluded
    assert reading.confidence < 0.8


# ------------------------------------------------------------- calibration


def test_calibrate_baseline_uses_the_median():
    readings = [estimate_occupancy(shelf(), full_shelf(), BASE)] * 3
    assert calibrate_baseline(readings) == pytest.approx(1.0)


def test_calibrate_baseline_ignores_occluded_readings():
    blocked = estimate_occupancy(
        shelf(), [detection(0.2, 0.3, 0.8, 0.7, class_name="person")], BASE
    )
    good = estimate_occupancy(shelf(), full_shelf(), BASE)
    assert calibrate_baseline([blocked, good, good]) == pytest.approx(1.0)


def test_calibrate_baseline_returns_none_when_nothing_usable():
    blocked = estimate_occupancy(
        shelf(), [detection(0.2, 0.3, 0.8, 0.7, class_name="person")], BASE
    )
    assert calibrate_baseline([blocked]) is None


# ------------------------------------------------------------------ engine


def engine(**overrides) -> InventoryEngine:
    kwargs = {"shelves": [shelf()], "camera_id": "cam-1", "stability_samples": 2}
    kwargs.update(overrides)
    return InventoryEngine(**kwargs)


def settle(eng: InventoryEngine, dets: list[Detection], start: datetime, samples: int = 2):
    """Drive the engine until a value is confirmed stable."""
    out = []
    for i in range(samples):
        out += eng.update(dets, start + timedelta(seconds=60 * i))
    return out


def test_first_stable_reading_emits_nothing():
    """There is no 'before' yet, so there is no change to report."""
    assert settle(engine(), full_shelf(), BASE) == []


def test_sampling_interval_is_respected():
    eng = engine()
    eng.update(full_shelf(), BASE)
    eng.update(full_shelf(), BASE + timedelta(seconds=5))
    # Only the first reading was taken, so nothing is confirmed yet.
    assert eng.occupancy()["shelf-1"][0] is None


def test_stock_removal_is_reported():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    later = BASE + timedelta(seconds=300)
    obs = settle(eng, [detection(0.2, 0.4, 0.4, 0.6)], later)
    kinds = [o.kind for o in obs]
    assert InventoryObservationKind.PRODUCT_REMOVED in kinds
    removal = next(o for o in obs if o.kind is InventoryObservationKind.PRODUCT_REMOVED)
    assert removal.delta < 0
    assert removal.occupancy_before == pytest.approx(1.0)


def test_emptying_a_shelf_reports_empty():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    obs = settle(eng, [], BASE + timedelta(seconds=300))
    assert InventoryObservationKind.SHELF_EMPTY in [o.kind for o in obs]


def test_restocking_is_distinguished_from_a_return():
    eng = engine()
    settle(eng, [], BASE)
    obs = settle(eng, full_shelf(), BASE + timedelta(seconds=300))
    kinds = [o.kind for o in obs]
    assert InventoryObservationKind.SHELF_RESTOCKED in kinds
    assert InventoryObservationKind.PRODUCT_RETURNED not in kinds


def test_small_return_is_reported_as_a_return():
    eng = engine(restock_threshold=0.9)
    settle(eng, [detection(0.2, 0.4, 0.4, 0.6)], BASE)
    obs = settle(eng, [detection(0.2, 0.4, 0.6, 0.6)], BASE + timedelta(seconds=300))
    assert InventoryObservationKind.PRODUCT_RETURNED in [o.kind for o in obs]


def test_noise_below_the_change_threshold_emits_nothing():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    nudged = [detection(0.2, 0.4, 0.79, 0.6)]
    assert settle(eng, nudged, BASE + timedelta(seconds=300)) == []


def test_a_single_anomalous_reading_never_emits():
    """One dropped frame must not manufacture a stockout."""
    eng = engine(stability_samples=2)
    settle(eng, full_shelf(), BASE)
    t = BASE + timedelta(seconds=300)
    assert eng.update([], t) == []  # anomaly
    assert eng.update(full_shelf(), t + timedelta(seconds=60)) == []  # recovered


def test_occluded_readings_never_move_state_or_emit():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    before = eng.occupancy()["shelf-1"][0]
    blocked = [detection(0.2, 0.3, 0.8, 0.7, class_name="person")]
    t = BASE + timedelta(seconds=300)
    assert eng.update(blocked, t) == []
    assert eng.update(blocked, t + timedelta(seconds=60)) == []
    assert eng.occupancy()["shelf-1"][0] == before


def test_occlusion_clears_partial_confirmation():
    """Readings either side of a passer-by are not stitched together."""
    eng = engine(stability_samples=2)
    settle(eng, full_shelf(), BASE)
    t = BASE + timedelta(seconds=300)
    eng.update([], t)  # first empty reading, pending
    eng.update([detection(0.2, 0.3, 0.8, 0.7, class_name="person")], t + timedelta(seconds=60))
    # Pending was discarded, so one further empty reading is not yet enough.
    assert eng.update([], t + timedelta(seconds=120)) == []


def test_level_events_are_not_repeated_while_the_level_holds():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    first = settle(eng, [], BASE + timedelta(seconds=300))
    assert InventoryObservationKind.SHELF_EMPTY in [o.kind for o in first]
    later = settle(eng, [], BASE + timedelta(seconds=3000))
    assert InventoryObservationKind.SHELF_EMPTY not in [o.kind for o in later]


def test_cooldown_suppresses_repeat_observations():
    eng = engine(cooldown_seconds=10_000)
    settle(eng, full_shelf(), BASE)
    settle(eng, [], BASE + timedelta(seconds=300))
    settle(eng, full_shelf(), BASE + timedelta(seconds=600))
    obs = settle(eng, [], BASE + timedelta(seconds=900))
    assert InventoryObservationKind.SHELF_EMPTY not in [o.kind for o in obs]


def test_disabled_shelf_is_not_read():
    eng = engine(shelves=[shelf(monitoring_enabled=False)])
    assert eng.update(full_shelf(), BASE) == []


def test_read_ignores_the_sample_interval():
    eng = engine()
    eng.update(full_shelf(), BASE)
    readings = eng.read(full_shelf(), BASE + timedelta(seconds=1))
    assert len(readings) == 1
    assert readings[0].level is OccupancyLevel.FULL


def test_read_does_not_disturb_the_event_timeline():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    before = eng.occupancy()["shelf-1"]
    eng.read([], BASE + timedelta(seconds=1))
    assert eng.occupancy()["shelf-1"] == before


def test_set_shelves_preserves_state_for_surviving_shelves():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    stable = eng.occupancy()["shelf-1"][0]
    eng.set_shelves([shelf(name="Aisle 3 top, nudged")])
    assert eng.occupancy()["shelf-1"][0] == stable


def test_set_shelves_drops_state_for_removed_shelves():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    eng.set_shelves([])
    assert eng.occupancy() == {}


def test_reset_forgets_stable_state():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    eng.reset()
    assert eng.occupancy()["shelf-1"][0] is None


def test_nearby_tracks_are_recorded_as_context_only():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    t = BASE + timedelta(seconds=300)
    obs = []
    for i in range(2):
        obs += eng.update([], t + timedelta(seconds=60 * i), nearby_track_keys=("cam-1:s:7",))
    assert obs
    assert obs[0].nearby_track_keys == ("cam-1:s:7",)


def test_observation_serialises_for_transport():
    eng = engine()
    settle(eng, full_shelf(), BASE)
    obs = settle(eng, [], BASE + timedelta(seconds=300))
    payload = obs[0].to_dict()
    assert payload["shelf_id"] == "shelf-1"
    assert payload["delta"] < 0
    assert payload["level_after"] == "EMPTY"
    assert isinstance(payload["timestamp"], str)


# ----------------------------------------------------------------- honesty


def test_observation_vocabulary_makes_no_accusation():
    """docs/PRIVACY.md: the engine reports shelves, never motives."""
    forbidden = ("THEFT", "STEAL", "SHOPLIFT", "CRIMINAL", "GUILTY", "SUSPECT")
    for kind in InventoryObservationKind:
        assert not any(word in kind.value.upper() for word in forbidden)


def test_vision_and_backend_inventory_vocabularies_agree():
    from app.models.enums import InventoryEventType
    from app.models.enums import OccupancyLevel as BackendLevel

    assert {k.value for k in InventoryObservationKind} == {e.value for e in InventoryEventType}
    assert {lvl.value for lvl in OccupancyLevel} == {lvl.value for lvl in BackendLevel}
