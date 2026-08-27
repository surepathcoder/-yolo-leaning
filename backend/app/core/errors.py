"""Application error types and the consistent API error envelope.

Every error the API returns has the shape::

    {"error": {"code": "CAMERA_CONNECTION_FAILED",
               "message": "Unable to connect to the camera.",
               "details": {...}}}

Internal exception text and stack traces are logged, never returned.
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    """Stable, machine-readable error codes shared with the frontend."""

    # generic
    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"

    # auth
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    EMAIL_ALREADY_REGISTERED = "EMAIL_ALREADY_REGISTERED"

    # tenancy
    ORGANIZATION_SCOPE_VIOLATION = "ORGANIZATION_SCOPE_VIOLATION"
    PLAN_LIMIT_EXCEEDED = "PLAN_LIMIT_EXCEEDED"

    # cameras / vision
    CAMERA_CONNECTION_FAILED = "CAMERA_CONNECTION_FAILED"
    CAMERA_ALREADY_RUNNING = "CAMERA_ALREADY_RUNNING"
    STREAM_UNAVAILABLE = "STREAM_UNAVAILABLE"
    MODEL_LOAD_FAILED = "MODEL_LOAD_FAILED"
    INFERENCE_FAILED = "INFERENCE_FAILED"

    # zones / geometry
    INVALID_POLYGON = "INVALID_POLYGON"
    INVALID_LINE = "INVALID_LINE"

    # inventory / pos
    POS_PROVIDER_ERROR = "POS_PROVIDER_ERROR"
    UNKNOWN_POS_PROVIDER = "UNKNOWN_POS_PROVIDER"

    # infrastructure
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    REDIS_UNAVAILABLE = "REDIS_UNAVAILABLE"
    STORAGE_ERROR = "STORAGE_ERROR"


class AppError(Exception):
    """Base class for all errors that map onto an HTTP response.

    ``message`` is user-facing and must never contain internal detail.
    ``internal_message`` is logged only.
    """

    status_code: int = 500
    code: str = ErrorCode.INTERNAL_ERROR
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        internal_message: str | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        self.internal_message = internal_message
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


class NotFoundError(AppError):
    status_code = 404
    code = ErrorCode.NOT_FOUND
    message = "The requested resource was not found."


class ConflictError(AppError):
    status_code = 409
    code = ErrorCode.CONFLICT
    message = "The request conflicts with the current state of the resource."


class ValidationError(AppError):
    status_code = 422
    code = ErrorCode.VALIDATION_ERROR
    message = "The request payload is invalid."


class AuthenticationError(AppError):
    status_code = 401
    code = ErrorCode.NOT_AUTHENTICATED
    message = "Authentication is required."


class InvalidCredentialsError(AuthenticationError):
    code = ErrorCode.INVALID_CREDENTIALS
    message = "Incorrect email or password."


class PermissionDeniedError(AppError):
    status_code = 403
    code = ErrorCode.PERMISSION_DENIED
    message = "You do not have permission to perform this action."


class RateLimitedError(AppError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED
    message = "Too many requests. Please slow down."


class CameraConnectionError(AppError):
    status_code = 502
    code = ErrorCode.CAMERA_CONNECTION_FAILED
    message = "Unable to connect to the camera."


class PlanLimitExceededError(AppError):
    status_code = 402
    code = ErrorCode.PLAN_LIMIT_EXCEEDED
    message = "This action exceeds your subscription plan limits."


class ServiceUnavailableError(AppError):
    status_code = 503
    message = "A required service is unavailable."
