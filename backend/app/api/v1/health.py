"""Health and readiness endpoints."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from app.core.config import get_settings
from app.db.redis_client import check_redis
from app.db.session import check_database
from app.schemas.health import HealthResponse, ReadinessResponse

router = APIRouter(tags=["system"])

_PROCESS_STARTED = time.monotonic()


@router.get("/health", response_model=HealthResponse, summary="Service health")
def health(response: Response) -> HealthResponse:
    """Report the health of the service and its backing components.

    Returns HTTP 200 with ``status="degraded"`` when a component is down, so
    that monitoring can distinguish "process is up but Redis is gone" from
    "process is dead".
    """
    settings = get_settings()
    db_ok, _ = check_database()
    redis_ok, _ = check_redis()

    overall = "ok" if (db_ok and redis_ok) else "degraded"
    if overall == "degraded":
        response.status_code = status.HTTP_200_OK

    return HealthResponse(
        status=overall,
        database="connected" if db_ok else "unavailable",
        redis="connected" if redis_ok else "unavailable",
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
        uptime_seconds=round(time.monotonic() - _PROCESS_STARTED, 3),
        timestamp=datetime.now(UTC),
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
def ready(response: Response) -> ReadinessResponse:
    """Kubernetes-style readiness probe: 503 when a hard dependency is down."""
    db_ok, _ = check_database()
    redis_ok, _ = check_redis()
    checks = {"database": db_ok, "redis": redis_ok}
    is_ready = all(checks.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(ready=is_ready, checks=checks)


@router.get("/live", summary="Liveness probe")
def live() -> dict[str, str]:
    """Process liveness -- deliberately touches no external dependency."""
    return {"status": "alive"}
