"""Runs vision pipelines for cameras and feeds their events into the system.

This is the seam between the two halves of the product. The vision engine
knows nothing about the database; the database layer knows nothing about
detection. This module owns the translation in both directions:

* **down** -- reads a camera, its zones, its lines and its store's opening
  hours, and builds the plain :class:`~vision.pipelines.PipelineConfig` the
  engine understands;
* **up** -- receives :class:`~vision.events.VisionEvent` objects from the
  engine and persists, escalates and broadcasts them.

Each pipeline runs on its own thread with its own database session. A single
detection engine is shared across every pipeline in the process: the model is
several hundred megabytes and loading one per camera would exhaust an edge
box's memory for no benefit.

**Self-test cameras.** A camera with ``source_kind = SYNTHETIC`` is paired with
a detector driven by the synthetic source's own ground truth rather than with
YOLO. Running a neural network over generated rectangles would detect nothing,
which makes it useless for what the mode is actually for: letting an installer
verify the whole chain -- zones, rules, events, alerts, WebSocket, dashboard --
before a real camera is mounted, or confirming after an outage that the chain
is still intact.

Those events are genuine outputs of the real rule engine, but the *detections*
behind them are generated. Every one is stamped ``synthetic: true`` in its
metadata so nothing downstream can mistake a self-test for an observation of
the real world.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import AppError, ConflictError, ErrorCode, NotFoundError
from app.core.logging import get_logger
from app.db.session import session_scope
from app.models import Camera, CountingLine, Shelf, Store, Zone
from app.models.enums import CameraStatus, VideoSourceKind
from app.services.cameras import build_video_source, record_status
from app.services.events import ingest_vision_event
from vision.cameras import VideoSourceError
from vision.config import VisionConfig
from vision.counting import LineDefinition
from vision.detection import DetectionEngine, ModelLoadError, build_engine
from vision.events import CallbackSink, EventEngine, default_rules
from vision.inventory import ShelfDefinition
from vision.pipelines import CameraPipeline, PipelineConfig, PipelineState
from vision.types import ReferencePoint
from vision.zones import GeometryError, ZoneDefinition, ZoneKind

log = get_logger(__name__)


class PipelineAlreadyRunningError(ConflictError):
    code = ErrorCode.CAMERA_ALREADY_RUNNING
    message = "A pipeline is already running for this camera."


class PipelineStartError(AppError):
    status_code = 502
    code = ErrorCode.STREAM_UNAVAILABLE
    message = "The camera pipeline could not be started."


class ModelUnavailableError(AppError):
    """The detection model could not be loaded.

    A distinct, actionable error rather than a generic 500: "the weights file
    is missing" is something an operator can fix in a minute, and burying it
    behind an incident id turns a two-minute fix into a support ticket.
    """

    status_code = 503
    code = ErrorCode.MODEL_LOAD_FAILED
    message = "The detection model is not available."


# ---------------------------------------------------------------------------
# Configuration translation
# ---------------------------------------------------------------------------


def build_zone_definitions(zones: list[Zone], *, default_loitering: float) -> list[ZoneDefinition]:
    """Translate zone rows into the engine's plain definitions.

    Invalid geometry is skipped with a loud log rather than aborting the whole
    pipeline: one badly drawn zone must not blind a camera entirely.
    """
    definitions: list[ZoneDefinition] = []
    for zone in zones:
        try:
            definitions.append(
                ZoneDefinition(
                    zone_id=str(zone.id),
                    name=zone.name,
                    polygon=zone.polygon,
                    kind=ZoneKind(zone.zone_type.value),
                    reference_point=ReferencePoint(zone.reference_point),
                    trigger_classes=frozenset(zone.trigger_classes or {"person"}),
                    loitering_threshold_seconds=(
                        float(zone.loitering_threshold_seconds)
                        if zone.loitering_threshold_seconds is not None
                        else default_loitering
                    ),
                    dwell_threshold_seconds=(
                        float(zone.dwell_threshold_seconds)
                        if zone.dwell_threshold_seconds is not None
                        else None
                    ),
                    after_hours_monitoring=zone.after_hours_monitoring,
                    max_occupancy=zone.max_occupancy,
                    is_active=zone.is_active,
                )
            )
        except Exception as exc:
            log.error(
                "zone_definition_invalid",
                zone_id=str(zone.id),
                zone_name=zone.name,
                error=str(exc)[:200],
                detail="zone skipped; the camera keeps running",
            )
    return definitions


def build_line_definitions(
    lines: list[CountingLine], *, default_cooldown: float
) -> list[LineDefinition]:
    definitions: list[LineDefinition] = []
    for line in lines:
        try:
            definitions.append(
                LineDefinition(
                    line_id=str(line.id),
                    name=line.name,
                    point_a=tuple(line.point_a),
                    point_b=tuple(line.point_b),
                    invert_direction=line.invert_direction,
                    reference_point=ReferencePoint(line.reference_point),
                    trigger_classes=frozenset(line.trigger_classes or {"person"}),
                    cooldown_seconds=line.cooldown_seconds or default_cooldown,
                    is_active=line.is_active,
                )
            )
        except Exception as exc:
            log.error(
                "line_definition_invalid",
                line_id=str(line.id),
                error=str(exc)[:200],
                detail="line skipped; the camera keeps running",
            )
    return definitions


def build_shelf_definitions(shelves: list[Shelf]) -> list[ShelfDefinition]:
    """Translate shelf rows into the engine's plain definitions.

    A shelf with unusable geometry is skipped with a loud log rather than
    aborting the pipeline, for the same reason zones are: one badly drawn
    shelf must not blind a camera to everything else it watches.
    """
    definitions: list[ShelfDefinition] = []
    for shelf in shelves:
        if shelf.polygon is None:
            log.warning("shelf_skipped_no_polygon", shelf_id=str(shelf.id))
            continue
        try:
            definitions.append(
                ShelfDefinition(
                    shelf_id=str(shelf.id),
                    name=shelf.name,
                    polygon=shelf.polygon,
                    camera_id=str(shelf.camera_id) if shelf.camera_id else "camera",
                    product_classes=frozenset(
                        product.detection_class_name
                        for product in shelf.products
                        if product.detection_class_name
                    ),
                    baseline_occupancy=shelf.baseline_occupancy,
                    low_threshold=shelf.low_threshold,
                    empty_threshold=shelf.empty_threshold,
                    sample_interval_seconds=float(shelf.sample_interval_seconds),
                    is_active=shelf.is_active,
                    monitoring_enabled=shelf.monitoring_enabled,
                )
            )
        except (GeometryError, ValueError) as exc:
            log.warning(
                "shelf_skipped_invalid",
                shelf_id=str(shelf.id),
                error=str(exc)[:200],
            )
    return definitions


def build_pipeline_config(session, camera: Camera) -> PipelineConfig:
    """Assemble everything the engine needs for one camera."""
    settings = get_settings()

    store = session.scalar(
        select(Store)
        .where(Store.id == camera.store_id)
        .options(selectinload(Store.operating_hours))
    )
    zones = list(
        session.scalars(
            select(Zone).where(
                Zone.camera_id == camera.id,
                Zone.deleted_at.is_(None),
                Zone.is_active.is_(True),
            )
        ).all()
    )
    lines = list(
        session.scalars(
            select(CountingLine).where(
                CountingLine.camera_id == camera.id,
                CountingLine.deleted_at.is_(None),
                CountingLine.is_active.is_(True),
            )
        ).all()
    )

    shelves = list(
        session.scalars(
            select(Shelf)
            .where(
                Shelf.camera_id == camera.id,
                Shelf.deleted_at.is_(None),
                Shelf.is_active.is_(True),
            )
            .options(selectinload(Shelf.products))
        ).all()
    )

    schedule = None
    if store is not None and store.operating_hours:
        from app.services.stores import build_schedule

        schedule = build_schedule(store)

    return PipelineConfig(
        camera_id=str(camera.id),
        store_id=str(camera.store_id),
        organization_id=str(camera.organization_id),
        camera_name=camera.name,
        zones=build_zone_definitions(
            zones, default_loitering=float(settings.LOITERING_THRESHOLD_SECONDS)
        ),
        lines=build_line_definitions(
            lines, default_cooldown=float(settings.LINE_CROSSING_COOLDOWN_SECONDS)
        ),
        shelves=build_shelf_definitions(shelves),
        schedule=schedule,
        confidence_threshold=camera.confidence_threshold,
        detect_classes=tuple(camera.detect_classes or ()),
    )


def build_vision_config(camera: Camera) -> VisionConfig:
    """The engine settings for one camera, with per-camera overrides applied."""
    settings = get_settings()
    return VisionConfig(
        model_path=settings.YOLO_MODEL_PATH,
        confidence_threshold=(camera.confidence_threshold or settings.YOLO_CONFIDENCE_THRESHOLD),
        iou_threshold=settings.YOLO_IOU_THRESHOLD,
        device=settings.YOLO_DEVICE,
        image_size=settings.YOLO_IMAGE_SIZE,
        classes=tuple(camera.detect_classes or ()),
        tracker_type=settings.TRACKER_TYPE,
        target_fps=camera.target_fps or settings.VISION_FPS,
        frame_queue_size=settings.VISION_FRAME_QUEUE_SIZE,
        loitering_threshold_seconds=float(settings.LOITERING_THRESHOLD_SECONDS),
        line_crossing_cooldown_seconds=float(settings.LINE_CROSSING_COOLDOWN_SECONDS),
        rtsp_transport=camera.transport or "tcp",
    )


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


@dataclass
class RunningPipeline:
    """A pipeline this process is running."""

    camera_id: uuid.UUID
    organization_id: uuid.UUID
    pipeline: CameraPipeline
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    events_ingested: int = 0
    events_deduplicated: int = 0
    ingest_failures: int = 0


class PipelineManager:
    """Owns the pipelines running in this process.

    Deliberately process-local. Multi-process orchestration -- deciding which
    worker owns which camera -- is a deployment concern, and pretending to
    solve it here would produce two workers fighting over one camera.
    """

    def __init__(self) -> None:
        self._pipelines: dict[uuid.UUID, RunningPipeline] = {}
        self._lock = threading.Lock()
        self._detector: DetectionEngine | None = None

    # ---------------------------------------------------------- detection
    @staticmethod
    def _synthetic_detector(source, config: VisionConfig) -> DetectionEngine:
        """A detector that reports the synthetic source's ground truth.

        Not a mock of YOLO: the boxes are exactly where the source drew its
        objects, which is precisely the property that makes the self-test
        deterministic and its expected outcome knowable.
        """
        from vision.detection import ScriptedDetectionEngine

        script = {
            frame_index: [
                ("person", 0.92, source.object_bbox_at(object_index, frame_index))
                for object_index in range(len(source.objects))
            ]
            for frame_index in range(source.frame_count)
        }
        return ScriptedDetectionEngine(config, script=script)

    def detector(self, config: VisionConfig) -> DetectionEngine:
        """The shared detection engine, loaded once per process.

        Sharing matters: the model is hundreds of megabytes, and one instance
        per camera would exhaust an edge box for no accuracy gain. Inference
        is serialised inside the engine.
        """
        if self._detector is None:
            engine = build_engine(config)
            try:
                engine.load()
            except ModelLoadError as exc:
                log.error(
                    "model_load_failed",
                    model_path=config.model_path,
                    device=config.resolved_device(),
                    error=str(exc)[:200],
                )
                raise ModelUnavailableError(
                    str(exc), details={"model_path": config.model_path}
                ) from exc
            self._detector = engine
            log.info(
                "detection_engine_loaded",
                device=self._detector.device,
                classes=len(self._detector.class_names),
                model=config.model_path,
            )
        return self._detector

    def preload_detector(self, config: VisionConfig | None = None) -> dict:
        """Load and warm the model before any camera needs it.

        The first inference is several times slower than steady state. Paying
        that at startup keeps it off the first real frame, which is exactly
        when an operator is watching a camera come online.
        """
        settings = get_settings()
        detector = self.detector(
            config
            or VisionConfig(
                model_path=settings.YOLO_MODEL_PATH,
                device=settings.YOLO_DEVICE,
                image_size=settings.YOLO_IMAGE_SIZE,
            )
        )
        warmup_ms = detector.warmup(runs=2)
        return {
            "device": detector.device,
            "classes": len(detector.class_names),
            "warmup_ms": round(warmup_ms, 1),
        }

    # ----------------------------------------------------------- lifecycle
    def is_running(self, camera_id: uuid.UUID) -> bool:
        with self._lock:
            running = self._pipelines.get(camera_id)
        return running is not None and running.pipeline.is_running

    def start(self, session, camera: Camera) -> RunningPipeline:
        """Start a pipeline for a camera."""
        if self.is_running(camera.id):
            raise PipelineAlreadyRunningError(details={"camera_id": str(camera.id)})
        if not camera.is_enabled:
            raise PipelineStartError(
                "This camera is disabled. Enable it before starting the pipeline.",
                details={"camera_id": str(camera.id)},
            )

        pipeline_config = build_pipeline_config(session, camera)
        vision_config = build_vision_config(camera)
        try:
            source = build_video_source(camera, target_fps=camera.target_fps)
        except VideoSourceError as exc:
            raise PipelineStartError(str(exc), details={"camera_id": str(camera.id)}) from exc

        organization_id = camera.organization_id
        camera_id = camera.id
        store_id = camera.store_id

        is_self_test = camera.source_kind == VideoSourceKind.SYNTHETIC
        running_ref: dict[str, RunningPipeline | None] = {"value": None}

        def on_event(vision_event) -> None:
            """Persist an event from the engine.

            Runs on the pipeline thread, so it opens its own session. Failures
            are recorded and swallowed: losing one event is bad, but taking
            down a camera because the database blinked is worse.
            """
            try:
                if is_self_test:
                    # Stamped so nothing downstream can read a self-test as an
                    # observation of the real world.
                    vision_event.metadata["synthetic"] = True
                with session_scope() as worker_session:
                    event, _alert = ingest_vision_event(
                        worker_session, vision_event, organization_id=organization_id
                    )
                running = running_ref["value"]
                if running is not None:
                    if event is None:
                        running.events_deduplicated += 1
                    else:
                        running.events_ingested += 1
            except Exception as exc:
                running = running_ref["value"]
                if running is not None:
                    running.ingest_failures += 1
                log.error(
                    "event_ingest_failed",
                    camera_id=str(camera_id),
                    event_type=str(vision_event.event_type),
                    error=str(exc)[:200],
                )

        def on_state_change(state: PipelineState, detail: str | None) -> None:
            """Mirror the pipeline's state onto the camera row and dashboards."""
            mapping = {
                PipelineState.RUNNING: CameraStatus.ONLINE,
                PipelineState.STARTING: CameraStatus.CONNECTING,
                PipelineState.RECONNECTING: CameraStatus.CONNECTING,
                PipelineState.ERROR: CameraStatus.ERROR,
                PipelineState.STOPPED: CameraStatus.OFFLINE,
            }
            status = mapping.get(state)
            if status is None:
                return
            try:
                with session_scope() as worker_session:
                    record_status(worker_session, camera_id, status, error=detail)
                self._broadcast_status(camera_id, organization_id, store_id, status.value, detail)
            except Exception as exc:
                log.warning(
                    "camera_status_update_failed",
                    camera_id=str(camera_id),
                    error=str(exc)[:200],
                )

        event_engine = EventEngine(
            rules=default_rules(
                loitering_threshold_seconds=vision_config.loitering_threshold_seconds
            ),
            sinks=[CallbackSink(on_event, name="database")],
        )

        detector = (
            self._synthetic_detector(source, vision_config)
            if is_self_test
            else self.detector(vision_config)
        )

        pipeline = CameraPipeline(
            pipeline_config,
            source,
            detector,
            event_engine,
            vision_config=vision_config,
            on_state_change=on_state_change,
        )

        running = RunningPipeline(
            camera_id=camera.id,
            organization_id=organization_id,
            pipeline=pipeline,
        )
        running_ref["value"] = running

        with self._lock:
            self._pipelines[camera.id] = running

        pipeline.start()
        log.info(
            "pipeline_started",
            camera_id=str(camera.id),
            camera_name=camera.name,
            source_kind=camera.source_kind.value,
            self_test=is_self_test,
            device=detector.device,
        )
        return running

    def stop(self, camera_id: uuid.UUID, *, timeout: float = 10.0) -> bool:
        """Stop a pipeline. Returns whether one was running."""
        with self._lock:
            running = self._pipelines.pop(camera_id, None)
        if running is None:
            return False

        running.pipeline.stop(timeout=timeout)
        try:
            with session_scope() as session:
                record_status(session, camera_id, CameraStatus.OFFLINE)
        except Exception:
            pass

        log.info(
            "pipeline_stopped",
            camera_id=str(camera_id),
            events_ingested=running.events_ingested,
            frames=running.pipeline.stats.frames_processed,
        )
        return True

    def stop_all(self, *, timeout: float = 10.0) -> int:
        with self._lock:
            camera_ids = list(self._pipelines)
        for camera_id in camera_ids:
            self.stop(camera_id, timeout=timeout)
        return len(camera_ids)

    # ------------------------------------------------------------ reporting
    def get(self, camera_id: uuid.UUID) -> RunningPipeline | None:
        with self._lock:
            return self._pipelines.get(camera_id)

    def running_camera_ids(self) -> list[uuid.UUID]:
        with self._lock:
            return list(self._pipelines)

    def metrics(self, organization_id: uuid.UUID | None = None) -> dict:
        """Per-camera performance, for ``/system/vision/metrics``."""
        with self._lock:
            pipelines = list(self._pipelines.values())

        cameras = [
            {
                **running.pipeline.metrics(),
                "events_ingested": running.events_ingested,
                "events_deduplicated": running.events_deduplicated,
                "ingest_failures": running.ingest_failures,
                "started_at": running.started_at.isoformat(),
            }
            for running in pipelines
            if organization_id is None or running.organization_id == organization_id
        ]
        return {
            "pipelines_running": len(cameras),
            "detector_loaded": self._detector is not None,
            "device": self._detector.device if self._detector else None,
            "cameras": cameras,
        }

    def _broadcast_status(
        self,
        camera_id: uuid.UUID,
        organization_id: uuid.UUID,
        store_id: uuid.UUID | None,
        status: str,
        detail: str | None,
    ) -> None:
        try:
            from app.websocket.broadcast import publish
            from app.websocket.manager import camera_status_message

            publish(
                camera_status_message(camera_id, organization_id, store_id, status, error=detail)
            )
        except Exception:
            # Realtime status is a convenience; the camera row is the truth.
            pass


