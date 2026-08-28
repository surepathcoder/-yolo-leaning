"""Milestone 15 -- the custom product model as a second detection pass.

The design rule under test: the product model's boxes feed the occupancy
engine (so a shelf is measured by real product class) but never the security
rules (a carton is not a person). It also runs on its own interval, not every
frame -- stock does not move frame to frame.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from vision.cameras import SyntheticVideoSource
from vision.config import VisionConfig
from vision.detection import ScriptedDetectionEngine
from vision.events import EventEngine, default_rules
from vision.inventory import ShelfDefinition
from vision.pipelines import CameraPipeline, PipelineConfig, ProductModelConfig
from vision.types import Frame
from vision.zones import ZoneDefinition, ZoneKind

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
SHELF_POLY = [(0.1, 0.4), (0.9, 0.4), (0.9, 0.7), (0.1, 0.7)]
RESTRICTED = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def frame(index: int) -> Frame:
    return Frame(
        image=np.zeros((480, 640, 3), dtype=np.uint8),
        index=index,
        timestamp=BASE + timedelta(seconds=index),
        camera_id="cam-1",
    )


def build_pipeline(
    *,
    main_script,
    product_script=None,
    shelves=None,
    zones=None,
    product_interval=5.0,
):
    vision_config = VisionConfig(model_path="scripted", target_fps=1.0)
    main = ScriptedDetectionEngine(vision_config, script=main_script)
    product = None
    product_model = None
    if product_script is not None:
        product = ScriptedDetectionEngine(
            vision_config, script=product_script, class_ids={"red_carton": 0, "blue_carton": 1}
        )
        product_model = ProductModelConfig(
            model_version_id="00000000-0000-0000-0000-0000000000aa",
            weights_path="scripted",
            class_names=("red_carton", "blue_carton"),
            interval_seconds=product_interval,
        )
    config = PipelineConfig(
        camera_id="cam-1",
        camera_name="Aisle 3",
        shelves=shelves or [],
        zones=zones or [],
        product_model=product_model,
    )
    pipeline = CameraPipeline(
        config,
        SyntheticVideoSource("cam-1"),
        main,
        EventEngine(rules=default_rules(), sinks=[]),
        vision_config=vision_config,
        product_detector=product,
    )
    return pipeline


def test_product_boxes_land_on_the_frame_result_and_feed_occupancy():
    shelf = ShelfDefinition(
        shelf_id="s1",
        name="Aisle 3 top",
        polygon=SHELF_POLY,
        product_classes=frozenset({"red_carton"}),
        sample_interval_seconds=0.0,
        baseline_occupancy=1.0,
    )
    # The main detector sees only a person; the product detector sees a carton
    # filling the shelf band.
    main = {0: [("person", 0.9, (0.80, 0.1, 0.95, 0.9))]}
    product = {0: [("red_carton", 0.9, (0.1, 0.4, 0.9, 0.7))]}

    pipeline = build_pipeline(main_script=main, product_script=product, shelves=[shelf])

    seen: list = []
    original = pipeline.inventory_engine.update

    def spy(detections, timestamp, **kwargs):
        seen.append([d.class_name for d in detections])
        return original(detections, timestamp, **kwargs)

    pipeline.inventory_engine.update = spy  # type: ignore[method-assign]
    result = pipeline.process_frame(frame(0))

    assert [d.class_name for d in result.product_detections] == ["red_carton"]
    # The occupancy engine received the person *and* the carton -- merged.
    assert seen == [["person", "red_carton"]]
    # And it measures the shelf as stocked, from the carton the security
    # detector never reported.
    reading = pipeline.inventory_engine.read(
        result.detections + result.product_detections, frame(0).timestamp
    )[0]
    assert reading.occupancy_ratio > 0.5


def test_the_product_model_runs_on_its_interval_not_every_frame():
    product = {i: [("red_carton", 0.9, (0.2, 0.4, 0.5, 0.6))] for i in range(10)}
    pipeline = build_pipeline(main_script={}, product_script=product, product_interval=5.0)

    ran_on = [i for i in range(10) if pipeline.process_frame(frame(i)).product_detections]
    # Frame 0 (first), then not again until >= 5 s later.
    assert ran_on == [0, 5]


def test_security_rules_never_see_a_product_box():
    zone = ZoneDefinition(
        zone_id="z1", name="Restricted", polygon=RESTRICTED, kind=ZoneKind.RESTRICTED
    )
    # A carton sits squarely in the restricted zone every frame.
    product = {i: [("red_carton", 0.95, (0.4, 0.4, 0.6, 0.6))] for i in range(5)}
    pipeline = build_pipeline(
        main_script={}, product_script=product, zones=[zone], product_interval=0.0
    )

    all_events = []
    for i in range(5):
        all_events.extend(pipeline.process_frame(frame(i)).events)

    assert all_events == [], f"a carton must not trip a security rule, got {all_events}"


def test_no_product_model_means_no_second_pass():
    pipeline = build_pipeline(main_script={0: [("person", 0.9, (0.1, 0.1, 0.2, 0.3))]})
    result = pipeline.process_frame(frame(0))
    assert result.product_detections == []
    assert pipeline.product_detector is None
