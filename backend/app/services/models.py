"""Custom detection models: registration, validation, activation, assignment.

A :class:`~app.models.system.ModelVersion` is a fine-tuned YOLO model bound to
one organization (or shared, when ``organization_id`` is null). The loader in
``pipeline_manager`` will refuse to serve a model whose organization does not
match the camera's, because a model trained on one retailer's products emits
class names that mean nothing for another.

Storage discipline matches ``services/evidence.py``: the weights live on disk
under ``MODEL_STORAGE_PATH`` by a **relative** path, and the row carries a
SHA-256 so a swapped or corrupted file is detectable.

The activation gate is the important rule here. A model only becomes ``ACTIVE``
if it has recorded validation metrics and its ``map50`` clears
``MODEL_MIN_MAP50``. A model that misses half the stock is worse than none --
it teaches the occupancy engine to under-count, silently.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.tenancy import TenantScope
from app.models import Camera, ModelVersion, ProductDetection
from app.models.enums import ModelStatus, ModelTask

log = get_logger(__name__)


def _model_store() -> Path:
    return Path(get_settings().MODEL_STORAGE_PATH)


def relative_weights_path(organization_id: uuid.UUID | None, name: str, version: str) -> str:
    """``<org|shared>/<name>/<version>/best.pt`` -- relative, never absolute."""
    scope = str(organization_id) if organization_id else "shared"
    safe = lambda part: "".join(c if c.isalnum() or c in "-_." else "_" for c in part)  # noqa: E731
    return f"{scope}/{safe(name)}/{safe(version)}/best.pt"


def weights_file_path(model: ModelVersion) -> Path | None:
    """The absolute path of the weights on disk, or ``None`` if missing/escaping."""
    root = _model_store().resolve()
    candidate = (root / model.weights_path).resolve()
    if root not in candidate.parents:
        log.error("model_weights_path_escape", model_id=str(model.id), path=model.weights_path)
        return None
    return candidate if candidate.is_file() else None


def _visible(scope: TenantScope):
    """A filter for models the caller may see: their org's, plus shared."""
    if scope.is_platform_operator:
        return None
    return or_(
        ModelVersion.organization_id == scope.organization_id,
        ModelVersion.organization_id.is_(None),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_model(
    session: Session,
    scope: TenantScope,
    *,
    name: str,
    version: str,
    weights: bytes,
    class_names: list[str],
    task: ModelTask = ModelTask.DETECTION,
    base_model: str | None = None,
    image_size: int = 640,
    checksum: str | None = None,
    metrics: dict | None = None,
    dataset_name: str | None = None,
    dataset_size: int | None = None,
    training_config: dict | None = None,
    trained_at: datetime | None = None,
    notes: str | None = None,
) -> ModelVersion:
    """Store a trained model's weights and record a ``ModelVersion`` row.

    Status is ``VALIDATED`` when validation metrics are present, ``DRAFT``
    otherwise. Nothing is ``ACTIVE`` until :func:`activate_model`.
    """
    scope.require(Permission.MODEL_WRITE)
    if not class_names:
        raise ValidationError("A model must declare at least one class name.")

    organization_id = scope.organization_id
    existing = session.scalar(
        select(ModelVersion).where(
            ModelVersion.organization_id == organization_id,
            ModelVersion.name == name,
            ModelVersion.version == version,
        )
    )
    if existing is not None:
        raise ConflictError(
            f"A model '{name}' version '{version}' already exists.",
            details={"model_id": str(existing.id)},
        )

    relative = relative_weights_path(organization_id, name, version)
    destination = _model_store() / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(weights)

    digest = hashlib.sha256(weights).hexdigest()
    if checksum is not None and checksum != digest:
        raise ValidationError("The provided checksum does not match the uploaded weights.")

    metrics = metrics or {}
    map50 = metrics.get("map50")
    model = ModelVersion(
        organization_id=organization_id,
        name=name,
        version=version,
        task=task,
        status=ModelStatus.VALIDATED if map50 is not None else ModelStatus.DRAFT,
        weights_path=relative,
        checksum=digest,
        base_model=base_model,
        class_names=list(class_names),
        image_size=image_size,
        metrics=metrics,
        map50=float(map50) if map50 is not None else None,
        map50_95=(float(metrics["map50_95"]) if metrics.get("map50_95") is not None else None),
        dataset_name=dataset_name,
        dataset_size=dataset_size,
        trained_at=trained_at,
        training_config=training_config or {},
        notes=notes,
    )
    session.add(model)
    session.flush()
    log.info(
        "model_registered",
        model_id=str(model.id),
        name=name,
        version=version,
        status=model.status.value,
        map50=model.map50,
    )
    return model


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_models(
    session: Session,
    scope: TenantScope,
    *,
    status: ModelStatus | None = None,
    name: str | None = None,
) -> list[ModelVersion]:
    scope.require(Permission.MODEL_READ)
    statement = select(ModelVersion)
    visible = _visible(scope)
    if visible is not None:
        statement = statement.where(visible)
    if status is not None:
        statement = statement.where(ModelVersion.status == status)
    if name is not None:
        statement = statement.where(ModelVersion.name == name)
    return list(
        session.scalars(statement.order_by(ModelVersion.name, ModelVersion.created_at.desc())).all()
    )


def get_model(session: Session, scope: TenantScope, model_id: uuid.UUID) -> ModelVersion:
    scope.require(Permission.MODEL_READ)
    statement = select(ModelVersion).where(ModelVersion.id == model_id)
    visible = _visible(scope)
    if visible is not None:
        statement = statement.where(visible)
    model = session.scalar(statement)
    if model is None:
        raise NotFoundError("Model not found.", details={"model_id": str(model_id)})
    return model


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def activate_model(session: Session, scope: TenantScope, model_id: uuid.UUID) -> ModelVersion:
    """Promote a model to ``ACTIVE``, demoting the previous active one.

    Refuses a model with no metrics, or whose ``map50`` is below
    ``MODEL_MIN_MAP50`` -- see the module docstring for why.
    """
    scope.require(Permission.MODEL_ACTIVATE)
    model = get_model(session, scope, model_id)
    if model.organization_id is None and not scope.is_platform_operator:
        raise ConflictError("Shared models are managed by the platform operator.")

    if model.status not in (ModelStatus.VALIDATED, ModelStatus.ARCHIVED):
        raise ConflictError(
            f"A model in status {model.status.value} cannot be activated.",
            details={"status": model.status.value},
        )
    threshold = get_settings().MODEL_MIN_MAP50
    if model.map50 is None:
        raise ConflictError("This model has no validation metrics and cannot be activated.")
    if model.map50 < threshold:
        raise ConflictError(
            f"mAP50 {model.map50:.3f} is below the {threshold:.2f} activation threshold.",
            details={"map50": model.map50, "threshold": threshold},
        )

    previous = session.scalars(
        select(ModelVersion).where(
            ModelVersion.organization_id == model.organization_id,
            ModelVersion.name == model.name,
            ModelVersion.status == ModelStatus.ACTIVE,
            ModelVersion.id != model.id,
        )
    ).all()
    for row in previous:
        row.status = ModelStatus.ARCHIVED

    model.status = ModelStatus.ACTIVE
    model.activated_at = datetime.now(UTC)
    session.flush()
    log.info(
        "model_activated",
        model_id=str(model.id),
        name=model.name,
        version=model.version,
        demoted=[str(r.id) for r in previous],
    )
    return model


def archive_model(session: Session, scope: TenantScope, model_id: uuid.UUID) -> ModelVersion:
    scope.require(Permission.MODEL_ACTIVATE)
    model = get_model(session, scope, model_id)
    if model.status == ModelStatus.ACTIVE and _cameras_using(session, model.id):
        raise ConflictError("Unassign this model from its cameras before archiving it.")
    model.status = ModelStatus.ARCHIVED
    session.flush()
    return model


def delete_model(session: Session, scope: TenantScope, model_id: uuid.UUID) -> None:
    scope.require(Permission.MODEL_WRITE)
    model = get_model(session, scope, model_id)
    if model.status not in (ModelStatus.DRAFT, ModelStatus.FAILED):
        raise ConflictError(
            "Only a draft or failed model can be deleted. Archive an established one."
        )
    used = session.scalar(
        select(ProductDetection.id).where(ProductDetection.model_version_id == model.id)
    )
    if used:
        raise ConflictError("This model has produced detections and cannot be deleted.")
    path = weights_file_path(model)
    if path is not None:
        path.unlink(missing_ok=True)
    session.delete(model)
    session.flush()


# ---------------------------------------------------------------------------
# Camera assignment
# ---------------------------------------------------------------------------


def _cameras_using(session: Session, model_id: uuid.UUID) -> list[Camera]:
    return list(session.scalars(select(Camera).where(Camera.model_version_id == model_id)).all())


def assign_model_to_camera(
    session: Session,
    scope: TenantScope,
    *,
    camera_id: uuid.UUID,
    model_id: uuid.UUID | None,
) -> Camera:
    """Point a camera at a custom model, or clear it (``model_id=None``)."""
    from app.services.cameras import get_camera

    scope.require(Permission.CAMERA_WRITE)
    camera = get_camera(session, scope, camera_id)
    scope.require_store(camera.store_id)

    if model_id is not None:
        model = get_model(session, scope, model_id)
        if model.status != ModelStatus.ACTIVE:
            raise ConflictError(
                f"Model {model.name}:{model.version} is {model.status.value}, not ACTIVE.",
            )
        if model.organization_id not in (None, camera.organization_id):
            raise NotFoundError("Model not found.", details={"model_id": str(model_id)})
        camera.model_version_id = model.id
    else:
        camera.model_version_id = None

    session.flush()
    _notify_pipeline(session, camera_id)
    log.info(
        "model_assigned_to_camera",
        model_id=str(model_id) if model_id else None,
        camera_id=str(camera_id),
    )
    return camera


def _notify_pipeline(session: Session, camera_id: uuid.UUID) -> None:
    """Re-apply config to a running pipeline so a model swap takes effect live.

    Same reasoning as ``inventory._notify_pipeline``: a restart would reset
    every track id mid-shift, and the stored config is the truth anyway.
    """
    try:
        from app.services.pipeline_manager import build_pipeline_config, manager

        running = manager.get(camera_id)
        if running is None:
            return
        camera = session.get(Camera, camera_id)
        if camera is not None:
            running.pipeline.apply_config(build_pipeline_config(session, camera))
            log.info("pipeline_reconfigured", camera_id=str(camera_id))
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("pipeline_reconfigure_failed", camera_id=str(camera_id), error=str(exc)[:200])