#: Process-wide manager.
manager = PipelineManager()


def start_camera_pipeline(session, scope, camera_id: uuid.UUID) -> dict:
    """Start a camera's pipeline, checking permission and tenancy first."""
    from app.core.permissions import Permission
    from app.services.cameras import get_camera

    scope.require(Permission.CAMERA_CONTROL)
    camera = get_camera(session, scope, camera_id)
    scope.require_store(camera.store_id)

    if camera.source_kind == VideoSourceKind.RTSP and not camera.host:
        raise PipelineStartError(
            "This camera has no host address configured.",
            details={"camera_id": str(camera_id)},
        )

    running = manager.start(session, camera)
    return {
        "camera_id": str(camera_id),
        "state": running.pipeline.state.value,
        "started_at": running.started_at.isoformat(),
    }


def stop_camera_pipeline(session, scope, camera_id: uuid.UUID) -> dict:
    from app.core.permissions import Permission
    from app.services.cameras import get_camera

    scope.require(Permission.CAMERA_CONTROL)
    camera = get_camera(session, scope, camera_id)
    scope.require_store(camera.store_id)

    stopped = manager.stop(camera_id)
    if not stopped:
        raise NotFoundError(
            "No pipeline is running for this camera in this process.",
            details={"camera_id": str(camera_id)},
        )
    return {"camera_id": str(camera_id), "state": "STOPPED"}
