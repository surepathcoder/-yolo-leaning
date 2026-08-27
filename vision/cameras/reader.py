"""Frame reading with reconnection, FPS control and graceful shutdown.

Real CCTV installations in the target market sit on unreliable power and
long cable runs.  Cameras drop out for seconds or hours and come back.  A
pipeline that dies on the first read failure is useless, and one that
reconnects in a tight loop will hammer a struggling camera and fill the disk
with logs.

:class:`FrameReader` sits between a :class:`VideoSource` and the pipeline and
handles all of that:

* exponential backoff with a cap on reconnection
* frame skipping to hold a target processing rate on a live source
* a bounded queue in threaded mode, dropping the **oldest** frame when full
* clean shutdown from another thread
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from vision.cameras.source import SourceState, VideoSource, VideoSourceError
from vision.types import Frame


@dataclass(slots=True)
class ReaderStats:
    frames_delivered: int = 0
    frames_skipped: int = 0
    frames_dropped: int = 0
    reconnect_attempts: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    started_at: datetime | None = None

    @property
    def uptime_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return (datetime.now(UTC) - self.started_at).total_seconds()

    @property
    def delivered_fps(self) -> float:
        elapsed = self.uptime_seconds
        return self.frames_delivered / elapsed if elapsed > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "frames_delivered": self.frames_delivered,
            "frames_skipped": self.frames_skipped,
            "frames_dropped": self.frames_dropped,
            "reconnect_attempts": self.reconnect_attempts,
            "consecutive_failures": self.consecutive_failures,
            "delivered_fps": round(self.delivered_fps, 2),
            "uptime_seconds": round(self.uptime_seconds, 1),
            "last_error": self.last_error,
        }


def backoff_delay(
    attempt: int, *, initial: float = 1.0, maximum: float = 60.0, factor: float = 2.0
) -> float:
    """Exponential backoff, capped.

    Deliberately has no random jitter: a VisionGuard deployment reconnects to
    a handful of cameras it owns, not to a shared service, so there is no
    thundering herd to spread out, and a predictable delay is far easier to
    reason about when reading logs at 3am.
    """
    if attempt <= 0:
        return 0.0
    return min(initial * (factor ** (attempt - 1)), maximum)


class FrameReader:
    """Reads frames from a source, reconnecting as needed.

    ``target_fps`` throttles *processing*, not the camera. On a live source
    frames are read as fast as they arrive and surplus ones are discarded, so
    the pipeline always works on recent video rather than falling behind.
    """

    def __init__(
        self,
        source: VideoSource,
        *,
        target_fps: float = 0.0,
        max_reconnect_attempts: int = 0,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
        max_consecutive_read_failures: int = 30,
        queue_size: int = 8,
        on_state_change=None,
    ) -> None:
        self.source = source
        self.target_fps = target_fps
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_initial_delay = reconnect_initial_delay
        self.reconnect_max_delay = reconnect_max_delay
        self.max_consecutive_read_failures = max_consecutive_read_failures
        self.queue_size = queue_size
        self.on_state_change = on_state_change

        self.stats = ReaderStats()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue[Frame | None] = queue.Queue(maxsize=queue_size)
        self._last_delivered = 0.0

    # ------------------------------------------------------------- lifecycle
    def stop(self) -> None:
        """Request shutdown. Safe to call from any thread, more than once."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _notify(self, state: SourceState, detail: str | None = None) -> None:
        if self.on_state_change is not None:
            # A misbehaving callback must never take the reader down with it.
            with contextlib.suppress(Exception):
                self.on_state_change(state, detail)

    def _should_deliver(self) -> bool:
        """Whether enough time has passed to process another frame."""
        if self.target_fps <= 0:
            return True
        interval = 1.0 / self.target_fps
        now = time.monotonic()
        if now - self._last_delivered >= interval:
            self._last_delivered = now
            return True
        return False

    def _connect(self) -> bool:
        """Open the source, retrying with backoff. False if we gave up."""
        attempt = 0
        while not self._stop.is_set():
            try:
                self._notify(SourceState.CONNECTING)
                self.source.open()
                if attempt > 0:
                    self.stats.reconnect_attempts += attempt
                self.stats.consecutive_failures = 0
                self.stats.last_error = None
                self._notify(SourceState.CONNECTED)
                return True
            except VideoSourceError as exc:
                attempt += 1
                self.stats.last_error = str(exc)
                self._notify(SourceState.RECONNECTING, str(exc))
                if 0 < self.max_reconnect_attempts <= attempt:
                    self.stats.reconnect_attempts += attempt
                    self._notify(SourceState.ERROR, str(exc))
                    return False
                delay = backoff_delay(
                    attempt,
                    initial=self.reconnect_initial_delay,
                    maximum=self.reconnect_max_delay,
                )
                # Wait on the stop event rather than sleeping, so shutdown is
                # immediate instead of blocking for up to a minute.
                if self._stop.wait(delay):
                    return False
        return False

    # -------------------------------------------------------------- iteration
    def read_frames(self) -> Iterator[Frame]:
        """Yield frames, reconnecting on failure, until stopped or ended.

        Synchronous: the caller's thread does the reading. Use
        :meth:`start_background` when the consumer is slower than the camera.
        """
        self.stats.started_at = datetime.now(UTC)
        try:
            if not self._connect():
                return

            while not self._stop.is_set():
                frame = self.source.read()

                if frame is None:
                    if self.source.state == SourceState.ENDED:
                        return  # a file finished: not an error, do not reconnect
                    self.stats.consecutive_failures += 1
                    if self.stats.consecutive_failures >= self.max_consecutive_read_failures:
                        # The socket is open but no frames arrive -- a camera
                        # that has hung. Only a full reconnect recovers this.
                        self.stats.last_error = "stream stalled; reconnecting"
                        self._notify(SourceState.RECONNECTING, self.stats.last_error)
                        self.source.close()
                        if not self._connect():
                            return
                    else:
                        self._stop.wait(0.05)
                    continue

                self.stats.consecutive_failures = 0

                if not self._should_deliver():
                    self.stats.frames_skipped += 1
                    continue

                self.stats.frames_delivered += 1
                yield frame

                if not self.source.is_live:
                    self._throttle_file_source()
        finally:
            self.source.close()
            self._notify(SourceState.ENDED)

    def _throttle_file_source(self) -> None:
        """Pace a file source so it does not race through at disk speed."""
        if self.target_fps <= 0:
            return
        self._stop.wait(1.0 / self.target_fps)

    # ------------------------------------------------------------- threaded
    def start_background(self) -> None:
        """Read into a bounded queue on a background thread.

        When the queue is full the **oldest** frame is dropped, not the newest:
        for security video the most recent frame is always the useful one, and
        an operator would rather miss a stale frame than watch a growing lag.
        """
        if self._thread is not None:
            raise RuntimeError("reader already started")

        def _worker() -> None:
            try:
                for frame in self.read_frames():
                    while True:
                        try:
                            self._queue.put_nowait(frame)
                            break
                        except queue.Full:
                            with contextlib.suppress(queue.Empty):
                                self._queue.get_nowait()
                                self.stats.frames_dropped += 1
            finally:
                self._queue.put(None)  # sentinel: the stream is finished

        self._thread = threading.Thread(
            target=_worker, name=f"frame-reader-{self.source.camera_id}", daemon=True
        )
        self._thread.start()

    def get(self, timeout: float = 1.0) -> Frame | None:
        """Take the next frame from the background queue.

        Returns ``None`` on timeout or when the stream has finished; check
        :attr:`finished` to tell the two apart.
        """
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def finished(self) -> bool:
        return self._thread is not None and not self._thread.is_alive()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def join(self, timeout: float = 5.0) -> None:
        """Stop the background thread and wait for it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
