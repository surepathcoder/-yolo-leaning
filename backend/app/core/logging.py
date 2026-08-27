"""Structured logging setup.

Every log line carries a ``service`` field plus whatever contextual fields the
call site binds (``camera_id``, ``store_id``, ``event_id``, ``event_type``,
``severity``, ...).  In development the console renderer is used; in production
JSON is emitted so logs can be shipped to a collector.

Credentials must never reach the logs -- :func:`_redact_secrets` scrubs a
known set of sensitive keys from every event dict as a last line of defence.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from app.core.config import get_settings

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "jwt_secret",
        "rtsp_password",
        "camera_password",
        "api_key",
        "credentials_encryption_key",
        "hashed_password",
        "password_hash",
    }
)

REDACTED = "***redacted***"

_configured = False


def _redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace values of sensitive keys with a placeholder."""
    for key in list(event_dict.keys()):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def _add_service(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict.setdefault("service", "visionguard")
    return event_dict


def configure_logging(force: bool = False) -> None:
    """Configure structlog + stdlib logging once per process."""
    global _configured
    if _configured and not force:
        return

    settings = get_settings()
    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )
    # Uvicorn duplicates access logs through its own handlers; keep them but at
    # the configured level.
    for noisy in ("uvicorn.error", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_service,
        _redact_secrets,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.LOG_FORMAT == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger, configuring logging on first use."""
    configure_logging()
    return structlog.get_logger(name)
