"""Custom detection model management (milestone 15).

The primary way a model gets here is the training script calling
``models.register_model`` directly with a database session. ``POST /models``
is the path for a model trained on another machine: upload the weights plus
the metrics from validation.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, Query, Request, UploadFile, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, Scope, get_client_ip
from app.core.config import get_settings
from app.core.errors import ValidationError
from app.models import Camera
from app.models.enums import AuditAction, ModelStatus
from app.schemas.common import MessageResponse
from app.schemas.model import (
    ModelRegisterMeta,
    ModelVersionRead,
    ModelVersionSummary,
    build_model_read,
    build_model_summary,
)
from app.services import models as model_service
from app.services.audit import record_audit

router = APIRouter(prefix="/models", tags=["models"])

#: Weights larger than this are almost certainly a mistake (a full YOLO-x is
#: ~130 MB; a fine-tuned nano/small is single-digit MB).
_MAX_WEIGHTS_BYTES = 300 * 1024 * 1024


def _cameras_for(session, model_id: uuid.UUID) -> list[uuid.UUID]:
    return list(session.scalars(select(Camera.id).where(Camera.model_version_id == model_id)).all())


@router.get("", response_model=list[ModelVersionSummary], summary="List models")
def list_models(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    status_filter: ModelStatus | None = Query(None, alias="status"),
    name: str | None = Query(None, max_length=120),
):
    min_map50 = get_settings().MODEL_MIN_MAP50
    rows = model_service.list_models(session, scope, status=status_filter, name=name)
    return [build_model_summary(row, min_map50=min_map50) for row in rows]


@router.get("/{model_id}", response_model=ModelVersionRead, summary="Get a model")
def get_model(model_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    model = model_service.get_model(session, scope, model_id)
    return build_model_read(
        model,
        min_map50=get_settings().MODEL_MIN_MAP50,
        camera_ids=_cameras_for(session, model.id),
    )


@router.post(
    "",
    response_model=ModelVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register (upload) a trained model",
)
def register_model(
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
    request: Request,
    weights: UploadFile = File(..., description="the .pt weights file"),
    metadata: str = Form(..., description="a JSON object matching ModelRegisterMeta"),
):
    try:
        meta = ModelRegisterMeta.model_validate_json(metadata)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid model metadata.", details={"errors": exc.errors()}) from exc

    blob = weights.file.read()
    if not blob:
        raise ValidationError("The weights file is empty.")
    if len(blob) > _MAX_WEIGHTS_BYTES:
        raise ValidationError("The weights file is larger than 300 MB.")

    model = model_service.register_model(
        session,
        scope,
        name=meta.name,
        version=meta.version,
        weights=blob,
        class_names=meta.class_names,
        task=meta.task,
        base_model=meta.base_model,
        image_size=meta.image_size,
        checksum=meta.checksum,
        metrics=meta.metrics(),
        dataset_name=meta.dataset_name,
        dataset_size=meta.dataset_size,
        notes=meta.notes,
    )
    record_audit(
        session,
        action=AuditAction.CONFIG_CHANGED,
        organization_id=model.organization_id,
        user=user,
        resource_type="model_version",
        resource_id=str(model.id),
        description=f"Registered model {model.name}:{model.version}",
        ip_address=get_client_ip(request),
    )
    session.commit()
    return build_model_read(model, min_map50=get_settings().MODEL_MIN_MAP50, camera_ids=[])


@router.post("/{model_id}/activate", response_model=ModelVersionRead, summary="Activate a model")
def activate_model(
    model_id: uuid.UUID, session: DbSession, scope: Scope, user: CurrentUser, request: Request
):
    model = model_service.activate_model(session, scope, model_id)
    record_audit(
        session,
        action=AuditAction.CONFIG_CHANGED,
        organization_id=model.organization_id,
        user=user,
        resource_type="model_version",
        resource_id=str(model.id),
        description=f"Activated model {model.name}:{model.version}",
        ip_address=get_client_ip(request),
    )
    session.commit()
    return build_model_read(
        model,
        min_map50=get_settings().MODEL_MIN_MAP50,
        camera_ids=_cameras_for(session, model.id),
    )


@router.post("/{model_id}/archive", response_model=ModelVersionRead, summary="Archive a model")
def archive_model(model_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    model = model_service.archive_model(session, scope, model_id)
    session.commit()
    return build_model_read(
        model,
        min_map50=get_settings().MODEL_MIN_MAP50,
        camera_ids=_cameras_for(session, model.id),
    )


@router.delete("/{model_id}", response_model=MessageResponse, summary="Delete a draft model")
def delete_model(model_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    model_service.delete_model(session, scope, model_id)
    session.commit()
    return MessageResponse(message="Model deleted.")


@router.put(
    "/{model_id}/cameras/{camera_id}",
    response_model=MessageResponse,
    summary="Assign a model to a camera",
)
def assign_camera(
    model_id: uuid.UUID,
    camera_id: uuid.UUID,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
    request: Request,
):
    camera = model_service.assign_model_to_camera(
        session, scope, camera_id=camera_id, model_id=model_id
    )
    record_audit(
        session,
        action=AuditAction.CONFIG_CHANGED,
        organization_id=camera.organization_id,
        user=user,
        resource_type="camera",
        resource_id=str(camera.id),
        description=f"Assigned model {model_id} to camera '{camera.name}'",
        ip_address=get_client_ip(request),
    )
    session.commit()
    return MessageResponse(message="Model assigned.")


@router.delete(
    "/{model_id}/cameras/{camera_id}",
    response_model=MessageResponse,
    summary="Unassign a model from a camera",
)
def unassign_camera(
    model_id: uuid.UUID,
    camera_id: uuid.UUID,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    model_service.assign_model_to_camera(session, scope, camera_id=camera_id, model_id=None)
    session.commit()
    return MessageResponse(message="Model unassigned.")
