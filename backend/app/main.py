"""VisionGuard FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.v1 import health as health_routes
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.websocket import routes as websocket_routes
from app.websocket.broadcast import subscriber

log = get_logger(__name__)


async def _retention_loop(interval_hours: int) -> None:
    """Run the retention sweep every ``interval_hours``, forever.

    The sweep is synchronous and touches the database, so it runs in a worker
    thread. One failed pass logs and waits for the next rather than killing
    the loop.
    """
    from app.services.maintenance import run_retention_sweep

    interval_seconds = max(interval_hours, 1) * 3600
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await anyio.to_thread.run_sync(run_retention_sweep)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("retention_sweep_failed", error=str(exc)[:200])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    settings.ensure_storage_dirs()
    log.info(
        "application_starting",
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
        edge_mode=settings.EDGE_MODE,
    )

    # Relays realtime messages published by other processes onto this one's
    # WebSocket connections. Failing to start must not stop the API: the
    # dashboard falls back to polling.
    try:
        await subscriber.start()
    except Exception as exc:
        log.warning("realtime_subscriber_start_failed", error=str(exc)[:200])

    retention_task: asyncio.Task | None = None
    if settings.RETENTION_SWEEP_ENABLED:
        retention_task = asyncio.create_task(
            _retention_loop(settings.RETENTION_SWEEP_INTERVAL_HOURS)
        )

    yield

    if retention_task is not None:
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task

    # Stop cameras before closing sockets: a pipeline mid-frame would
    # otherwise try to publish to a manager that is already shutting down.
    from app.services.pipeline_manager import manager as pipelines

    stopped = pipelines.stop_all()
    if stopped:
        log.info("pipelines_stopped_on_shutdown", count=stopped)

    await subscriber.stop()
    from app.websocket.manager import manager as connection_manager

    await connection_manager.close_all()
    log.info("application_stopped")


def create_app() -> FastAPI:
    """Application factory -- also used by the test suite."""
    settings = get_settings()

    app = FastAPI(
        title=f"{settings.APP_NAME} API",
        description=(
            "AI-powered CCTV, retail security and inventory intelligence platform.\n\n"
            "The vision engine reports **observations**; the event engine applies "
            "configured rules to interpret them. Observations and inferred "
            "conclusions are always kept distinct."
        ),
        version=settings.APP_VERSION,
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    register_exception_handlers(app)

    # System endpoints live at the root so probes do not depend on API version.
    app.include_router(health_routes.router)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    app.include_router(websocket_routes.router)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "docs": "/docs",
            "health": "/health",
        }

    return app


app = create_app()
