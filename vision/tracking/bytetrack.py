"""ByteTrack multi-object tracker.

The idea that makes ByteTrack work is simple and worth stating plainly: most
trackers throw away low-confidence detections, but a person who is partially
occluded produces exactly those. Discarding them ends the track, and when the
person reappears they get a new id. ByteTrack instead associates in two passes:

1. **High-confidence pass** — match confident detections to all tracks.
2. **Low-confidence pass** — match what is left over against the *unmatched
   tracks only*.

The second pass is what keeps "person 42" as person 42 when they walk behind a
shelf. It costs almost nothing, because the low-confidence detections were
already computed.

A third, VisionGuard-specific pass follows. IoU association assumes an object
overlaps itself between consecutive frames, which stops being true when motion
is large relative to object size -- a person walking past a doorway camera at
5 FPS, or anyone running. Their boxes then share no pixels at all and pure IoU
matching drops the track and issues a new id. The recovery pass matches such
tracks by **centre proximity**, gated on comparable box size so a distant
person is never matched to a nearby one.

A track must be matched ``min_hits`` times before it is reported, which
suppresses single-frame false positives, and survives ``max_age`` frames
without a match before being closed, which bridges brief occlusions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

import numpy as np

from vision.tracking.assignment import (
    associate,
    centre_distance_cost_matrix,
    iou_cost_matrix,
    size_ratio_matrix,
)
from vision.tracking.kalman import KalmanBoxFilter
from vision.types import BoundingBox, Detection, TrackedObject, new_session_id


class TrackState(StrEnum):
    TENTATIVE = "TENTATIVE"  # seen, not yet confirmed -- not reported
    CONFIRMED = "CONFIRMED"  # reported to the rest of the pipeline
    LOST = "LOST"  # unmatched recently, still a candidate for re-association
    REMOVED = "REMOVED"


@dataclass
class _Track:
    """Internal tracker state. ``TrackedObject`` is the public view."""

    track_id: int
    class_id: int
    class_name: str
    confidence: float
    mean: np.ndarray
    covariance: np.ndarray
    first_seen: datetime
    last_seen: datetime
    state: TrackState = TrackState.TENTATIVE
    hits: int = 1
    age: int = 0
    time_since_update: int = 0
    best_confidence: float = 0.0
    #: The public object, created when the track is confirmed.
    public: TrackedObject | None = None
    history: list[tuple[float, float, float, float]] = field(default_factory=list)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return KalmanBoxFilter.to_bbox(self.mean)


class ByteTracker:
    """Multi-object tracker producing stable ids across frames.

    One tracker instance per camera: track ids are only meaningful within one
    camera and one session.
    """

    def __init__(
        self,
        *,
        high_threshold: float = 0.5,
        low_threshold: float = 0.1,
        match_threshold: float = 0.8,
        second_match_threshold: float = 0.5,
        max_age: int = 30,
        min_hits: int = 3,
        camera_id: str = "camera",
        session_id: str | None = None,
        proximity_recovery: bool = True,
        max_proximity_distance: float = 2.5,
        max_size_ratio: float = 2.5,
    ) -> None:
        #: Detections at or above this confidence go in the first pass.
        self.high_threshold = high_threshold
        #: Detections below this are discarded entirely.
        self.low_threshold = low_threshold
        #: Maximum ``1 - IoU`` accepted in the first pass.
        self.match_threshold = match_threshold
        #: Stricter for the second pass: a low-confidence detection needs
        #: better geometric agreement before we trust it.
        self.second_match_threshold = second_match_threshold
        self.max_age = max_age
        self.min_hits = min_hits
        self.camera_id = camera_id
        self.session_id = session_id or new_session_id()
        #: Recover tracks whose motion outran their own bounding box.
        self.proximity_recovery = proximity_recovery
        #: Maximum centre distance, in units of mean box diagonal.
        self.max_proximity_distance = max_proximity_distance
        #: Maximum area ratio between a track and a candidate detection.
        self.max_size_ratio = max_size_ratio

        self._kalman = KalmanBoxFilter()
        self._tracks: list[_Track] = []
        self._next_id = 1
        self.frame_count = 0

    # ------------------------------------------------------------- accessors
    @property
    def active_tracks(self) -> list[TrackedObject]:
        """Confirmed, currently-visible tracks."""
        return [
            track.public
            for track in self._tracks
            if track.state == TrackState.CONFIRMED
            and track.time_since_update == 0
            and track.public is not None
        ]

    @property
    def all_tracks(self) -> list[TrackedObject]:
        """Every confirmed track, including ones temporarily lost."""
        return [
            track.public
            for track in self._tracks
            if track.state in (TrackState.CONFIRMED, TrackState.LOST) and track.public is not None
        ]

    def get_track(self, track_id: int) -> TrackedObject | None:
        for track in self._tracks:
            if track.track_id == track_id and track.public is not None:
                return track.public
        return None

    def reset(self, *, new_session: bool = True) -> None:
        """Drop all state, e.g. after a reconnect.

        A new session id is issued by default so ids from before the break
        cannot be confused with ids after it -- the tracker restarts its
        numbering at 1, and without a new session two different people would
        share the key ``camera:session:1``.
        """
        self._tracks.clear()
        self._next_id = 1
        self.frame_count = 0
        if new_session:
            self.session_id = new_session_id()

    # ---------------------------------------------------------------- update
    def update(self, detections: list[Detection], timestamp: datetime) -> list[TrackedObject]:
        """Advance the tracker by one frame. Returns the visible tracks."""
        self.frame_count += 1

        for track in self._tracks:
            track.age += 1
            track.mean, track.covariance = self._kalman.predict(track.mean, track.covariance)

        usable = [d for d in detections if d.confidence >= self.low_threshold]
        high = [d for d in usable if d.confidence >= self.high_threshold]
        low = [d for d in usable if d.confidence < self.high_threshold]

        candidates = [
            track
            for track in self._tracks
            if track.state in (TrackState.TENTATIVE, TrackState.CONFIRMED, TrackState.LOST)
        ]

        # --- pass 1: confident detections against every live track ----------
        matches, unmatched_tracks, unmatched_high = self._match(
            candidates, high, self.match_threshold
        )
        for track_index, detection_index in matches:
            self._update_track(candidates[track_index], high[detection_index], timestamp)

        # --- pass 2: the leftovers -------------------------------------------
        # This is the whole point of ByteTrack. An occluded person's detection
        # confidence collapses; matching those against the tracks that just
        # went unmatched is what preserves their id.
        remaining = [candidates[i] for i in unmatched_tracks]
        second_matches, still_unmatched, _ = self._match(
            remaining, low, self.second_match_threshold
        )
        for track_index, detection_index in second_matches:
            self._update_track(remaining[track_index], low[detection_index], timestamp)

        # --- pass 3: proximity recovery --------------------------------------
        # Everything still unmatched: a track whose object moved further than
        # its own width, and a confident detection with no home. If they are
        # close and similarly sized, they are almost certainly the same object.
        leftover_tracks = [remaining[i] for i in still_unmatched]
        leftover_detections = [high[i] for i in unmatched_high]
        if self.proximity_recovery and leftover_tracks and leftover_detections:
            recovered, unrecovered_tracks, unrecovered_detections = self._match_by_proximity(
                leftover_tracks, leftover_detections
            )
            for track_index, detection_index in recovered:
                self._update_track(
                    leftover_tracks[track_index],
                    leftover_detections[detection_index],
                    timestamp,
                )
            for track_index in unrecovered_tracks:
                self._mark_missed(leftover_tracks[track_index])
            unmatched_high = [unmatched_high[i] for i in unrecovered_detections]
        else:
            for track in leftover_tracks:
                self._mark_missed(track)

        # --- new tracks -------------------------------------------------------
        # Only confident, unmatched detections start a track. A low-confidence
        # detection that matches nothing is far more likely to be noise than a
        # new object, and starting tracks from noise floods the event engine.
        for detection_index in unmatched_high:
            self._start_track(high[detection_index], timestamp)

        self._prune()
        return self.active_tracks

    # --------------------------------------------------------------- helpers
    def _match(
        self, tracks: list[_Track], detections: list[Detection], threshold: float
    ) -> tuple[list[tuple[int, int]], list[int], list[int]]:
        if not tracks or not detections:
            return [], list(range(len(tracks))), list(range(len(detections)))

        track_boxes = np.array([track.bbox for track in tracks], dtype=np.float64)
        detection_boxes = np.array([d.bbox.as_tuple() for d in detections], dtype=np.float64)
        cost = iou_cost_matrix(track_boxes, detection_boxes)

        # A track may only match a detection of the same class. Without this a
        # person walking in front of a parked car can inherit the car's id.
        for i, track in enumerate(tracks):
            for j, detection in enumerate(detections):
                if track.class_id != detection.class_id:
                    cost[i, j] = 1.0

        return associate(cost, threshold)

    def _match_by_proximity(
        self, tracks: list[_Track], detections: list[Detection]
    ) -> tuple[list[tuple[int, int]], list[int], list[int]]:
        """Associate by centre distance when IoU found nothing.

        Two gates keep this conservative -- it runs only on leftovers, and a
        wrong match here creates an id swap, which is worse than a new id:

        * centre distance must be within ``max_proximity_distance`` box
          diagonals;
        * the boxes must be within ``max_size_ratio`` in area, so a person at
          the back of the shop is never matched to one at the counter.
        """
        track_boxes = np.array([track.bbox for track in tracks], dtype=np.float64)
        detection_boxes = np.array([d.bbox.as_tuple() for d in detections], dtype=np.float64)

        cost = centre_distance_cost_matrix(track_boxes, detection_boxes)
        ratios = size_ratio_matrix(track_boxes, detection_boxes)

        blocked = self.max_proximity_distance + 1.0
        for i, track in enumerate(tracks):
            for j, detection in enumerate(detections):
                if track.class_id != detection.class_id or ratios[i, j] > self.max_size_ratio:
                    cost[i, j] = blocked

        return associate(cost, self.max_proximity_distance)

    def _update_track(self, track: _Track, detection: Detection, timestamp: datetime) -> None:
        measurement = self._kalman.to_measurement(detection.bbox.as_tuple())
        track.mean, track.covariance = self._kalman.update(
            track.mean, track.covariance, measurement
        )
        track.hits += 1
        track.time_since_update = 0
        track.last_seen = timestamp
        track.confidence = detection.confidence
        track.best_confidence = max(track.best_confidence, detection.confidence)
        track.history.append(track.bbox)

        if track.state == TrackState.TENTATIVE and track.hits >= self.min_hits:
            track.state = TrackState.CONFIRMED
            track.public = self._build_public(track, timestamp)
        elif track.state == TrackState.LOST:
            track.state = TrackState.CONFIRMED

        if track.public is not None:
            track.public.update(BoundingBox(*track.bbox), detection.confidence, timestamp)

    def _mark_missed(self, track: _Track) -> None:
        track.time_since_update += 1
        if track.state == TrackState.TENTATIVE:
            # An unconfirmed track that misses even once was probably noise.
            track.state = TrackState.REMOVED
        elif track.state == TrackState.CONFIRMED:
            track.state = TrackState.LOST
        if track.public is not None:
            track.public.mark_missed()
            track.public.is_active = False

    def _start_track(self, detection: Detection, timestamp: datetime) -> None:
        measurement = self._kalman.to_measurement(detection.bbox.as_tuple())
        mean, covariance = self._kalman.initiate(measurement)
        track = _Track(
            track_id=self._next_id,
            class_id=detection.class_id,
            class_name=detection.class_name,
            confidence=detection.confidence,
            best_confidence=detection.confidence,
            mean=mean,
            covariance=covariance,
            first_seen=timestamp,
            last_seen=timestamp,
        )
        self._next_id += 1

        if self.min_hits <= 1:
            track.state = TrackState.CONFIRMED
            track.public = self._build_public(track, timestamp)
        self._tracks.append(track)

    def _build_public(self, track: _Track, timestamp: datetime) -> TrackedObject:
        return TrackedObject(
            track_id=track.track_id,
            class_id=track.class_id,
            class_name=track.class_name,
            camera_id=self.camera_id,
            session_id=self.session_id,
            bbox=BoundingBox(*track.bbox),
            confidence=track.confidence,
            best_confidence=track.best_confidence,
            first_seen=track.first_seen,
            last_seen=timestamp,
            frame_count=track.hits,
        )

    def _prune(self) -> None:
        self._tracks = [
            track
            for track in self._tracks
            if track.state != TrackState.REMOVED and track.time_since_update <= self.max_age
        ]

    def __repr__(self) -> str:
        return (
            f"<ByteTracker camera={self.camera_id} frame={self.frame_count} "
            f"active={len(self.active_tracks)}>"
        )


def build_tracker(
    tracker_type: str = "bytetrack",
    *,
    camera_id: str = "camera",
    max_age: int = 30,
    min_hits: int = 3,
    iou_threshold: float = 0.3,
    low_confidence_threshold: float = 0.1,
    session_id: str | None = None,
) -> ByteTracker:
    """Build the configured tracker.

    BoT-SORT is accepted as a name but currently maps to ByteTrack: BoT-SORT's
    advantage is its appearance-embedding re-identification model, which is a
    second network per frame. On the CPU-class hardware this product targets
    that would roughly halve the frame rate, and re-identification also raises
    privacy questions that ``docs/PRIVACY.md`` deliberately keeps closed.
    Camera-motion compensation, BoT-SORT's other component, buys little on the
    fixed cameras VisionGuard reads.
    """
    if tracker_type.lower() not in {"bytetrack", "botsort", "bot-sort"}:
        raise ValueError(f"Unknown tracker type: {tracker_type!r}")
    return ByteTracker(
        camera_id=camera_id,
        max_age=max_age,
        min_hits=min_hits,
        match_threshold=1.0 - iou_threshold,
        low_threshold=low_confidence_threshold,
        session_id=session_id or uuid.uuid4().hex[:12],
    )
