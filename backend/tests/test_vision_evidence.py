"""Vision engine -- event evidence capture (milestone 13).

The capturer sits on the pipeline's hot path, so the tests that matter most
assert it stays out of the way: an ordinary line crossing produces nothing, a
broken video codec is swallowed rather than raised, and the set of events it
fires on tracks the backend's alert policy exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import cv2
import numpy as np
import pytest

from app.services.alerts import should_alert
from vision.events.types import EventType, Severity, VisionEvent
from vision.evidence import EvidenceCapturer, EvidenceSettings, should_capture
from vision.evidence.capture import EvidenceArtifact
from vision.types import BoundingBox, Frame, TrackedObject

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def frame(index: int, *, fill: int = 120, size: tuple[int, int] = (480, 640)) -> Frame:
    image = np.full((*size, 3), fill, dtype=np.uint8)
    return Frame(
        image=image,
        index=index,
        timestamp=BASE + timedelta(seconds=index * 0.2),
        camera_id="cam-1",
    )


def event(
    *,
    event_type: EventType = EventType.RESTRICTED_AREA_ENTRY,
    severity: Severity = Severity.HIGH,
    track_key: str | None = "cam-1:sess:1",
    occurred_at: datetime = BASE,
) -> VisionEvent:
    return VisionEvent(
        event_type=event_type,
        severity=severity,
        camera_id="cam-1",
        occurred_at=occurred_at,
        track_key=track_key,
        summary="Person entered restricted zone.",
    )


def track(track_key: str = "cam-1:sess:1", bbox=(0.1, 0.1, 0.4, 0.9)) -> TrackedObject:
    return TrackedObject(
        track_id=1,
        class_id=0,
        class_name="person",
        camera_id="cam-1",
        session_id="sess",
        bbox=BoundingBox(*bbox),
        confidence=0.9,
        first_seen=BASE,
        last_seen=BASE,
    )


def capturer(collector: list[EvidenceArtifact], **overrides) -> EvidenceCapturer:
    settings = EvidenceSettings(
        enabled=True, pre_seconds=1.0, post_seconds=1.0, clip_fps=5.0, **overrides
    )
    return EvidenceCapturer(settings, collector.append)


# --------------------------------------------------------------- snapshot/thumb
def test_a_significant_event_yields_a_snapshot_and_thumbnail_immediately():
    got: list[EvidenceArtifact] = []
    cap = capturer(got)

    cap.observe(frame(0))
    cap.trigger(frame(1), event(), [track()])

    kinds = [a.kind for a in got]
    assert kinds.count("SNAPSHOT") == 1
    assert kinds.count("THUMBNAIL") == 1
    snapshot = next(a for a in got if a.kind == "SNAPSHOT")
    assert snapshot.mime_type == "image/jpeg"
    assert (snapshot.width, snapshot.height) == (640, 480)
    thumb = next(a for a in got if a.kind == "THUMBNAIL")
    assert thumb.width == 320 and thumb.height == 240


def test_thumbnail_is_not_produced_when_disabled():
    got: list[EvidenceArtifact] = []
    cap = capturer(got, thumbnail=False)
    cap.trigger(frame(1), event(), [track()])
    assert [a.kind for a in got] == ["SNAPSHOT"]


# ------------------------------------------------------------------------ clip
def test_clip_is_emitted_only_after_the_post_window_and_is_decodable(tmp_path):
    got: list[EvidenceArtifact] = []
    cap = capturer(got)

    for i in range(6):  # a second of pre-roll
        cap.observe(frame(i))
    cap.trigger(frame(6), event(), [track()])
    assert not any(a.kind == "CLIP" for a in got), "clip must wait for post frames"

    for i in range(7, 20):
        cap.observe(frame(i))

    clip = next(a for a in got if a.kind == "CLIP")
    assert clip.mime_type == "video/mp4"
    assert clip.duration_seconds and clip.duration_seconds > 0

    path = tmp_path / "clip.mp4"
    path.write_bytes(clip.data)
    reader = cv2.VideoCapture(str(path))
    frames = int(reader.get(cv2.CAP_PROP_FRAME_COUNT))
    reader.release()
    assert frames >= 5


def test_flush_finalises_a_clip_that_only_has_pre_event_frames():
    got: list[EvidenceArtifact] = []
    cap = capturer(got)
    for i in range(6):
        cap.observe(frame(i))
    cap.trigger(frame(6), event(), [track()])
    cap.flush()
    assert any(a.kind == "CLIP" for a in got)


# -------------------------------------------------------------------- no-ops
@pytest.mark.parametrize(
    "event_type,severity",
    [
        (EventType.LINE_CROSSED, Severity.MEDIUM),
        (EventType.PERSON_DETECTED, Severity.INFO),
        (EventType.PERSON_ENTERED_ZONE, Severity.LOW),
    ],
)
def test_noise_events_capture_nothing(event_type, severity):
    got: list[EvidenceArtifact] = []
    cap = capturer(got)
    ev = event(event_type=event_type, severity=severity)
    if should_capture(ev):
        cap.trigger(frame(1), ev, [track()])
    assert got == []


# ---------------------------------------------------------------- annotation
def test_annotation_boxes_the_triggering_track_only():
    got: list[EvidenceArtifact] = []
    cap = capturer(got)
    plain = frame(1)
    other = track("cam-1:sess:2", bbox=(0.6, 0.6, 0.9, 0.9))
    cap.trigger(plain, event(track_key="cam-1:sess:1"), [track("cam-1:sess:1"), other])

    snapshot = next(a for a in got if a.kind == "SNAPSHOT")
    assert snapshot.bbox == pytest.approx((0.1, 0.1, 0.4, 0.9))
    decoded = cv2.imdecode(np.frombuffer(snapshot.data, np.uint8), cv2.IMREAD_COLOR)
    # The source frame is a flat fill; the box draw is the only variation.
    assert decoded.std() > 0


# ------------------------------------------------------------------ failure
def test_a_broken_video_encoder_is_counted_not_raised(monkeypatch):
    got: list[EvidenceArtifact] = []
    cap = capturer(got)

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no codec")

    monkeypatch.setattr(cv2, "VideoWriter", Boom)

    for i in range(6):
        cap.observe(frame(i))
    cap.trigger(frame(6), event(), [track()])
    for i in range(7, 15):
        cap.observe(frame(i))

    assert not any(a.kind == "CLIP" for a in got)
    assert any(a.kind == "SNAPSHOT" for a in got)  # stills still work
    assert cap.stats.failures >= 1
    assert "no codec" in (cap.stats.last_error or "")


# ------------------------------------------------------------- anti-drift
@pytest.mark.parametrize("event_type", list(EventType))
def test_should_capture_matches_should_alert(event_type):
    """The vision-side capture policy must not drift from the backend's."""
    from app.models.enums import EventSeverity
    from app.models.enums import EventType as BackendEventType
    from app.models.event import Event

    for severity, backend_severity in [
        (Severity.INFO, EventSeverity.INFO),
        (Severity.LOW, EventSeverity.LOW),
        (Severity.MEDIUM, EventSeverity.MEDIUM),
        (Severity.HIGH, EventSeverity.HIGH),
        (Severity.CRITICAL, EventSeverity.CRITICAL),
    ]:
        vision_event = event(event_type=event_type, severity=severity, track_key=None)
        backend_event = Event(
            event_type=BackendEventType(event_type.value),
            severity=backend_severity,
        )
        assert should_capture(vision_event) == should_alert(backend_event), (
            f"{event_type.value} @ {severity.value}"
        )
