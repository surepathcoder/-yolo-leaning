"""System diagnostics: vision performance, realtime health, plan usage.

These endpoints answer the questions an operator actually asks when something
feels wrong: is the model running on the GPU, how fast is inference, are
frames being dropped, is anyone connected, and is a camera quietly offline.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession, Scope
from app.core.permissions import Permission
from app.services import plan_limits
from app.services.pipeline_manager import manager as pipeline_manager
from app.websocket.broadcast import subscriber
from app.websocket.manager import manager as connection_manager

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/vision/metrics", summary="Vision engine performance")
def vision_metrics(session: DbSession, scope: Scope, _user: CurrentUser) -> dict:
    """Per-camera inference and pipeline performance.

    Scoped to the caller's organization: a customer sees the health of their
    own cameras, not the load of the box they share.

    The numbers that matter, and why:

    * ``mean_inference_ms`` / ``p95_inference_ms`` -- p95 is the one that
      predicts dropped frames; a good mean hides a periodic stall.
    * ``frames_skipped`` -- expected. The reader deliberately drops surplus
      frames to hold the target rate on a live camera.
    * ``frames_dropped`` -- not expected. The processing queue overflowed,
      which means the box cannot keep up with the configured FPS.
    * ``theoretical_max_fps`` -- what inference alone could sustain. If it is
      below the camera's target FPS, the camera is over-configured.
    """
    scope.require(Permission.SYSTEM_METRICS_READ)
    metrics = pipeline_manager.metrics(organization_id=scope.organization_id)
    metrics["realtime"] = {
        **connection_manager.stats(),
        "subscriber": subscriber.stats(),
    }
    return metrics


@router.get("/vision/device", summary="Detection device and model")
def vision_device(scope: Scope, _user: CurrentUser) -> dict:
    """What hardware inference is running on.

    Worth surfacing plainly: a deployment that silently fell back to CPU when
    a GPU was expected runs perhaps twenty times slower, and the only symptom
    is dropped frames.
    """
    scope.require(Permission.SYSTEM_METRICS_READ)
    from app.core.config import get_settings
    from vision.config import VisionConfig

    settings = get_settings()
    config = VisionConfig(
        model_path=settings.YOLO_MODEL_PATH,
        device=settings.YOLO_DEVICE,
        image_size=settings.YOLO_IMAGE_SIZE,
    )
    detector = pipeline_manager._detector

    payload: dict = {
        "configured_device": settings.YOLO_DEVICE,
        "resolved_device": config.resolved_device(),
        "model_path": settings.YOLO_MODEL_PATH,
        "model_present": config.model_exists(),
        "image_size": settings.YOLO_IMAGE_SIZE,
        "tracker": settings.TRACKER_TYPE,
        "loaded": detector is not None,
    }
    if detector is not None:
        payload["classes"] = len(detector.class_names)
        payload["inference"] = detector.stats.to_dict()
    return payload


@router.get("/plan", summary="Subscription plan and usage")
def plan(session: DbSession, scope: Scope, _user: CurrentUser) -> dict:
    """The current plan, its limits, and how much of each is used."""
    scope.require(Permission.SETTINGS_READ)
    if scope.organization_id is None:
        return {"tier": None, "limits": {}}
    return plan_limits.plan_summary(session, scope.organization_id)


@router.get("/realtime", summary="Realtime delivery health")
def realtime(scope: Scope, _user: CurrentUser) -> dict:
    """WebSocket connection counts and the Redis relay's state.

    ``subscriber.connected == false`` means this process is not receiving
    events published by other processes: dashboards served here will look
    stale even though the data is fine.
    """
    scope.require(Permission.SYSTEM_METRICS_READ)
    return {
        "connections": connection_manager.stats(),
        "subscriber": subscriber.stats(),
        "organization_connections": (
            connection_manager.organization_connection_count(scope.organization_id)
            if scope.organization_id
            else 0
        ),
    }
