"""Detection engine abstraction.

The pipeline asks a :class:`DetectionEngine` "what is in this frame" and gets
back :class:`~vision.types.Detection` objects.  It never imports Ultralytics
directly, which means:

* the model can be swapped (a customer-specific product model, a newer YOLO)
  without touching the pipeline;
* tracking, zones and rules can be tested with a scripted engine, no weights
  and no GPU;
* a missing or broken model fails at one identifiable point.

Two implementations:

``YOLODetectionEngine``    Ultralytics YOLO -- production
``ScriptedDetectionEngine`` returns pre-programmed detections -- tests
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from vision.config import VisionConfig
from vision.types import BoundingBox, Detection, Frame, utcnow


class DetectionError(Exception):
    """Inference failed. The message is safe to show a user."""


class ModelLoadError(DetectionError):
    """The model could not be loaded."""


@dataclass(slots=True)
class EngineStats:
    """Rolling inference performance, surfaced on the metrics endpoint.

    A bounded deque of recent timings is kept rather than a running mean: an
    average over hours hides the thing that matters, which is whether
    inference is slow *now*.
    """

    frames_processed: int = 0
    detections_emitted: int = 0
    failures: int = 0
    recent_inference_ms: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    last_error: str | None = None

    def record(self, inference_ms: float, detection_count: int) -> None:
        self.frames_processed += 1
        self.detections_emitted += detection_count
        self.recent_inference_ms.append(inference_ms)

    @property
    def mean_inference_ms(self) -> float:
        if not self.recent_inference_ms:
            return 0.0
        return sum(self.recent_inference_ms) / len(self.recent_inference_ms)

    @property
    def p95_inference_ms(self) -> float:
        """95th percentile latency -- the number that predicts dropped frames."""
        if not self.recent_inference_ms:
            return 0.0
        ordered = sorted(self.recent_inference_ms)
        index = min(int(len(ordered) * 0.95), len(ordered) - 1)
        return ordered[index]

    @property
    def theoretical_max_fps(self) -> float:
        mean = self.mean_inference_ms
        return 1000.0 / mean if mean > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "frames_processed": self.frames_processed,
            "detections_emitted": self.detections_emitted,
            "failures": self.failures,
            "mean_inference_ms": round(self.mean_inference_ms, 2),
            "p95_inference_ms": round(self.p95_inference_ms, 2),
            "theoretical_max_fps": round(self.theoretical_max_fps, 2),
            "last_error": self.last_error,
        }


class DetectionEngine(ABC):
    """Turns a frame into detections."""

    def __init__(self, config: VisionConfig) -> None:
        self.config = config
        self.stats = EngineStats()

    @abstractmethod
    def load(self) -> None:
        """Load the model. Raises :class:`ModelLoadError` on failure."""

    @abstractmethod
    def detect(self, frame: Frame) -> list[Detection]:
        """Detect objects in one frame."""

    @property
    @abstractmethod
    def is_loaded(self) -> bool: ...

    @property
    def class_names(self) -> dict[int, str]:
        """Class id to name, as the loaded model defines it."""
        return {}

    @property
    def device(self) -> str:
        return self.config.resolved_device()

    def warmup(self, width: int = 640, height: int = 640, runs: int = 2) -> float:
        """Run inference on blank frames to pay one-off costs up front.

        The first inference is several times slower than steady state (kernel
        compilation, memory allocation, lazy imports).  Without a warmup that
        cost lands on the first real frame, which is exactly when a camera has
        just come online and an operator is watching.

        Returns the mean warmup latency in milliseconds.
        """
        if not self.is_loaded:
            self.load()
        blank = np.zeros((height, width, 3), dtype=np.uint8)
        timings: list[float] = []
        for index in range(runs):
            frame = Frame(image=blank, index=index, timestamp=utcnow(), camera_id="warmup")
            started = time.perf_counter()
            self.detect(frame)
            timings.append((time.perf_counter() - started) * 1000)
        # Warmup frames are not real work; keep them out of the statistics.
        self.stats = EngineStats()
        return sum(timings) / len(timings) if timings else 0.0

    def close(self) -> None:  # noqa: B027 - optional hook, not every engine holds resources
        """Release model resources. Safe to call more than once."""

    def __enter__(self) -> DetectionEngine:
        if not self.is_loaded:
            self.load()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class YOLODetectionEngine(DetectionEngine):
    """Ultralytics YOLO.

    Ultralytics is imported lazily inside :meth:`load` so that importing this
    module costs nothing: the API process needs the *type* to build a pipeline
    description, but only the worker needs the 2 GB of PyTorch behind it.

    Inference is serialised with a lock. A single model instance is not
    thread-safe, and one engine is intentionally shared across cameras: loading
    a separate copy per camera would multiply memory for no benefit.
    """

    def __init__(self, config: VisionConfig) -> None:
        super().__init__(config)
        self._model: Any = None
        self._class_names: dict[int, str] = {}
        self._lock = threading.Lock()
        self._resolved_device: str | None = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def class_names(self) -> dict[int, str]:
        return dict(self._class_names)

    @property
    def device(self) -> str:
        return self._resolved_device or self.config.resolved_device()

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            from ultralytics import YOLO
        except ImportError as exc:  # pragma: no cover
            raise ModelLoadError(
                "Ultralytics is not installed. Install vision/requirements.txt."
            ) from exc

        from pathlib import Path

        model_path = Path(self.config.model_path)
        if not model_path.exists():
            raise ModelLoadError(
                f"Model weights not found at '{model_path.name}'. "
                "Set YOLO_MODEL_PATH or download the weights."
            )

        device = self.config.resolved_device()
        try:
            model = YOLO(str(model_path))
            model.to(device)
        except Exception as exc:
            raise ModelLoadError(
                f"Failed to load the detection model on device '{device}'."
            ) from exc

        self._model = model
        self._resolved_device = device
        names = getattr(model, "names", {}) or {}
        self._class_names = {int(k): str(v) for k, v in names.items()}

    def _allowed_class_ids(self) -> list[int] | None:
        """Translate configured class *names* into the model's class ids.

        Filtering inside the model is much cheaper than filtering afterwards,
        because non-matching boxes never reach NMS.
        """
        if not self.config.classes:
            return None
        wanted = {name.lower() for name in self.config.classes}
        ids = [i for i, name in self._class_names.items() if name.lower() in wanted]
        return ids or None

    def detect(self, frame: Frame) -> list[Detection]:
        if not self.is_loaded:
            self.load()
        if frame.is_empty:
            return []

        started = time.perf_counter()
        try:
            with self._lock:
                results = self._model.predict(
                    source=frame.image,
                    conf=self.config.confidence_threshold,
                    iou=self.config.iou_threshold,
                    imgsz=self.config.image_size,
                    device=self.device,
                    classes=self._allowed_class_ids(),
                    max_det=self.config.max_detections,
                    verbose=False,
                )
        except Exception as exc:
            self.stats.failures += 1
            self.stats.last_error = str(exc)[:200]
            raise DetectionError("Object detection failed for this frame.") from exc

        inference_ms = (time.perf_counter() - started) * 1000
        detections = self._parse(results, frame)
        self.stats.record(inference_ms, len(detections))
        return detections

    def _parse(self, results: Any, frame: Frame) -> list[Detection]:
        """Convert Ultralytics output into normalised Detection objects."""
        detections: list[Detection] = []
        height, width = frame.image.shape[:2]

        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None or len(boxes) == 0:
                continue
            # xyxy in pixels, on whatever device inference ran on.
            xyxy = boxes.xyxy.cpu().numpy()
            confidences = boxes.conf.cpu().numpy()
            class_ids = boxes.cls.cpu().numpy().astype(int)

            for (x1, y1, x2, y2), confidence, class_id in zip(
                xyxy, confidences, class_ids, strict=False
            ):
                detections.append(
                    Detection(
                        class_id=int(class_id),
                        class_name=self._class_names.get(int(class_id), str(class_id)),
                        confidence=float(confidence),
                        bbox=BoundingBox.from_pixels(
                            float(x1), float(y1), float(x2), float(y2), width, height
                        ),
                        timestamp=frame.timestamp,
                        camera_id=frame.camera_id,
                        frame_index=frame.index,
                        model_version=self.config.model_version,
                    )
                )
        return detections

    def close(self) -> None:
        self._model = None
        self._class_names = {}


class ScriptedDetectionEngine(DetectionEngine):
    """Returns pre-programmed detections. For deterministic tests.

    This is not a stub that fakes success -- it is a real engine whose input
    happens to be a script rather than a neural network.  It lets tracking,
    zone and rule logic be tested against exactly known inputs, which is the
    only way to assert that a person crossing a line is counted exactly once.

    ``script`` maps frame index to a list of
    ``(class_name, confidence, (x1, y1, x2, y2))`` tuples in normalised
    coordinates.  A ``default`` applies to any frame not in the script.
    """

    def __init__(
        self,
        config: VisionConfig | None = None,
        *,
        script: dict[int, list[tuple[str, float, tuple[float, float, float, float]]]] | None = None,
        default: list[tuple[str, float, tuple[float, float, float, float]]] | None = None,
        class_ids: dict[str, int] | None = None,
    ) -> None:
        super().__init__(config or VisionConfig())
        self.script = script or {}
        self.default = default or []
        self.class_ids = class_ids or {"person": 0, "car": 2, "bottle": 39, "backpack": 24}
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def class_names(self) -> dict[int, str]:
        return {v: k for k, v in self.class_ids.items()}

    def load(self) -> None:
        self._loaded = True

    def detect(self, frame: Frame) -> list[Detection]:
        if not self._loaded:
            self.load()
        started = time.perf_counter()
        entries = self.script.get(frame.index, self.default)
        detections = [
            Detection(
                class_id=self.class_ids.get(class_name, 0),
                class_name=class_name,
                confidence=confidence,
                bbox=BoundingBox(*box),
                timestamp=frame.timestamp,
                camera_id=frame.camera_id,
                frame_index=frame.index,
                model_version="scripted",
            )
            for class_name, confidence, box in entries
            if confidence >= self.config.confidence_threshold
        ]
        self.stats.record((time.perf_counter() - started) * 1000, len(detections))
        return detections


def build_engine(config: VisionConfig) -> DetectionEngine:
    """Create the detection engine described by ``config``.

    A ``model_path`` of ``"scripted"`` selects the test engine; anything else
    is a YOLO weights file.
    """
    if config.model_path == "scripted":
        return ScriptedDetectionEngine(config)
    return YOLODetectionEngine(config)
