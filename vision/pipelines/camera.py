"""The per-camera pipeline.

One :class:`CameraPipeline` owns one camera end to end::

    VideoSource -> FrameReader -> DetectionEngine -> Tracker
                -> ZoneEngine + LineCounter -> EventEngine -> sinks

It is the only place those pieces meet, and it holds no database connection
and no HTTP client. Everything it needs arrives as configuration, and
everything it produces leaves through an :class:`~vision.events.EventSink`.
That is what lets the identical pipeline run on an offline edge box and in the
cloud.

Threading model: one pipeline runs on one thread. Multiple cameras mean
multiple pipelines, which share a single detection engine (the model is loaded
once and its inference is serialised internally).
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from vision.cameras import FrameReader, SourceState, VideoSource
from vision.config import VisionConfig
from vision.counting import CrossingObservation, LineCounter, LineDefinition
from vision.detection import DetectionEngine, DetectionError
from vision.events import (
    EventEngine,
    EventType,
    OperatingSchedule,
    RuleContext,
    Severity,
    VisionEvent,
)
from vision.inventory import InventoryEngine, InventoryObservation, ShelfDefinition
from vision.tracking import ByteTracker, build_tracker
from vision.types import Detection, Frame, TrackedObject
from vision.zones import ZoneDefinition, ZoneEngine, ZoneObservation


class PipelineState(StrEnum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


@dataclass
class PipelineConfig:
    """Everything one camera pipeline needs to run.

    Assembled by the backend from database rows; the vision engine never
    queries anything itself.
    """

    camera_id: str
    store_id: str | None = None
    organization_id: str | None = None
    camera_name: str = ""
    zones: list[ZoneDefinition] = field(default_factory=list)
    lines: list[LineDefinition] = field(default_factory=list)
    #: Shelves this camera watches. Empty on a pure-security camera, which is
    #: the common case -- the inventory engine then costs nothing per frame.
    shelves: list[ShelfDefinition] = field(default_factory=list)
    schedule: OperatingSchedule | None = None
    #: Per-camera override of the global confidence threshold.
    confidence_threshold: float | None = None
    #: Restrict detection to these class names. Empty = all.
    detect_classes: tuple[str, ...] = ()
    #: Emit a PERSON_DETECTED event on a track's first confirmation. Off by
    #: default: it is high volume and rarely actionable on its own.
    emit_person_detected: bool = False


@dataclass(slots=True)
class PipelineStats:
    """Live health, surfaced on ``/system/vision/metrics``."""

    frames_processed: int = 0
    detections: int = 0
    tracks_seen: int = 0
    events_emitted: int = 0
    errors: int = 0
    started_at: datetime | None = None
    last_frame_at: datetime | None = None
    last_error: str | None = None
    #: Rolling mean of end-to-end per-frame processing time.
    mean_frame_ms: float = 0.0

    def record_frame(self, duration_ms: float, detections: int, events: int) -> None:
        self.frames_processed += 1
        self.detections += detections
        self.events_emitted += events
        # Exponential moving average: cheap, and reflects recent conditions
        # rather than being dragged around by a slow start hours ago.
        alpha = 0.05
        self.mean_frame_ms = (
            duration_ms
            if self.frames_processed == 1
            else (1 - alpha) * self.mean_frame_ms + alpha * duration_ms
        )

    @property
    def uptime_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return (datetime.now(UTC) - self.started_at).total_seconds()

    @property
    def processed_fps(self) -> float:
        elapsed = self.uptime_seconds
        return self.frames_processed / elapsed if elapsed > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "frames_processed": self.frames_processed,
            "detections": self.detections,
            "tracks_seen": self.tracks_seen,
            "events_emitted": self.events_emitted,
            "errors": self.errors,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "processed_fps": round(self.processed_fps, 2),
            "mean_frame_ms": round(self.mean_frame_ms, 2),
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class FrameResult:
    """Everything one frame produced. Used by the live view and by tests."""

    frame: Frame
    detections: list[Detection]
    tracks: list[TrackedObject]
    zone_observations: list[ZoneObservation]
    crossings: list[CrossingObservation]
    inventory_observations: list[InventoryObservation]
    events: list[VisionEvent]
    duration_ms: float


class CameraPipeline:
    """Runs detection, tracking and rules for one camera."""

    def __init__(
        self,
        config: PipelineConfig,
        source: VideoSource,
        detector: DetectionEngine,
        event_engine: EventEngine,
        *,
        vision_config: VisionConfig | None = None,
        tracker: ByteTracker | None = None,
        on_state_change: Callable[[PipelineState, str | None], None] | None = None,
        on_frame: Callable[[FrameResult], None] | None = None,
    ) -> None:
        self.config = config
        self.source = source
        self.detector = detector
        self.event_engine = event_engine
        self.vision_config = vision_config or VisionConfig()
        self.on_state_change = on_state_change
        self.on_frame = on_frame

        self.state = PipelineState.IDLE
        self.stats = PipelineStats()

        self.tracker = tracker or build_tracker(
            self.vision_config.tracker_type,
            camera_id=config.camera_id,
            max_age=self.vision_config.track_max_age,
            min_hits=self.vision_config.track_min_hits,
            iou_threshold=self.vision_config.track_iou_threshold,
            low_confidence_threshold=self.vision_config.track_low_confidence_threshold,
        )
        self.zone_engine = ZoneEngine(config.zones, camera_id=config.camera_id)
        self.line_counter = LineCounter(config.lines, camera_id=config.camera_id)
        self.inventory_engine = InventoryEngine(config.shelves, camera_id=config.camera_id)

        self.reader = FrameReader(
            source,
            target_fps=self.vision_config.target_fps,
            max_reconnect_attempts=self.vision_config.max_reconnect_attempts,
            reconnect_initial_delay=self.vision_config.reconnect_initial_delay,
            reconnect_max_delay=self.vision_config.reconnect_max_delay,
            queue_size=self.vision_config.frame_queue_size,
            on_state_change=self._on_source_state,
        )

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen_tracks: set[str] = set()

    # ------------------------------------------------------------------ state
    def _set_state(self, state: PipelineState, detail: str | None = None) -> None:
        if state == self.state:
            return
        self.state = state
        if self.on_state_change is not None:
            # A callback must never take the pipeline down with it.
            with contextlib.suppress(Exception):
                self.on_state_change(state, detail)

    def _on_source_state(self, source_state: SourceState, detail: str | None) -> None:
        mapping = {
            SourceState.CONNECTING: PipelineState.STARTING,
            SourceState.CONNECTED: PipelineState.RUNNING,
            SourceState.RECONNECTING: PipelineState.RECONNECTING,
            SourceState.ERROR: PipelineState.ERROR,
        }
        mapped = mapping.get(source_state)
        if mapped is not None:
            self._set_state(mapped, detail)

        if source_state == SourceState.RECONNECTING:
            # A reconnect means a gap in the video. Track ids from before the
            # gap cannot be trusted to refer to the same people afterwards, so
            # the tracker starts a new session rather than silently merging
            # two different sets of ids.
            self.tracker.reset(new_session=True)
            self.event_engine.reset()

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Run the pipeline on a background thread."""
        if self._thread is not None:
            raise RuntimeError("pipeline already running")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run, name=f"pipeline-{self.config.camera_id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the pipeline to stop and wait for it."""
        self._set_state(PipelineState.STOPPING)
        self._stop.set()
        self.reader.stop()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._set_state(PipelineState.STOPPED)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run(self) -> None:
        """Process frames until the source ends or ``stop`` is called."""
        self._set_state(PipelineState.STARTING)
        self.stats.started_at = datetime.now(UTC)

        try:
            if not self.detector.is_loaded:
                self.detector.load()
        except Exception as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)[:200]
            self._set_state(PipelineState.ERROR, str(exc))
            self._emit_system_event(
                EventType.PIPELINE_ERROR,
                Severity.HIGH,
                f"Camera '{self.config.camera_name or self.config.camera_id}' could not "
                "start: the detection model failed to load.",
            )
            return

        try:
            for frame in self.reader.read_frames():
                if self._stop.is_set():
                    break
                self.process_frame(frame)
        # Never let one camera take the whole process down.
        except Exception as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)[:200]
            self._set_state(PipelineState.ERROR, str(exc))
            self._emit_system_event(
                EventType.PIPELINE_ERROR,
                Severity.HIGH,
                f"Camera '{self.config.camera_name or self.config.camera_id}' stopped "
                "unexpectedly.",
            )
        finally:
            self.event_engine.flush()
            if self.state != PipelineState.ERROR:
                self._set_state(PipelineState.STOPPED)

    # ----------------------------------------------------------------- frames
    def process_frame(self, frame: Frame) -> FrameResult:
        """Run one frame through the whole pipeline.

        Public and synchronous so a test can drive the pipeline frame by frame
        with no threads and no timing, which is the only way to assert exactly
        which events a given sequence produces.
        """
        started = time.perf_counter()

        try:
            detections = self.detector.detect(frame)
        except DetectionError as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)[:200]
            # One bad frame is not a reason to stop: skip it and carry on.
            detections = []

        if self.config.detect_classes:
            allowed = set(self.config.detect_classes)
            detections = [d for d in detections if d.class_name in allowed]

        tracks = self.tracker.update(detections, frame.timestamp)

        for track in tracks:
            if track.track_key not in self._seen_tracks:
                self._seen_tracks.add(track.track_key)
                self.stats.tracks_seen += 1

        zone_observations = self.zone_engine.update(tracks, frame.timestamp)
        crossings = self.line_counter.update(tracks, frame.timestamp)
        # Shelves are sampled on their own interval, so this is a cheap
        # no-op on the vast majority of frames.
        inventory_observations = self.inventory_engine.update(
            detections, frame.timestamp, tracks=tracks
        )

        context = RuleContext(
            camera_id=self.config.camera_id,
            store_id=self.config.store_id,
            now=frame.timestamp,
            tracks=tracks,
            zone_observations=zone_observations,
            crossings=crossings,
            zones=self.zone_engine.zones,
            dwell_seconds=self._dwell_map(frame.timestamp),
            zone_occupancy={
                zone_id: occupancy.count
                for zone_id, occupancy in self.zone_engine.occupancy().items()
            },
            schedule=self.config.schedule,
        )
        events = self.event_engine.process(context)

        duration_ms = (time.perf_counter() - started) * 1000
        self.stats.record_frame(duration_ms, len(detections), len(events))
        self.stats.last_frame_at = frame.timestamp

        result = FrameResult(
            frame=frame,
            detections=detections,
            tracks=tracks,
            zone_observations=zone_observations,
            crossings=crossings,
            inventory_observations=inventory_observations,
            events=events,
            duration_ms=duration_ms,
        )
        if self.on_frame is not None:
            # A live viewer must not stop frames being processed.
            with contextlib.suppress(Exception):
                self.on_frame(result)
        return result

    def _dwell_map(self, now: datetime) -> dict[tuple[str, str], float]:
        """Live dwell time for every track currently inside a zone."""
        dwell: dict[tuple[str, str], float] = {}
        for zone_id, occupancy in self.zone_engine.occupancy().items():
            for track_key in occupancy.track_keys:
                seconds = self.zone_engine.dwell_seconds(zone_id, track_key, now)
                if seconds is not None:
                    dwell[(zone_id, track_key)] = seconds
        return dwell

    def _emit_system_event(self, event_type: EventType, severity: Severity, summary: str) -> None:
        self.event_engine.dispatch(
            [
                VisionEvent(
                    event_type=event_type,
                    severity=severity,
                    camera_id=self.config.camera_id,
                    store_id=self.config.store_id,
                    occurred_at=datetime.now(UTC),
                    rule_name="pipeline",
                    summary=summary,
                )
            ],
            datetime.now(UTC),
        )

    # ------------------------------------------------------------ maintenance
    def apply_config(self, config: PipelineConfig) -> None:
        """Adopt new zones, lines or schedule without restarting the camera.

        An operator redrawing a zone should not cost a video gap, and a
        restart would reset every track id mid-shift.
        """
        self.config = config
        self.zone_engine.set_zones(config.zones)
        self.line_counter.set_lines(config.lines)
        self.inventory_engine.set_shelves(config.shelves)

    def prune(self, now: datetime | None = None) -> None:
        """Drop state for tracks that are long gone."""
        moment = now or datetime.now(UTC)
        self.zone_engine.prune(moment)
        self.line_counter.prune(moment)
        if len(self._seen_tracks) > 50_000:
            self._seen_tracks.clear()

    def metrics(self) -> dict:
        """Everything a diagnostics endpoint needs about this camera."""
        return {
            "camera_id": self.config.camera_id,
            "camera_name": self.config.camera_name,
            "state": self.state.value,
            "pipeline": self.stats.to_dict(),
            "source": self.source.stats.to_dict(),
            "reader": self.reader.stats.to_dict(),
            "detector": self.detector.stats.to_dict(),
            "events": self.event_engine.stats.to_dict(),
            "device": self.detector.device,
            "queue_depth": self.reader.queue_depth,
            "active_tracks": len(self.tracker.active_tracks),
            "line_counts": {
                line_id: counts.to_dict() for line_id, counts in self.line_counter.totals().items()
            },
            "zone_occupancy": {
                zone_id: occupancy.count
                for zone_id, occupancy in self.zone_engine.occupancy().items()
            },
        }

    def __repr__(self) -> str:
        return f"<CameraPipeline {self.config.camera_id} {self.state}>"
