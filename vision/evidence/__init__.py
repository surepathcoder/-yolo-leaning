"""Event evidence capture.

Turns the frame a significant event fired on into a snapshot, a thumbnail and
a short clip. Produces plain ``bytes`` and never touches disk or the database
-- the backend's evidence service stores what this hands back.
"""

from vision.evidence.capture import (
    EvidenceArtifact,
    EvidenceCapturer,
    EvidenceSettings,
    EvidenceStats,
)
from vision.evidence.policy import (
    EVIDENCE_EVENT_TYPES,
    EVIDENCE_MIN_SEVERITY,
    NON_CAPTURING_EVENT_TYPES,
    should_capture,
)

__all__ = [
    "EVIDENCE_EVENT_TYPES",
    "EVIDENCE_MIN_SEVERITY",
    "NON_CAPTURING_EVENT_TYPES",
    "EvidenceArtifact",
    "EvidenceCapturer",
    "EvidenceSettings",
    "EvidenceStats",
    "should_capture",
]
