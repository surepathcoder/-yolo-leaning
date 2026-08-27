"""Redis connection helper.

Redis is used for pub/sub fan-out of realtime events between the vision
workers and the API's WebSocket layer, plus rate limiting and short-lived
caches.  The application must degrade gracefully when Redis is unavailable:
callers check :func:`check_redis` rather than assuming a connection.
"""

from __future__ import annotations

import contextlib

import redis

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Return the process-wide Redis client (lazily connected)."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
            health_check_interval=30,
        )
    return _client


def reset_redis() -> None:
    global _client
    if _client is not None:
        with contextlib.suppress(Exception):
            _client.close()
    _client = None


def check_redis() -> tuple[bool, str | None]:
    """Ping Redis. Returns ``(healthy, error_message)``."""
    try:
        get_redis().ping()
        return True, None
    except Exception as exc:
        log.error("redis_health_check_failed", error=str(exc))
        return False, str(exc)
