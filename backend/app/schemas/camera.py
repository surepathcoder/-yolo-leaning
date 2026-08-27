"""Camera request and response schemas.

The central rule: **no response schema has a password field**. Not masked, not
nulled — absent. A field that exists can be accidentally populated by a future
change; a field that does not exist cannot. ``has_credentials`` tells the UI
whether a password is stored without revealing anything about it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import CameraStatus, StreamProfile, VideoSourceKind
from app.schemas.common import APISchema


class CameraStreamEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: StreamProfile = StreamProfile.SUB
    stream_path: str = Field(min_length=1, max_length=500)
    width: int | None = Field(default=None, ge=16, le=16384)
    height: int | None = Field(default=None, ge=16, le=16384)
    fps: float | None = Field(default=None, gt=0, le=240)
    codec: str | None = Field(default=None, max_length=20)
    is_inference_source: bool = False


class CameraStreamRead(APISchema):
    id: uuid.UUID
    profile: StreamProfile
    stream_path: str
    width: int | None
    height: int | None
    fps: float | None
    codec: str | None
    is_inference_source: bool


class CameraBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    location: str | None = Field(default=None, max_length=200)

    source_kind: VideoSourceKind = VideoSourceKind.RTSP
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=554, ge=1, le=65535)
    stream_path: str | None = Field(default=None, max_length=500)
    #: FILE: path on disk. WEBCAM: device index. SYNTHETIC: scenario name.
    source_locator: str | None = Field(default=None, max_length=1000)
    username: str | None = Field(default=None, max_length=200)

    width: int | None = Field(default=None, ge=16, le=16384)
    height: int | None = Field(default=None, ge=16, le=16384)
    target_fps: float = Field(default=10.0, gt=0, le=60)
    transport: str = Field(default="tcp", pattern="^(tcp|udp)$")

    detection_enabled: bool = True
    confidence_threshold: float | None = Field(default=None, gt=0, le=1)
    detect_classes: list[str] = Field(default_factory=list, max_length=100)
    settings: dict = Field(default_factory=dict)

    @field_validator("host")
    @classmethod
    def _clean_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        host = value.strip()
        # Guard against someone pasting a whole URL, credentials and all, into
        # the host field -- which would store a password in a plaintext column.
        if "://" in host or "@" in host:
            raise ValueError("enter only the host or IP address here, not a full RTSP URL")
        return host

    @field_validator("stream_path")
    @classmethod
    def _clean_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = value.strip()
        if path and not path.startswith("/"):
            path = "/" + path
        return path

    @model_validator(mode="after")
    def _check_source_fields(self) -> CameraBase:
        if self.source_kind == VideoSourceKind.RTSP and not self.host:
            raise ValueError("an RTSP camera needs a host address")
        if self.source_kind == VideoSourceKind.FILE and not self.source_locator:
            raise ValueError("a file source needs source_locator set to a video path")
        return self


class CameraCreate(CameraBase):
    model_config = ConfigDict(extra="forbid")

    store_id: uuid.UUID
    #: Write-only. Encrypted immediately and never returned.
    password: str | None = Field(default=None, max_length=256)


class CameraUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    location: str | None = Field(default=None, max_length=200)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    stream_path: str | None = Field(default=None, max_length=500)
    source_locator: str | None = Field(default=None, max_length=1000)
    username: str | None = Field(default=None, max_length=200)
    #: Write-only. Omit to leave the stored credential untouched.
    password: str | None = Field(default=None, max_length=256)
    #: The only way to remove a stored credential.
    clear_password: bool = False

    width: int | None = Field(default=None, ge=16, le=16384)
    height: int | None = Field(default=None, ge=16, le=16384)
    target_fps: float | None = Field(default=None, gt=0, le=60)
    transport: str | None = Field(default=None, pattern="^(tcp|udp)$")

    is_enabled: bool | None = None
    detection_enabled: bool | None = None
    confidence_threshold: float | None = Field(default=None, gt=0, le=1)
    detect_classes: list[str] | None = Field(default=None, max_length=100)
    settings: dict | None = None

    @field_validator("host")
    @classmethod
    def _clean_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        host = value.strip()
        if "://" in host or "@" in host:
            raise ValueError("enter only the host or IP address here, not a full RTSP URL")
        return host


class CameraRead(APISchema):
    """A camera as returned by the API.

    There is no ``password`` field and no assembled URL. ``connection_hint``
    shows the host and path with no credentials, which is what an operator
    needs for diagnostics.
    """

    id: uuid.UUID
    organization_id: uuid.UUID
    store_id: uuid.UUID
    name: str
    description: str | None
    location: str | None

    source_kind: VideoSourceKind
    host: str | None
    port: int | None
    stream_path: str | None
    source_locator: str | None
    username: str | None
    #: Whether a password is stored. Never the password itself.
    has_credentials: bool = False
    connection_hint: str | None = None

    width: int | None
    height: int | None
    target_fps: float
    transport: str
    measured_fps: float | None

    is_enabled: bool
    detection_enabled: bool
    confidence_threshold: float | None
    detect_classes: list
    settings: dict

    status: CameraStatus
    last_seen_at: datetime | None
    last_connected_at: datetime | None
    last_error: str | None
    consecutive_failures: int

    created_at: datetime
    updated_at: datetime
    streams: list[CameraStreamRead] = Field(default_factory=list)


class CameraSummary(APISchema):
    """A camera as it appears in a list or grid."""

    id: uuid.UUID
    store_id: uuid.UUID
    name: str
    location: str | None
    status: CameraStatus
    is_enabled: bool
    last_seen_at: datetime | None
    measured_fps: float | None


class ConnectionTestResult(BaseModel):
    """Outcome of a live connection test.

    Not an error response: "this camera is unreachable" is an answer, and the
    UI needs the detail to help an installer fix it.
    """

    camera_id: uuid.UUID
    success: bool
    message: str
    resolution: list[int] | None = None
    source_fps: float | None = None
    latency_ms: float | None = None


class SetStreamsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    streams: list[CameraStreamEntry] = Field(min_length=0, max_length=6)


class CameraHealthSummary(BaseModel):
    """Counts per status for the dashboard tile."""

    ONLINE: int = 0
    OFFLINE: int = 0
    CONNECTING: int = 0
    ERROR: int = 0
    DISABLED: int = 0
    total: int = 0


def build_camera_read(camera) -> CameraRead:
    """Serialise a camera, deriving the credential-free fields.

    Written explicitly rather than via ``from_attributes`` so that adding a
    column to the model can never silently start exposing it.
    """
    hint = None
    if camera.source_kind == VideoSourceKind.RTSP and camera.host:
        hint = f"rtsp://{camera.host}:{camera.port or 554}{camera.stream_path or '/'}"
    elif camera.source_locator:
        hint = camera.source_locator

    return CameraRead(
        id=camera.id,
        organization_id=camera.organization_id,
        store_id=camera.store_id,
        name=camera.name,
        description=camera.description,
        location=camera.location,
        source_kind=camera.source_kind,
        host=camera.host,
        port=camera.port,
        stream_path=camera.stream_path,
        source_locator=camera.source_locator,
        username=camera.username,
        has_credentials=bool(camera.encrypted_password),
        connection_hint=hint,
        width=camera.width,
        height=camera.height,
        target_fps=camera.target_fps,
        transport=camera.transport,
        measured_fps=camera.measured_fps,
        is_enabled=camera.is_enabled,
        detection_enabled=camera.detection_enabled,
        confidence_threshold=camera.confidence_threshold,
        detect_classes=list(camera.detect_classes or []),
        settings=dict(camera.settings or {}),
        status=camera.status,
        last_seen_at=camera.last_seen_at,
        last_connected_at=camera.last_connected_at,
        last_error=camera.last_error,
        consecutive_failures=camera.consecutive_failures,
        created_at=camera.created_at,
        updated_at=camera.updated_at,
        streams=[CameraStreamRead.model_validate(s) for s in camera.streams],
    )
