"""Video source abstraction.

The pipeline never talks to OpenCV directly.  It asks a :class:`VideoSource`
for frames, and the source deals with whatever a file, a webcam or a flaky
RTSP stream on a rural ADSL line actually does.

Four implementations:

``LocalVideoSource``     a video file -- development and deterministic tests
``WebcamSource``         an attached camera -- development
``RTSPVideoSource``      a real CCTV camera or NVR -- production
``SyntheticVideoSource`` generated frames -- tests with no files or hardware

The synthetic source matters more than it sounds: it lets the whole pipeline,
including tracking and rule evaluation, be tested deterministically with no
media files, no hardware and no model weights.
"""

from __future__ import annotations

import contextlib
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np

from vision.types import Frame


class SourceState(StrEnum):
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ENDED = "ENDED"
    ERROR = "ERROR"


class VideoSourceError(Exception):
    """A video source could not be opened or read.

    The message is safe to show a user: implementations must not put
    credentials into it.
    """


@dataclass(slots=True)
class SourceStats:
    """Health counters for one source, surfaced on the metrics endpoint."""

    frames_read: int = 0
    frames_failed: int = 0
    reconnects: int = 0
    opened_at: datetime | None = None
    last_frame_at: datetime | None = None
    last_error: str | None = None

    @property
    def uptime_seconds(self) -> float:
        if self.opened_at is None:
            return 0.0
        return (datetime.now(UTC) - self.opened_at).total_seconds()

    @property
    def measured_fps(self) -> float:
        if self.opened_at is None or self.frames_read == 0:
            return 0.0
        elapsed = self.uptime_seconds
        return self.frames_read / elapsed if elapsed > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "frames_read": self.frames_read,
            "frames_failed": self.frames_failed,
            "reconnects": self.reconnects,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "measured_fps": round(self.measured_fps, 2),
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "last_error": self.last_error,
        }


class VideoSource(ABC):
    """A source of video frames.

    Implementations must:

    * never raise from :meth:`read` for an ordinary read failure -- return
      ``None`` so the pipeline can decide whether to reconnect;
    * be safe to :meth:`close` twice;
    * never include credentials in an exception message, ``repr`` or log line.
    """

    def __init__(self, camera_id: str, *, target_fps: float = 0.0) -> None:
        self.camera_id = camera_id
        self.target_fps = target_fps
        self.state: SourceState = SourceState.IDLE
        self.stats = SourceStats()
        self._frame_index = 0
        self._last_yield = 0.0

    # ------------------------------------------------------------- lifecycle
    @abstractmethod
    def open(self) -> None:
        """Connect. Raises :class:`VideoSourceError` on failure."""

    @abstractmethod
    def read(self) -> Frame | None:
        """Return the next frame, or ``None`` when unavailable or ended."""

    @abstractmethod
    def close(self) -> None:
        """Release resources. Must be idempotent."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    # ------------------------------------------------------------ properties
    @property
    def resolution(self) -> tuple[int, int] | None:
        """``(width, height)`` if known before the first frame is read."""
        return None

    @property
    def source_fps(self) -> float | None:
        """The source's own frame rate, if it reports one."""
        return None

    @property
    def is_live(self) -> bool:
        """Whether frames arrive in real time.

        A live source must drop frames to keep up; a file source must not,
        because dropping would change what the pipeline sees.
        """
        return False

    # --------------------------------------------------------------- helpers
    def _next_index(self) -> int:
        index = self._frame_index
        self._frame_index += 1
        return index

    def _make_frame(self, image: np.ndarray, timestamp: datetime | None = None) -> Frame:
        frame = Frame(
            image=image,
            index=self._next_index(),
            timestamp=timestamp or datetime.now(UTC),
            camera_id=self.camera_id,
            source_fps=self.source_fps,
        )
        self.stats.frames_read += 1
        self.stats.last_frame_at = frame.timestamp
        return frame

    def _throttle(self) -> None:
        """Sleep so a non-live source does not run faster than ``target_fps``.

        Only applied to non-live sources: throttling a live camera would build
        an ever-growing backlog in the driver instead of dropping frames.
        """
        if self.target_fps <= 0 or self.is_live:
            return
        interval = 1.0 / self.target_fps
        elapsed = time.monotonic() - self._last_yield
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_yield = time.monotonic()

    # -------------------------------------------------------------- iteration
    def frames(self) -> Iterator[Frame]:
        """Iterate frames until the source ends.

        Opens on first use and always closes, so callers can simply write
        ``for frame in source.frames():``.
        """
        if not self.is_open:
            self.open()
        try:
            while True:
                frame = self.read()
                if frame is None:
                    if self.state in (SourceState.ENDED, SourceState.ERROR):
                        return
                    continue
                self._throttle()
                yield frame
        finally:
            self.close()

    def __enter__(self) -> VideoSource:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} camera={self.camera_id} state={self.state}>"


