"""Shared FastAPI dependencies.

These are the single enforcement point for authentication and authorisation on
HTTP routes.  A route that needs a caller declares it here rather than reading
headers itself, so no endpoint can accidentally skip a check.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.security import decode_token
from app.core.tenancy import TenantScope
from app.db.session import get_db
from app.models import User
from app.services.auth import AccountDisabledError, build_scope

log = get_logger(__name__)

# auto_error=False so a missing header produces our own error envelope rather
# than FastAPI's default {"detail": ...} shape.
bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

DbSession = Annotated[Session, Depends(get_db)]


def get_client_ip(request: Request) -> str | None:
    """Best-effort client IP.

    ``X-Forwarded-For`` is honoured because the production deployment sits
    behind nginx.  It is used only for audit context and rate limiting, never
    for authorisation -- the header is client-controlled and trivially spoofed.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()[:45]
    return request.client.host if request.client else None


def get_user_agent(request: Request) -> str | None:
    agent = request.headers.get("User-Agent")
    return agent[:400] if agent else None


def get_current_user(
    request: Request,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """Resolve the authenticated user from the bearer token.

    The user row is loaded on every request rather than trusting the token's
    claims alone: a deactivated or deleted account must stop working
    immediately, not when its access token happens to expire.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Sign in to continue.")

    payload = decode_token(credentials.credentials, expected_type="access")

    user = session.get(User, payload.subject)
    if user is None or user.deleted_at is not None:
        log.warning("auth_rejected", reason="user_missing", user_id=str(payload.subject))
        raise AuthenticationError("Sign in to continue.")
    if not user.is_active:
        raise AccountDisabledError()
    if user.organization is not None and not user.organization.is_active:
        raise AccountDisabledError("This organization is not active. Contact support.")

    # A role change since the token was minted must take effect immediately;
    # the database row wins over the cached claim.
    if payload.role and payload.role != user.role.value:
        log.info(
            "token_role_claim_stale",
            user_id=str(user.id),
            token_role=payload.role,
            current_role=user.role.value,
        )

    request.state.user_id = str(user.id)
    request.state.organization_id = str(user.organization_id) if user.organization_id else None
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_scope(user: CurrentUser) -> TenantScope:
    """The caller's tenant scope. Pass this into every service call."""
    return build_scope(user)


Scope = Annotated[TenantScope, Depends(get_scope)]


def require_permission(permission: Permission):
    """Build a dependency that enforces one permission.

    Usage::

        @router.post(
            "/cameras",
            dependencies=[Depends(require_permission(Permission.CAMERA_WRITE))],
        )
    """

    def dependency(scope: Scope) -> TenantScope:
        scope.require(permission)
        return scope

    dependency.__name__ = f"require_{permission.value.replace(':', '_')}"
    return dependency


def require_any_permission(*permissions: Permission):
    """Enforce that the caller holds at least one of ``permissions``."""

    def dependency(scope: Scope) -> TenantScope:
        if not any(scope.has(p) for p in permissions):
            scope.require(permissions[0])  # raises with a useful message
        return scope

    dependency.__name__ = "require_any_permission"
    return dependency


def get_optional_user(
    request: Request,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User | None:
    """Resolve the user if a valid token is present; otherwise ``None``."""
    if credentials is None or not credentials.credentials:
        return None
    try:
        return get_current_user(request, session, credentials)
    except Exception:
        return None


OptionalUser = Annotated[User | None, Depends(get_optional_user)]
