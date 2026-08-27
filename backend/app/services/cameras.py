"""Camera management.

The security-critical part of this module is credential handling. The rules,
enforced here and tested in ``test_camera_api.py``:

* A camera password is **encrypted at rest** and stored separately from the
  connection details. There is no plaintext password column and no stored URL.
* A password is **never** returned by any API response, in any shape.
* Decryption happens only when a pipeline is about to connect, and the
  assembled URL exists only as a local variable.
* Connection errors name the host, never the URL, so a stack trace or a log
  line cannot leak credentials.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import CameraConnectionError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.security import decrypt_credential, encrypt_credential
from app.core.tenancy import TenantScope
from app.models import Camera, CameraStream, Store
from app.models.enums import AuditAction, CameraStatus, VideoSourceKind
from app.services.audit import record_audit
from app.services.plan_limits import LimitKey, enforce_limit
from app.services.stores import get_store

log = get_logger(__name__)

#: How many consecutive connection failures before a camera is marked ERROR
#: rather than merely OFFLINE. A single failure is usually a transient network
#: blip; three in a row is a fault worth showing an operator.
FAILURES_BEFORE_ERROR = 3


def _base_query():
    return select(Camera).where(Camera.deleted_at.is_(None))


def list_cameras(
    session: Session,
    scope: TenantScope,
    *,
    store_id: uuid.UUID | None = None,
    status: CameraStatus | None = None,
    enabled_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Camera], int]:
    scope.require(Permission.CAMERA_READ)

    statement = scope.apply(_base_query(), Camera)
    if store_id is not None:
        scope.require_store(store_id)
        statement = statement.where(Camera.store_id == store_id)
    if status is not None:
        statement = statement.where(Camera.status == status)
    if enabled_only:
        statement = statement.where(Camera.is_enabled.is_(True))

    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = session.scalars(
        statement.options(selectinload(Camera.streams))
        .order_by(Camera.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total


def get_camera(session: Session, scope: TenantScope, camera_id: uuid.UUID) -> Camera:
    scope.require(Permission.CAMERA_READ)
    camera = session.scalar(
        scope.apply(_base_query(), Camera)
        .where(Camera.id == camera_id)
        .options(selectinload(Camera.streams))
    )
    if camera is None:
        raise NotFoundError("Camera not found.", details={"camera_id": str(camera_id)})
    return camera


def create_camera(
    session: Session,
    scope: TenantScope,
    *,
    store_id: uuid.UUID,
    name: str,
    source_kind: VideoSourceKind = VideoSourceKind.RTSP,
    password: str | None = None,
    **fields,
) -> Camera:
    """Add a camera. ``password`` is encrypted immediately and then discarded."""
    scope.require(Permission.CAMERA_WRITE)
    store: Store = get_store(session, scope, store_id)
    scope.require_store(store.id)

    enforce_limit(session, store.organization_id, LimitKey.MAX_CAMERAS)

    duplicate = session.scalar(
        _base_query().where(Camera.store_id == store.id, Camera.name == name.strip())
    )
    if duplicate is not None:
        raise ConflictError(
            f"A camera named '{name}' already exists in this store.",
            details={"field": "name"},
        )

    camera = Camera(
        organization_id=store.organization_id,
        store_id=store.id,
        name=name.strip(),
        source_kind=source_kind,
        status=CameraStatus.OFFLINE,
        **fields,
    )
    if password:
        camera.encrypted_password = encrypt_credential(password)

    session.add(camera)
    session.flush()

    record_audit(
        session,
        action=AuditAction.CREATE,
        organization_id=camera.organization_id,
        user=None,
        resource_type="camera",
        resource_id=str(camera.id),
        description=f"Camera '{camera.name}' added to store '{store.name}'",
        # The password is deliberately absent; record only that one was set.
        changes={"has_credentials": bool(password), "source_kind": source_kind.value},
    )
    log.info(
        "camera_created",
        camera_id=str(camera.id),
        store_id=str(store.id),
        source_kind=source_kind.value,
    )
    return camera


def update_camera(
    session: Session,
    scope: TenantScope,
    camera_id: uuid.UUID,
    *,
    password: str | None = None,
    clear_password: bool = False,
    **changes,
) -> Camera:
    """Update a camera.

    ``password`` is write-only: omitting it leaves the stored credential
    untouched, and there is no way to read it back. ``clear_password``
    explicitly removes it, which is the only way to go from "has credentials"
    to "has none".
    """
    scope.require(Permission.CAMERA_WRITE)
    camera = get_camera(session, scope, camera_id)
    scope.require_store(camera.store_id)

    applied: dict[str, object] = {}
    for field, value in changes.items():
        if value is None or not hasattr(camera, field):
            continue
        if getattr(camera, field) != value:
            applied[field] = value
            setattr(camera, field, value)

    if clear_password:
        camera.encrypted_password = None
        applied["credentials"] = "cleared"
    elif password:
        camera.encrypted_password = encrypt_credential(password)
        applied["credentials"] = "updated"

    # A camera that has just been disabled must not linger as ONLINE on the
    # dashboard: the pipeline has stopped, so the status is no longer true.
    if changes.get("is_enabled") is False:
        camera.status = CameraStatus.DISABLED
    elif changes.get("is_enabled") is True and camera.status == CameraStatus.DISABLED:
        camera.status = CameraStatus.OFFLINE

    if applied:
        session.flush()
        record_audit(
            session,
            action=AuditAction.UPDATE,
            organization_id=camera.organization_id,
            user=None,
            resource_type="camera",
            resource_id=str(camera.id),
            description=f"Camera '{camera.name}' updated",
            changes=applied,
        )
    return camera


def delete_camera(session: Session, scope: TenantScope, camera_id: uuid.UUID) -> None:
    """Soft-delete a camera and destroy its stored credentials.

    The credential is cleared on delete rather than left encrypted: a deleted
    camera has no legitimate use for it, and holding a secret with no owner is
    a liability.
    """
    scope.require(Permission.CAMERA_DELETE)
    camera = get_camera(session, scope, camera_id)
    scope.require_store(camera.store_id)

    camera.deleted_at = datetime.now(UTC)
    camera.is_enabled = False
    camera.status = CameraStatus.DISABLED
    camera.encrypted_password = None
    session.flush()

    record_audit(
        session,
        action=AuditAction.DELETE,
        organization_id=camera.organization_id,
        user=None,
        resource_type="camera",
        resource_id=str(camera.id),
        description=f"Camera '{camera.name}' deleted; stored credentials destroyed",
    )
    log.info("camera_deleted", camera_id=str(camera.id))


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def build_video_source(camera: Camera, *, target_fps: float = 0.0):
    """Construct the vision-engine source for a camera.

    This is the **only** place a camera's password is decrypted, and the only
    place a playable URL exists. The URL is assembled inside the source object
    and is never stored, logged or returned.
    """
    from vision.cameras import (
        LocalVideoSource,
        RTSPVideoSource,
        SyntheticVideoSource,
        WebcamSource,
    )

    camera_id = str(camera.id)
    fps = target_fps or camera.target_fps

    if camera.source_kind == VideoSourceKind.RTSP:
        if not camera.host:
            raise CameraConnectionError(
                "This camera has no host address configured.",
                details={"camera_id": camera_id},
            )
        password = (
            decrypt_credential(camera.encrypted_password) if camera.encrypted_password else None
        )
        return RTSPVideoSource(
            host=camera.host,
            camera_id=camera_id,
            port=camera.port or 554,
            path=camera.stream_path or "/",
            username=camera.username,
            password=password,
            transport=camera.transport or "tcp",
            target_fps=fps,
        )

    if camera.source_kind == VideoSourceKind.FILE:
        if not camera.source_locator:
            raise CameraConnectionError(
                "This camera has no video file configured.",
                details={"camera_id": camera_id},
            )
        return LocalVideoSource(camera.source_locator, camera_id=camera_id, target_fps=fps)

    if camera.source_kind == VideoSourceKind.WEBCAM:
        try:
            index = int(camera.source_locator or 0)
        except ValueError:
            index = 0
        return WebcamSource(index, camera_id=camera_id, target_fps=fps)

    if camera.source_kind == VideoSourceKind.SYNTHETIC:
        return SyntheticVideoSource(camera_id=camera_id, target_fps=fps)

    raise CameraConnectionError(
        f"Unsupported camera source type: {camera.source_kind.value}",
        details={"camera_id": camera_id},
    )


def test_connection(
    session: Session, scope: TenantScope, camera_id: uuid.UUID, *, timeout: float = 10.0
) -> dict:
    """Try to open the camera and read one frame.

    Reading a frame matters: an RTSP socket can open against a camera that
    then delivers nothing, which is a common failure with a misconfigured
    stream path. "Connected" without a frame is not a working camera.

    Never raises for a connection failure -- it returns a structured result,
    because "this camera is unreachable" is an answer, not an error.
    """
    scope.require(Permission.CAMERA_CONTROL)
    camera = get_camera(session, scope, camera_id)
    scope.require_store(camera.store_id)

    started = datetime.now(UTC)
    result: dict = {
        "camera_id": str(camera.id),
        "success": False,
        "message": "",
        "resolution": None,
        "source_fps": None,
        "latency_ms": None,
    }

    source = None
    try:
        source = build_video_source(camera, target_fps=0)
        source.open()
        frame = None
        # A camera can take a moment to deliver its first frame after the
        # socket opens; a handful of attempts avoids a false negative.
        for _ in range(30):
            frame = source.read()
            if frame is not None:
                break
        latency_ms = (datetime.now(UTC) - started).total_seconds() * 1000

        if frame is None:
            result["message"] = "Connected, but the camera sent no video."
            camera.status = CameraStatus.ERROR
            camera.last_error = result["message"]
            camera.consecutive_failures += 1
        else:
            result.update(
                success=True,
                message="Connected successfully.",
                resolution=[frame.width, frame.height],
                source_fps=source.source_fps,
                latency_ms=round(latency_ms, 1),
            )
            camera.status = CameraStatus.ONLINE
            camera.last_error = None
            camera.consecutive_failures = 0
            camera.last_connected_at = datetime.now(UTC)
            camera.last_seen_at = camera.last_connected_at
            if camera.width is None or camera.height is None:
                camera.width, camera.height = frame.width, frame.height

    except Exception as exc:
        # The message is written by the source and is credential-free by
        # contract; it is still truncated in case a driver appends detail.
        message = str(exc)[:200] or "Unable to connect to the camera."
        result["message"] = message
        camera.consecutive_failures += 1
        camera.last_error = message
        camera.status = (
            CameraStatus.ERROR
            if camera.consecutive_failures >= FAILURES_BEFORE_ERROR
            else CameraStatus.OFFLINE
        )
        log.warning(
            "camera_connection_test_failed",
            camera_id=str(camera.id),
            host=camera.host,
            consecutive_failures=camera.consecutive_failures,
            error=message,
        )
    finally:
        if source is not None:
            source.close()

    session.flush()
    record_audit(
        session,
        action=AuditAction.CONFIG_CHANGED,
        organization_id=camera.organization_id,
        user=None,
        resource_type="camera",
        resource_id=str(camera.id),
        description=f"Connection test: {result['message']}",
        succeeded=result["success"],
    )
    return result


def record_status(
    session: Session,
    camera_id: uuid.UUID,
    status: CameraStatus,
    *,
    error: str | None = None,
    measured_fps: float | None = None,
) -> None:
    """Update a camera's live status from a running pipeline.

    Takes no scope: the caller is a worker acting on a camera it was given,
    not a user making a request. It is deliberately not exposed over HTTP.
    """
    camera = session.get(Camera, camera_id)
    if camera is None:
        return

    camera.status = status
    camera.last_seen_at = datetime.now(UTC)
    if measured_fps is not None:
        camera.measured_fps = measured_fps

    if status == CameraStatus.ONLINE:
        camera.consecutive_failures = 0
        camera.last_error = None
        camera.last_connected_at = camera.last_seen_at
    elif error:
        camera.last_error = error[:500]
        camera.consecutive_failures += 1

    session.flush()


def camera_health_summary(session: Session, scope: TenantScope) -> dict:
    """Counts per status, for the dashboard's camera-health tile."""
    scope.require(Permission.CAMERA_READ)
    statement = scope.apply(
        select(Camera.status, func.count()).where(Camera.deleted_at.is_(None)), Camera
    ).group_by(Camera.status)

    counts = {status.value: 0 for status in CameraStatus}
    for status, count in session.execute(statement):
        counts[status.value] = int(count)
    counts["total"] = sum(counts[s.value] for s in CameraStatus)
    return counts


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------


