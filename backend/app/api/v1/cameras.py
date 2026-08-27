"""Camera endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, status

from app.api.deps import CurrentUser, DbSession, Scope, get_client_ip
from app.models.enums import AuditAction, CameraStatus
from app.schemas.camera import (
    CameraCreate,
    CameraHealthSummary,
    CameraRead,
    CameraStreamRead,
    CameraSummary,
    CameraUpdate,
    ConnectionTestResult,
    SetStreamsRequest,
    build_camera_read,
)
from app.schemas.common import MessageResponse, Page
from app.services import cameras as camera_service
from app.services import pipeline_manager as pipeline_service
from app.services.audit import record_audit

router = APIRouter(prefix="/cameras", tags=["cameras"])


@router.get("", response_model=Page[CameraSummary], summary="List cameras")
def list_cameras(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    store_id: uuid.UUID | None = Query(None),
    camera_status: CameraStatus | None = Query(None, alias="status"),
    enabled_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows, total = camera_service.list_cameras(
        session,
        scope,
        store_id=store_id,
        status=camera_status,
        enabled_only=enabled_only,
        limit=limit,
        offset=offset,
    )
    return Page[CameraSummary](
        items=[CameraSummary.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/health", response_model=CameraHealthSummary, summary="Camera health counts")
def camera_health(session: DbSession, scope: Scope, _user: CurrentUser):
    """Counts per status, for the dashboard's camera-health tile.

    Declared before ``/{camera_id}`` so "health" is not parsed as an id.
    """
    return CameraHealthSummary(**camera_service.camera_health_summary(session, scope))


@router.post(
    "",
    response_model=CameraRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a camera",
)
def create_camera(
    payload: CameraCreate,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    """Add a camera.

    ``password`` is write-only: it is encrypted on arrival and no response
    ever returns it.
    """
    data = payload.model_dump(exclude={"store_id", "password"})
    camera = camera_service.create_camera(
        session,
        scope,
        store_id=payload.store_id,
        password=payload.password,
        **data,
    )
    record_audit(
        session,
        action=AuditAction.CREATE,
        organization_id=camera.organization_id,
        user=user,
        resource_type="camera",
        resource_id=str(camera.id),
        description=f"Camera '{camera.name}' added",
        ip_address=get_client_ip(request),
        request_id=getattr(request.state, "request_id", None),
    )
    session.commit()
    session.refresh(camera)
    return build_camera_read(camera)


@router.get("/{camera_id}", response_model=CameraRead, summary="Get a camera")
def get_camera(camera_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    return build_camera_read(camera_service.get_camera(session, scope, camera_id))


@router.patch("/{camera_id}", response_model=CameraRead, summary="Update a camera")
def update_camera(
    camera_id: uuid.UUID,
    payload: CameraUpdate,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    changes = payload.model_dump(exclude_unset=True)
    password = changes.pop("password", None)
    clear_password = changes.pop("clear_password", False)

    camera = camera_service.update_camera(
        session,
        scope,
        camera_id,
        password=password,
        clear_password=clear_password,
        **changes,
    )
    record_audit(
        session,
        action=AuditAction.UPDATE,
        organization_id=camera.organization_id,
        user=user,
        resource_type="camera",
        resource_id=str(camera.id),
        description=f"Camera '{camera.name}' updated",
        # sanitize_changes redacts the password key; it is popped above anyway.
        changes=changes,
        ip_address=get_client_ip(request),
    )
    session.commit()
    session.refresh(camera)
    return build_camera_read(camera)


@router.delete("/{camera_id}", response_model=MessageResponse, summary="Delete a camera")
def delete_camera(
    camera_id: uuid.UUID,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    camera = camera_service.get_camera(session, scope, camera_id)
    name, organization_id = camera.name, camera.organization_id

    camera_service.delete_camera(session, scope, camera_id)
    record_audit(
        session,
        action=AuditAction.DELETE,
        organization_id=organization_id,
        user=user,
        resource_type="camera",
        resource_id=str(camera_id),
        description=f"Camera '{name}' deleted",
        ip_address=get_client_ip(request),
    )
    session.commit()
    return MessageResponse(
        message=f"Camera '{name}' deleted. Its stored credentials were destroyed."
    )


@router.post(
    "/{camera_id}/test",
    response_model=ConnectionTestResult,
    summary="Test the camera connection",
)
def test_camera(
    camera_id: uuid.UUID,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    """Open the stream and read one frame.

    Returns 200 with ``success: false`` when the camera is unreachable --
    that is a diagnostic result, not a server error, and the installer needs
    the detail to fix it.
    """
    result = camera_service.test_connection(session, scope, camera_id)
    record_audit(
        session,
        action=AuditAction.CONFIG_CHANGED,
        organization_id=camera_service.get_camera(session, scope, camera_id).organization_id,
        user=user,
        resource_type="camera",
        resource_id=str(camera_id),
        description=f"Connection test: {result['message']}",
        succeeded=result["success"],
        ip_address=get_client_ip(request),
    )
    session.commit()
    return ConnectionTestResult(**result)


@router.post("/{camera_id}/enable", response_model=CameraRead, summary="Enable a camera")
def enable_camera(
    camera_id: uuid.UUID,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    camera = camera_service.update_camera(session, scope, camera_id, is_enabled=True)
    record_audit(
        session,
        action=AuditAction.UPDATE,
        organization_id=camera.organization_id,
        user=user,
        resource_type="camera",
        resource_id=str(camera.id),
        description=f"Camera '{camera.name}' enabled",
        ip_address=get_client_ip(request),
    )
    session.commit()
    session.refresh(camera)
    return build_camera_read(camera)


@router.post("/{camera_id}/disable", response_model=CameraRead, summary="Disable a camera")
def disable_camera(
    camera_id: uuid.UUID,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    camera = camera_service.update_camera(session, scope, camera_id, is_enabled=False)
    record_audit(
        session,
        action=AuditAction.UPDATE,
        organization_id=camera.organization_id,
        user=user,
        resource_type="camera",
        resource_id=str(camera.id),
        description=f"Camera '{camera.name}' disabled",
        ip_address=get_client_ip(request),
    )
    session.commit()
    session.refresh(camera)
    return build_camera_read(camera)


@router.put(
    "/{camera_id}/streams",
    response_model=list[CameraStreamRead],
    summary="Set stream profiles",
)
def set_streams(
    camera_id: uuid.UUID,
    payload: SetStreamsRequest,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    """Replace a camera's stream profiles.

    If no stream is marked as the inference source, the lowest-resolution one
    is chosen: at the model's 640 px input the extra pixels of a main stream
    are discarded anyway and cost several times more CPU to decode.
    """
    rows = camera_service.set_streams(
        session, scope, camera_id, [entry.model_dump() for entry in payload.streams]
    )
    session.commit()
    return [CameraStreamRead.model_validate(row) for row in rows]


# ---------------------------------------------------------------------------
# Pipeline control
# ---------------------------------------------------------------------------


@router.post("/{camera_id}/pipeline/start", summary="Start the vision pipeline")
def start_pipeline(
    camera_id: uuid.UUID,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
) -> dict:
    """Begin detection, tracking and rule evaluation for this camera.

    Process-local: the pipeline runs in whichever worker handled this request.
    Deciding which worker owns which camera across a cluster is a deployment
    concern, and pretending to solve it here would let two workers fight over
    one stream.
    """
    result = pipeline_service.start_camera_pipeline(session, scope, camera_id)
    record_audit(
        session,
        action=AuditAction.CONFIG_CHANGED,
        organization_id=camera_service.get_camera(session, scope, camera_id).organization_id,
        user=user,
        resource_type="camera",
        resource_id=str(camera_id),
        description="Vision pipeline started",
        ip_address=get_client_ip(request),
    )
    session.commit()
    return result


@router.post("/{camera_id}/pipeline/stop", summary="Stop the vision pipeline")
def stop_pipeline(
    camera_id: uuid.UUID,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
) -> dict:
    result = pipeline_service.stop_camera_pipeline(session, scope, camera_id)
    record_audit(
        session,
        action=AuditAction.CONFIG_CHANGED,
        organization_id=camera_service.get_camera(session, scope, camera_id).organization_id,
        user=user,
        resource_type="camera",
        resource_id=str(camera_id),
        description="Vision pipeline stopped",
        ip_address=get_client_ip(request),
    )
    session.commit()
    return result


@router.get("/{camera_id}/pipeline", summary="Pipeline status for a camera")
def pipeline_status(
    camera_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser
) -> dict:
    """Live pipeline metrics, or ``running: false`` if none is active here."""
    camera = camera_service.get_camera(session, scope, camera_id)
    running = pipeline_service.manager.get(camera.id)
    if running is None:
        return {"camera_id": str(camera_id), "running": False}
    return {
        "camera_id": str(camera_id),
        "running": running.pipeline.is_running,
        "events_ingested": running.events_ingested,
        "events_deduplicated": running.events_deduplicated,
        "ingest_failures": running.ingest_failures,
        "started_at": running.started_at.isoformat(),
        **running.pipeline.metrics(),
    }