# ---------------------------------------------------------------------------
# Synthetic source
# ---------------------------------------------------------------------------


class SyntheticVideoSource(VideoSource):
    """Generates deterministic frames with moving rectangles.

    This exists so the tracking, zone and counting logic can be tested with
    exactly known ground truth: if a rectangle moves from x=0.2 to x=0.8 across
    30 frames, a line at x=0.5 must be crossed exactly once, in a known
    direction.  Testing that against real video would mean hand-labelling.

    ``objects`` is a list of ``{"start": (x, y), "end": (x, y), "size": (w, h)}``
    in normalised coordinates; each moves linearly from start to end over the
    frame count.
    """

    def __init__(
        self,
        camera_id: str = "synthetic",
        *,
        width: int = 640,
        height: int = 480,
        frame_count: int = 60,
        fps: float = 10.0,
        objects: list[dict[str, Any]] | None = None,
        start_time: datetime | None = None,
        target_fps: float = 0.0,
        noise: bool = False,
        seed: int = 1234,
    ) -> None:
        super().__init__(camera_id, target_fps=target_fps)
        self.width = width
        self.height = height
        self.frame_count = frame_count
        self.fps = fps
        self.objects = objects or [{"start": (0.2, 0.5), "end": (0.8, 0.5), "size": (0.08, 0.25)}]
        self.start_time = start_time or datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        self.noise = noise
        self._rng = np.random.default_rng(seed)
        self._open = False

    def open(self) -> None:
        self._open = True
        self.state = SourceState.CONNECTED
        self.stats.opened_at = datetime.now(UTC)

    def close(self) -> None:
        self._open = False
        if self.state == SourceState.CONNECTED:
            self.state = SourceState.ENDED

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def resolution(self) -> tuple[int, int]:
        return (self.width, self.height)

    @property
    def source_fps(self) -> float:
        return self.fps

    def object_bbox_at(
        self, object_index: int, frame_index: int
    ) -> tuple[float, float, float, float]:
        """The normalised box of one object at one frame -- the ground truth."""
        spec = self.objects[object_index]
        progress = frame_index / max(self.frame_count - 1, 1)
        sx, sy = spec["start"]
        ex, ey = spec["end"]
        w, h = spec.get("size", (0.08, 0.25))
        cx = sx + (ex - sx) * progress
        cy = sy + (ey - sy) * progress
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

    def read(self) -> Frame | None:
        if not self._open:
            return None
        if self._frame_index >= self.frame_count:
            self.state = SourceState.ENDED
            return None

        index = self._frame_index
        image = np.full((self.height, self.width, 3), 40, dtype=np.uint8)
        if self.noise:
            image = np.clip(
                image.astype(np.int16) + self._rng.integers(-12, 12, image.shape, dtype=np.int16),
                0,
                255,
            ).astype(np.uint8)

        for object_index in range(len(self.objects)):
            x1, y1, x2, y2 = self.object_bbox_at(object_index, index)
            px1 = max(int(x1 * self.width), 0)
            py1 = max(int(y1 * self.height), 0)
            px2 = min(int(x2 * self.width), self.width)
            py2 = min(int(y2 * self.height), self.height)
            if px2 > px1 and py2 > py1:
                image[py1:py2, px1:px2] = (200, 200, 210)

        timestamp = self.start_time + timedelta(seconds=index / self.fps)
        return self._make_frame(image, timestamp)