def set_streams(
    session: Session, scope: TenantScope, camera_id: uuid.UUID, streams: list[dict]
) -> list[CameraStream]:
    """Replace a camera's stream profiles.

    Exactly one stream may be the inference source. If none is marked, the
    lowest-resolution one is chosen: at the model's 640 px input the extra
    pixels of a main stream are discarded anyway, and decoding them costs
    several times more CPU on an edge box.
    """
    scope.require(Permission.CAMERA_WRITE)
    camera = get_camera(session, scope, camera_id)
    scope.require_store(camera.store_id)

    for existing in list(camera.streams):
        session.delete(existing)
    session.flush()

    rows = [
        CameraStream(
            camera_id=camera.id,
            profile=entry["profile"],
            stream_path=entry["stream_path"],
            width=entry.get("width"),
            height=entry.get("height"),
            fps=entry.get("fps"),
            codec=entry.get("codec"),
            is_inference_source=bool(entry.get("is_inference_source", False)),
        )
        for entry in streams
    ]

    marked = [row for row in rows if row.is_inference_source]
    if len(marked) > 1:
        raise ConflictError("Only one stream may be the inference source.")
    if not marked and rows:
        smallest = min(rows, key=lambda r: (r.width or 10**6) * (r.height or 10**6))
        smallest.is_inference_source = True

    session.add_all(rows)
    session.flush()
    return rows
