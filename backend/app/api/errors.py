"""Exception handlers wiring :mod:`app.core.errors` onto FastAPI."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

log = get_logger(__name__)


def _envelope(code: str, message: str, details: dict | None = None) -> dict:
    error: dict = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"error": error}


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    log.warning(
        "app_error",
        code=exc.code,
        status_code=exc.status_code,
        path=request.url.path,
        method=request.method,
        internal_message=exc.internal_message,
    )
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code_map = {
        401: ErrorCode.NOT_AUTHENTICATED,
        403: ErrorCode.PERMISSION_DENIED,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.CONFLICT,
        429: ErrorCode.RATE_LIMITED,
    }
    code = code_map.get(exc.status_code, "HTTP_ERROR")
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, detail),
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Surface field-level problems (useful and non-sensitive) but never the
    # raw input values, which may contain passwords.
    fields = [
        {
            "field": ".".join(str(part) for part in err.get("loc", ())[1:]),
            "message": err.get("msg", "invalid"),
            "type": err.get("type", "value_error"),
        }
        for err in exc.errors()
    ]
    log.info("request_validation_failed", path=request.url.path, field_count=len(fields))
    return JSONResponse(
        status_code=422,
        content=_envelope(
            ErrorCode.VALIDATION_ERROR, "The request payload is invalid.", {"fields": fields}
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log the real error, return an opaque reference to the user."""
    incident_id = str(uuid.uuid4())
    log.error(
        "unhandled_exception",
        incident_id=incident_id,
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
        error=str(exc),
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope(
            ErrorCode.INTERNAL_ERROR,
            "An unexpected error occurred. Please contact support with the incident id.",
            {"incident_id": incident_id},
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
