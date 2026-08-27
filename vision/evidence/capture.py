"""Turning the frame an event fired on into a stored artefact.

When a significant event fires, an operator later wants to see it: the still
frame at the moment, a small thumbnail for the timeline, and a few seconds of
video either side so the action has context. This module produces those three
things as plain ``bytes`` and hands them to a callback. It writes nothing to
disk and knows nothing about the database -- the backend's evidence service
does the storing.

**Memory.** A rolling buffer of decoded frames is kept so a clip can include
the seconds *before* the event. It is bounded to ``pre_seconds`` of video: at
the pipeline's target FPS that is a few dozen frames, each a copy of the
source image. A clip in progress holds its own frames until ``post_seconds``
have elapsed, then encodes and releases them.

**Failure.** Encoding is best-effort. A missing codec or a bad frame
increments a counter and drops that one artefact; it never raises into the
pipeline thread, on the same principle as :class:`~vision.events.CallbackSink`
-- losing a picture is bad, losing the camera is worse.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from vision.events.engine import dedupe_key
from vision.events.types import VisionEvent
from vision.types import Frame, TrackedObject

#: Annotation colour (BGR) for the box around the track an event concerns.
_BOX_COLOUR = (66, 135, 245)
_BOX_THICKNESS = 3


def _fourcc(code: str) -> int:
    """The fourcc int for a codec, across OpenCV versions."""
    factory = getattr(cv2, "VideoWriter_fourcc", None) or cv2.VideoWriter.fourcc
    return int(factory(*code))


@dataclass(frozen=True, slots=True)
class EvidenceSettings:
    """What to capture and how. Built by the backend from its settings."""

    enabled: bool = False
    snapshot: bool = True
    thumbnail: bool = True
    clip: bool = True
    #: Seconds of video kept before the event and captured after it.
    pre_seconds: float = 4.0
    post_seconds: float = 4.0
    #: Frame rate written into the clip container. The pipeline's processed
    #: rate, not the source rate.
    clip_fps: float = 8.0
    thumbnail_width: int = 320
    jpeg_quality: int = 85
    annotate: bool = True
    #: Ceiling on clips being filled at once, so a burst of events cannot pin
    #: an unbounded number of frame lists in memory.
    max_pending_clips: int = 8


@dataclass(slots=True)
class EvidenceArtifact:
    """One finished artefact, ready for the backend to store."""

    kind: str  # "SNAPSHOT" | "THUMBNAIL" | "CLIP"
    data: bytes
    mime_type: str
    width: int
    height: int
    captured_at: datetime
    #: The event this artefact documents. Kept whole so the backend can
    #: reconstruct the persisted event's dedupe key and link the row.
    event: VisionEvent
    duration_seconds: float | None = None
    #: Normalised box highlighted in the artefact, if any.
    bbox: tuple[float, float, float, float] | None = None


@dataclass(slots=True)
class _BufferedFrame:
    timestamp: datetime
    image: np.ndarray


@dataclass(slots=True)
class _PendingClip:
    event: VisionEvent
    deadline: datetime
    bbox: tuple[float, float, float, float] | None
    frames: list[_BufferedFrame] = field(default_factory=list)


@dataclass(slots=True)
class EvidenceStats:
    snapshots: int = 0
    thumbnails: int = 0
    clips: int = 0
    failures: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "snapshots": self.snapshots,
            "thumbnails": self.thumbnails,
            "clips": self.clips,
            "failures": self.failures,
            "last_error": self.last_error,
        }


class _FrameBuffer:
    """The last ``pre_seconds`` of decoded frames, newest last."""

    def __init__(self, pre_seconds: float) -> None:
        self._pre = max(pre_seconds, 0.0)
        self._frames: deque[_BufferedFrame] = deque()

    def append(self, timestamp: datetime, image: np.ndarray) -> None:
        self._frames.append(_BufferedFrame(timestamp, image.copy()))
        cutoff = timestamp - timedelta(seconds=self._pre)
        while self._frames and self._frames[0].timestamp < cutoff:
            self._frames.popleft()

    def snapshot(self) -> list[_BufferedFrame]:
        """A shallow copy of the current contents (frames are not re-copied)."""
        return list(self._frames)


class EvidenceCapturer:
    """Builds snapshot, thumbnail and clip artefacts for triggering events."""

    def __init__(
        self,
        settings: EvidenceSettings,
        on_artifact: Callable[[EvidenceArtifact], None],
    ) -> None:
        self.settings = settings
        self.on_artifact = on_artifact
        self.stats = EvidenceStats()
        self._buffer = _FrameBuffer(settings.pre_seconds)
        self._pending: dict[str, _PendingClip] = {}

    # ------------------------------------------------------------------ input
    def observe(self, frame: Frame) -> None:
        """Feed every processed frame in. Cheap on the common no-event path."""
        if frame.is_empty:
            return
        if self.settings.clip:
            self._buffer.append(frame.timestamp, frame.image)
            self._advance_clips(frame)

    def trigger(
        self,
        frame: Frame,
        event: VisionEvent,
        tracks: list[TrackedObject],
    ) -> None:
        """Capture evidence for one event that fired on this frame."""
        if frame.is_empty:
            return
        bbox = self._event_bbox(event, tracks)
        captured_at = frame.timestamp
        rendered = self._render(frame.image, bbox)
        height, width = rendered.shape[0], rendered.shape[1]

        if self.settings.snapshot:
            self._emit_image(
                "SNAPSHOT", self._encode_jpeg(rendered), width, height, event, captured_at, bbox
            )
        if self.settings.thumbnail:
            thumb = self._thumbnail(rendered)
            if thumb is not None:
                data, tw, th = thumb
                self._emit_image("THUMBNAIL", data, tw, th, event, captured_at, bbox)

        if self.settings.clip:
            self._open_clip(event, frame, bbox)

    def flush(self) -> None:
        """Finalise every clip still filling, with whatever frames it has."""
        for pending in list(self._pending.values()):
            self._finalise_clip(pending)
        self._pending.clear()

    # -------------------------------------------------------------- clip state
    def _open_clip(
        self,
        event: VisionEvent,
        frame: Frame,
        bbox: tuple[float, float, float, float] | None,
    ) -> None:
        key = dedupe_key(event)
        if key in self._pending:
            return
        if len(self._pending) >= self.settings.max_pending_clips:
            # Drop the oldest still-filling clip rather than grow without bound.
            oldest = min(self._pending, key=lambda k: self._pending[k].deadline)
            self._finalise_clip(self._pending.pop(oldest))
        self._pending[key] = _PendingClip(
            event=event,
            deadline=frame.timestamp + timedelta(seconds=self.settings.post_seconds),
            bbox=bbox,
            frames=self._buffer.snapshot(),
        )

    def _advance_clips(self, frame: Frame) -> None:
        finished: list[str] = []
        for key, pending in self._pending.items():
            if frame.timestamp <= pending.deadline:
                pending.frames.append(_BufferedFrame(frame.timestamp, frame.image.copy()))
            else:
                finished.append(key)
        for key in finished:
            self._finalise_clip(self._pending.pop(key))

    def _finalise_clip(self, pending: _PendingClip) -> None:
        data = self._encode_clip(pending.frames)
        if data is None:
            return
        first = pending.frames[0].image
        height, width = first.shape[0], first.shape[1]
        self.stats.clips += 1
        self._deliver(
            EvidenceArtifact(
                kind="CLIP",
                data=data,
                mime_type="video/mp4",
                width=int(width),
                height=int(height),
                captured_at=pending.frames[0].timestamp,
                event=pending.event,
                duration_seconds=round(len(pending.frames) / self.settings.clip_fps, 2),
                bbox=pending.bbox,
            )
        )

    # ------------------------------------------------------------- rendering
    def _event_bbox(
        self, event: VisionEvent, tracks: list[TrackedObject]
    ) -> tuple[float, float, float, float] | None:
        if not event.track_key:
            return None
        for track in tracks:
            if track.track_key == event.track_key:
                return track.bbox.as_tuple()
        return None

    def _render(
        self, image: np.ndarray, bbox: tuple[float, float, float, float] | None
    ) -> np.ndarray:
        if not self.settings.annotate or bbox is None:
            return image
        annotated = image.copy()
        height, width = annotated.shape[0], annotated.shape[1]
        x1, y1, x2, y2 = bbox
        pt1 = (round(x1 * width), round(y1 * height))
        pt2 = (round(x2 * width), round(y2 * height))
        with contextlib.suppress(Exception):
            cv2.rectangle(annotated, pt1, pt2, _BOX_COLOUR, _BOX_THICKNESS)
        return annotated

    def _thumbnail(self, image: np.ndarray) -> tuple[bytes, int, int] | None:
        height, width = image.shape[0], image.shape[1]
        if width <= 0 or height <= 0:
            return None
        target_w = min(self.settings.thumbnail_width, width)
        target_h = max(1, round(height * target_w / width))
        try:
            resized = cv2.resize(image, (target_w, target_h), interpolation=cv2.INTER_AREA)
        except Exception as exc:  # pragma: no cover - defensive
            self._record_failure(exc)
            return None
        data = self._encode_jpeg(resized)
        return None if data is None else (data, target_w, target_h)

    # --------------------------------------------------------------- encoding
    def _encode_jpeg(self, image: np.ndarray) -> bytes | None:
        try:
            ok, buffer = cv2.imencode(
                ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self.settings.jpeg_quality]
            )
        except Exception as exc:
            self._record_failure(exc)
            return None
        if not ok:
            self._record_failure(RuntimeError("cv2.imencode returned false"))
            return None
        return buffer.tobytes()

    def _encode_clip(self, frames: list[_BufferedFrame]) -> bytes | None:
        if not frames:
            return None
        first = frames[0].image
        height, width = first.shape[0], first.shape[1]
        if width <= 0 or height <= 0:
            return None

        writer = None
        try:
            with tempfile.TemporaryDirectory(prefix="vg-evidence-") as workdir:
                path = os.path.join(workdir, "clip.mp4")
                writer = cv2.VideoWriter(
                    path, _fourcc("mp4v"), max(self.settings.clip_fps, 1.0), (width, height)
                )
                if not writer.isOpened():
                    self._record_failure(RuntimeError("cv2.VideoWriter failed to open"))
                    return None
                for buffered in frames:
                    if buffered.image.shape[:2] != (height, width):
                        continue
                    writer.write(buffered.image)
                writer.release()
                writer = None
                data = Path(path).read_bytes()
            return data or None
        except Exception as exc:
            self._record_failure(exc)
            return None
        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.release()

    # ---------------------------------------------------------------- output
    def _emit_image(
        self,
        kind: str,
        data: bytes | None,
        width: int,
        height: int,
        event: VisionEvent,
        captured_at: datetime,
        bbox: tuple[float, float, float, float] | None,
    ) -> None:
        if data is None:
            return
        if kind == "SNAPSHOT":
            self.stats.snapshots += 1
        else:
            self.stats.thumbnails += 1
        self._deliver(
            EvidenceArtifact(
                kind=kind,
                data=data,
                mime_type="image/jpeg",
                width=int(width),
                height=int(height),
                captured_at=captured_at,
                event=event,
                bbox=bbox,
            )
        )

    def _deliver(self, artifact: EvidenceArtifact) -> None:
        try:
            self.on_artifact(artifact)
        except Exception as exc:
            self._record_failure(exc)

    def _record_failure(self, exc: Exception) -> None:
        self.stats.failures += 1
        self.stats.last_error = f"{type(exc).__name__}: {exc}"[:200]

    def to_dict(self) -> dict:
        return {**self.stats.to_dict(), "pending_clips": len(self._pending)}