# ---------------------------------------------------------------------------
# OpenCV-backed sources
# ---------------------------------------------------------------------------


class _OpenCVSource(VideoSource):
    """Shared plumbing for sources that read through ``cv2.VideoCapture``.

    OpenCV is imported lazily so that importing :mod:`vision.cameras` -- to
    use the synthetic source in a test, say -- does not require OpenCV.
    """

    def __init__(self, camera_id: str, *, target_fps: float = 0.0) -> None:
        super().__init__(camera_id, target_fps=target_fps)
        self._capture: Any = None

    @staticmethod
    def _cv2():
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover
            raise VideoSourceError(
                "OpenCV is not installed. Install vision/requirements.txt."
            ) from exc
        return cv2

    @property
    def is_open(self) -> bool:
        return self._capture is not None and self._capture.isOpened()

    @property
    def resolution(self) -> tuple[int, int] | None:
        if not self.is_open:
            return None
        cv2 = self._cv2()
        width = int(self._capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return (width, height) if width and height else None

    @property
    def source_fps(self) -> float | None:
        if not self.is_open:
            return None
        fps = float(self._capture.get(self._cv2().CAP_PROP_FPS))
        # Many RTSP streams report 0 or an absurd value; treat those as unknown
        # rather than propagating a number that would corrupt timing.
        return fps if 0 < fps < 1000 else None

    def close(self) -> None:
        if self._capture is not None:
            with contextlib.suppress(Exception):
                self._capture.release()
            self._capture = None
        if self.state in (SourceState.CONNECTED, SourceState.RECONNECTING):
            self.state = SourceState.ENDED


class LocalVideoSource(_OpenCVSource):
    """Reads a video file. Used for development and repeatable testing."""

    def __init__(
        self,
        path: str | Path,
        camera_id: str = "local",
        *,
        loop: bool = False,
        target_fps: float = 0.0,
        start_time: datetime | None = None,
    ) -> None:
        super().__init__(camera_id, target_fps=target_fps)
        self.path = Path(path)
        self.loop = loop
        self.start_time = start_time or datetime.now(UTC)

    def open(self) -> None:
        if not self.path.exists():
            self.state = SourceState.ERROR
            raise VideoSourceError(f"Video file not found: {self.path.name}")
        cv2 = self._cv2()
        self.state = SourceState.CONNECTING
        capture = cv2.VideoCapture(str(self.path))
        if not capture.isOpened():
            self.state = SourceState.ERROR
            raise VideoSourceError(f"Unable to open video file: {self.path.name}")
        self._capture = capture
        self.state = SourceState.CONNECTED
        self.stats.opened_at = datetime.now(UTC)

    def read(self) -> Frame | None:
        if not self.is_open:
            return None
        ok, image = self._capture.read()
        if not ok or image is None:
            if self.loop:
                self._capture.set(self._cv2().CAP_PROP_POS_FRAMES, 0)
                ok, image = self._capture.read()
                if not ok or image is None:
                    self.state = SourceState.ENDED
                    return None
            else:
                self.state = SourceState.ENDED
                return None

        fps = self.source_fps or 25.0
        timestamp = self.start_time + timedelta(seconds=self._frame_index / fps)
        return self._make_frame(image, timestamp)

    @property
    def frame_total(self) -> int | None:
        if not self.is_open:
            return None
        total = int(self._capture.get(self._cv2().CAP_PROP_FRAME_COUNT))
        return total if total > 0 else None


class WebcamSource(_OpenCVSource):
    """Reads an attached camera. Development only."""

    def __init__(
        self,
        device_index: int = 0,
        camera_id: str = "webcam",
        *,
        width: int | None = None,
        height: int | None = None,
        target_fps: float = 0.0,
    ) -> None:
        super().__init__(camera_id, target_fps=target_fps)
        self.device_index = device_index
        self.requested_width = width
        self.requested_height = height

    @property
    def is_live(self) -> bool:
        return True

    def open(self) -> None:
        cv2 = self._cv2()
        self.state = SourceState.CONNECTING
        capture = cv2.VideoCapture(self.device_index)
        if not capture.isOpened():
            self.state = SourceState.ERROR
            raise VideoSourceError(f"Unable to open webcam device {self.device_index}.")
        if self.requested_width:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.requested_width)
        if self.requested_height:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.requested_height)
        # A one-frame buffer keeps latency low: with a live source we want the
        # newest frame, not the oldest queued one.
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._capture = capture
        self.state = SourceState.CONNECTED
        self.stats.opened_at = datetime.now(UTC)

    def read(self) -> Frame | None:
        if not self.is_open:
            return None
        ok, image = self._capture.read()
        if not ok or image is None:
            self.stats.frames_failed += 1
            return None
        return self._make_frame(image)


