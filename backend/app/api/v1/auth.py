"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, get_client_ip, get_user_agent
from app.core.config import get_settings
from app.models import Organization, RefreshToken
from app.schemas.auth import (
    ChangePasswordRequest,
    CurrentUserResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    SessionInfo,
    TokenResponse,
)
from app.schemas.common import MessageResponse
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["authentication"])


def _token_response(access: str, refresh: str, expires_at) -> TokenResponse:
    settings = get_settings()
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        expires_at=expires_at,
    )


def _current_user_response(session, user) -> CurrentUserResponse:
    scope = auth_service.build_scope(user)
    organization_name = None
    if user.organization_id:
        organization_name = session.scalar(
            select(Organization.name).where(Organization.id == user.organization_id)
        )
    return CurrentUserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        organization_id=user.organization_id,
        is_active=user.is_active,
        phone=user.phone,
        timezone=user.timezone,
        locale=user.locale,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        permissions=sorted(p.value for p in scope.permissions),
        organization_name=organization_name,
        accessible_store_ids=sorted(scope.store_ids, key=str),
        is_store_restricted=scope.is_store_restricted,
    )


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new organization",
)
def register(payload: RegisterRequest, request: Request, session: DbSession):
    """Create a new organization and its first administrator.

    Self-service signup always creates a new tenant. Joining an existing
    organization happens through an invitation, not this endpoint.
    """
    user, organization, access, refresh, expires = auth_service.register_organization(
        session,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        organization_name=payload.organization_name,
        phone=payload.phone,
        country=payload.country,
        timezone=payload.timezone,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    session.commit()
    return RegisterResponse(
        tokens=_token_response(access, refresh, expires),
        user=_current_user_response(session, user),
        organization_id=organization.id,
        organization_slug=organization.slug,
    )


@router.post("/login", response_model=LoginResponse, summary="Sign in")
def login(payload: LoginRequest, request: Request, session: DbSession):
    """Exchange credentials for an access/refresh token pair.

    Unknown email, wrong password and deactivated account all return the same
    error so the endpoint cannot be used to enumerate accounts.
    """
    user, access, refresh, expires = auth_service.authenticate(
        session,
        email=payload.email,
        password=payload.password,
        organization_slug=payload.organization_slug,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    session.commit()
    return LoginResponse(
        tokens=_token_response(access, refresh, expires),
        user=_current_user_response(session, user),
    )


@router.post("/refresh", response_model=TokenResponse, summary="Rotate tokens")
def refresh(payload: RefreshRequest, request: Request, session: DbSession):
    """Exchange a refresh token for a new pair.

    The old token is revoked. Presenting an already-revoked token revokes the
    entire session family -- that pattern is what a stolen token looks like.
    """
    _user, access, new_refresh, expires = auth_service.refresh_session(
        session,
        refresh_token=payload.refresh_token,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    session.commit()
    return _token_response(access, new_refresh, expires)


@router.post("/logout", response_model=MessageResponse, summary="Sign out")
def logout(
    payload: LogoutRequest,
    request: Request,
    session: DbSession,
    user: CurrentUser,
):
    revoked = auth_service.logout(
        session,
        user=user,
        refresh_token=payload.refresh_token,
        all_sessions=payload.all_sessions,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    session.commit()
    return MessageResponse(message=f"Signed out. {revoked} session(s) ended.")


@router.get("/me", response_model=CurrentUserResponse, summary="Current user")
def me(session: DbSession, user: CurrentUser):
    """The signed-in user and their effective permissions.

    The permission list lets the dashboard hide actions the user cannot take.
    It is a usability affordance -- every endpoint still enforces server-side.
    """
    return _current_user_response(session, user)


@router.post("/change-password", response_model=MessageResponse, summary="Change password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    session: DbSession,
    user: CurrentUser,
):
    """Change the caller's password. Every session is ended, including this one."""
    auth_service.change_password(
        session,
        user=user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    session.commit()
    return MessageResponse(message="Password changed. Please sign in again on all your devices.")


@router.get("/sessions", response_model=list[SessionInfo], summary="Active sessions")
def list_sessions(session: DbSession, user: CurrentUser):
    """List the caller's live sessions so they can spot an unfamiliar device."""
    rows = session.scalars(
        select(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .order_by(RefreshToken.created_at.desc())
    ).all()
    return [
        SessionInfo(
            id=row.id,
            created_at=row.created_at,
            expires_at=row.expires_at,
            ip_address=row.ip_address,
            user_agent=row.user_agent,
        )
        for row in rows
    ]
