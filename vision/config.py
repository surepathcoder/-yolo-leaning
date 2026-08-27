"""Vision engine configuration.

The vision engine has its own configuration object rather than importing the
backend's ``Settings``.  That is what keeps ``vision/`` runnable on an edge box
with no database, no Redis and no web framework -- the backend simply builds
one of these from its own settings and hands it over.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass(slots=True)
class VisionConfig:
    """Everything the vision engine needs to run one camera pipeline."""

    # ------------------------------------------------------------- detection
    model_path: str = "yolo26n.pt"
    confidence_threshold: float = 0.35
    iou_threshold: float = 0.45
    #: "auto" resolves to CUDA when available, else CPU.
    device: str = "auto"
    image_size: int = 640
    #: Class names to report. Empty means every class the model knows.
    classes: tuple[str, ...] = ()
    #: Cap on detections per frame. A frame reporting hundreds of objects is
    #: almost always a model or threshold problem, not a crowded shop.
    max_detections: int = 100
    model_version: str | None = None

    # -------------------------------------------------------------- tracking
    tracker_type: str = "bytetrack"
    #: Frames a track survives without a match before it is closed. At 10 FPS
    #: this is ~3 s, long enough to bridge a person walking behind a pillar.
    track_max_age: int = 30
    #: Matches required before a track is reported, suppressing single-frame
    #: false positives.
    track_min_hits: int = 3
    track_iou_threshold: float = 0.3
    #: Second association pass over low-confidence detections -- the core of
    #: ByteTrack, and what keeps ids stable through partial occlusion.
    track_low_confidence_threshold: float = 0.1

    # --------------------------------------------------------------- capture
    target_fps: float = 10.0
    frame_queue_size: int = 8
    #: Seconds to wait for a frame before treating the source as stalled.
    read_timeout_seconds: float = 10.0
    reconnect_initial_delay: float = 1.0
    reconnect_max_delay: float = 60.0
    max_reconnect_attempts: int = 0  # 0 = retry forever
    rtsp_transport: str = "tcp"

    # ----------------------------------------------------------------- rules
    loitering_threshold_seconds: float = 60.0
    line_crossing_cooldown_seconds: float = 3.0
    zone_entry_debounce_seconds: float = 1.0

    # --------------------------------------------------------------- runtime
    draw_annotations: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> VisionConfig:
        """Build from environment variables, for standalone/edge use."""
        classes_raw = os.environ.get("YOLO_CLASSES", "").strip()
        return cls(
            model_path=os.environ.get("YOLO_MODEL_PATH", "yolo26n.pt"),
            confidence_threshold=_env_float("YOLO_CONFIDENCE_THRESHOLD", 0.35),
            iou_threshold=_env_float("YOLO_IOU_THRESHOLD", 0.45),
            device=os.environ.get("YOLO_DEVICE", "auto"),
            image_size=_env_int("YOLO_IMAGE_SIZE", 640),
            classes=tuple(c.strip() for c in classes_raw.split(",") if c.strip()),
            tracker_type=os.environ.get("TRACKER_TYPE", "bytetrack"),
            target_fps=_env_float("VISION_FPS", 10.0),
            frame_queue_size=_env_int("VISION_FRAME_QUEUE_SIZE", 8),
            loitering_threshold_seconds=_env_float("LOITERING_THRESHOLD_SECONDS", 60.0),
            line_crossing_cooldown_seconds=_env_float("LINE_CROSSING_COOLDOWN_SECONDS", 3.0),
            rtsp_transport=os.environ.get("RTSP_TRANSPORT", "tcp"),
        )

    def resolved_device(self) -> str:
        """Resolve ``device="auto"`` against the hardware actually present.

        Import of torch is deferred so that configuration can be built (and
        tested) without loading a 2 GB library.
        """
        if self.device != "auto":
            return self.device
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                return "mps"
        except Exception:  # pragma: no cover - torch missing or broken
            pass
        return "cpu"

    def model_exists(self) -> bool:
        return Path(self.model_path).exists()

    def frame_interval(self) -> float:
        """Seconds between processed frames at the target rate."""
        return 1.0 / self.target_fps if self.target_fps > 0 else 0.0