class RTSPVideoSource(_OpenCVSource):
    """Reads an RTSP stream from a CCTV camera or NVR.

    Credentials are supplied separately from the URL and are inserted only when
    the connection string is built, immediately before use.  The URL is never
    stored on the instance, never logged and never appears in an exception --
    :attr:`safe_url` is what any diagnostic should show.

    TCP transport is the default: UDP loses packets on the congested or
    long-cable installations that are common in practice, and the resulting
    corrupted frames produce phantom detections.
    """

    def __init__(
        self,
        host: str,
        *,
        camera_id: str = "rtsp",
        port: int = 554,
        path: str = "/",
        username: str | None = None,
        password: str | None = None,
        transport: str = "tcp",
        target_fps: float = 0.0,
        open_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(camera_id, target_fps=target_fps)
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else f"/{path}"
        self.username = username
        self._password = password
        self.transport = transport
        self.open_timeout_seconds = open_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds

    @property
    def is_live(self) -> bool:
        return True

    @property
    def safe_url(self) -> str:
        """The stream URL with credentials masked. Safe to log or display."""
        auth = f"{self.username}:***@" if self.username else ""
        return f"rtsp://{auth}{self.host}:{self.port}{self.path}"

    def _build_url(self) -> str:
        """Assemble the real URL. The result must never be stored or logged."""
        from urllib.parse import quote

        auth = ""
        if self.username:
            user = quote(self.username, safe="")
            # Passwords routinely contain @ : / -- percent-encode or the URL
            # silently parses into the wrong host.
            secret = quote(self._password or "", safe="")
            auth = f"{user}:{secret}@"
        return f"rtsp://{auth}{self.host}:{self.port}{self.path}"

    def open(self) -> None:
        import os

        cv2 = self._cv2()
        self.state = SourceState.CONNECTING

        # FFmpeg reads its options from this environment variable. Setting the
        # transport and timeouts here is what stops a dead camera from hanging
        # the whole pipeline thread indefinitely.
        timeout_us = int(self.open_timeout_seconds * 1_000_000)
        options = [
            f"rtsp_transport;{self.transport}",
            f"stimeout;{timeout_us}",
            "max_delay;500000",
        ]
        previous = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(options)
        try:
            capture = cv2.VideoCapture(self._build_url(), cv2.CAP_FFMPEG)
        finally:
            if previous is None:
                os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = previous

        if not capture.isOpened():
            self.state = SourceState.ERROR
            self.stats.last_error = "connection refused or timed out"
            # The message deliberately names the camera, not the URL.
            raise VideoSourceError(f"Unable to connect to camera at {self.host}:{self.port}.")

        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._capture = capture
        self.state = SourceState.CONNECTED
        self.stats.opened_at = datetime.now(UTC)

    def read(self) -> Frame | None:
        if not self.is_open:
            return None
        ok, image = self._capture.read()
        if not ok or image is None:
            self.stats.frames_failed += 1
            self.stats.last_error = "frame read failed"
            return None
        # A stream that returns a decodable but empty frame is corrupted;
        # passing it to the detector would produce nonsense detections.
        if image.size == 0:
            self.stats.frames_failed += 1
            return None
        return self._make_frame(image)

    def __repr__(self) -> str:
        return f"<RTSPVideoSource camera={self.camera_id} url={self.safe_url} state={self.state}>"
