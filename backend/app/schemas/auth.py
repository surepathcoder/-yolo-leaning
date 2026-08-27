"""Authentication request and response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import UserRole
from app.schemas.common import APISchema


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    #: Optional: disambiguates a person who operates two organizations with
    #: the same email address.
    organization_slug: str | None = Field(default=None, max_length=80)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class RegisterRequest(BaseModel):
    """Self-service registration: creates an organization and its first admin."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)
    organization_name: str = Field(min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    country: str = Field(default="TZ", min_length=2, max_length=2)
    timezone: str = Field(default="Africa/Dar_es_Salaam", max_length=64)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str | None = None
    #: Sign out everywhere, not just this device.
    all_sessions: bool = False


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class TokenResponse(BaseModel):
    """Issued on login and refresh.

    ``expires_in`` is seconds until the *access* token expires, so a client can
    schedule a refresh without decoding the JWT.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    expires_at: datetime


class UserSummary(APISchema):
    """A user as returned by the API.

    Note what is absent: ``hashed_password``, ``failed_login_count`` and
    ``locked_until`` are never serialised.
    """

    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    organization_id: uuid.UUID | None
    is_active: bool
    phone: str | None = None
    timezone: str | None = None
    locale: str | None = None
    last_login_at: datetime | None = None
    created_at: datetime


class CurrentUserResponse(UserSummary):
    """``/auth/me`` -- adds the caller's effective permissions.

    The frontend uses this to hide actions the user cannot perform. It is a
    usability affordance, never the enforcement point: every endpoint checks
    permissions server-side regardless of what the client believes.
    """

    permissions: list[str]
    organization_name: str | None = None
    accessible_store_ids: list[uuid.UUID] = Field(default_factory=list)
    is_store_restricted: bool = False


class LoginResponse(BaseModel):
    tokens: TokenResponse
    user: CurrentUserResponse


class RegisterResponse(BaseModel):
    tokens: TokenResponse
    user: CurrentUserResponse
    organization_id: uuid.UUID
    organization_slug: str


class SessionInfo(APISchema):
    """An active refresh-token session, shown in account settings."""

    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    ip_address: str | None = None
    user_agent: str | None = None
    is_current: bool = False
