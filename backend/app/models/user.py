"""Users, roles, permissions and refresh tokens."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    GUID,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
)
from app.models.enums import UserRole
from app.models.mixins import enum_column

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.store import Store


# Roles hold many permissions; permissions belong to many roles.
role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", GUID(), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_id",
        GUID(),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

# A user may be restricted to a subset of the organization's stores.
# No rows for a user means "every store in the organization", subject to role.
user_stores = Table(
    "user_stores",
    Base.metadata,
    Column("user_id", GUID(), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("store_id", GUID(), ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True),
)


class Permission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single capability, e.g. ``camera:write``.

    The authoritative permission catalogue lives in code
    (:mod:`app.core.permissions`); this table mirrors it so an admin UI can
    list and describe permissions, and so custom roles can reference them.
    """

    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="general")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    roles: Mapped[list[Role]] = relationship(
        secondary=role_permissions, back_populates="permissions"
    )

    def __repr__(self) -> str:
        return f"<Permission {self.code}>"


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named permission set.

    The six system roles are seeded from :mod:`app.core.permissions` and have
    ``is_system = True``.  ``organization_id`` is null for system roles and set
    for future per-tenant custom roles.
    """

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_role_org_name"),)

    name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )

    permissions: Mapped[list[Permission]] = relationship(
        secondary=role_permissions, back_populates="roles", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Role {self.name}>"


class User(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """An operator of the platform.

    ``role`` is the authoritative system role and is always present, so
    permission checks need no join on the hot path.  ``custom_role_id`` is an
    optional per-organization role that *adds* permissions on top.
    """

    __tablename__ = "users"
    __table_args__ = (
        # Email is unique per organization, not globally: the same person may
        # legitimately operate two unrelated businesses.
        UniqueConstraint("organization_id", "email", name="uq_user_org_email"),
        Index("ix_users_email_lower", "email"),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,  # SUPER_ADMIN belongs to no organization
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole), nullable=False, default=UserRole.VIEWER, index=True
    )
    custom_role_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("roles.id", ondelete="SET NULL"), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: Consecutive failed logins; reset on success. Used for lockout.
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(8), nullable=True)

    organization: Mapped[Organization | None] = relationship(back_populates="users")
    custom_role: Mapped[Role | None] = relationship(lazy="selectin")
    stores: Mapped[list[Store]] = relationship(secondary=user_stores, lazy="selectin")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User {self.email} {self.role}>"


class RefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A persisted, revocable refresh token.

    Only a hash of the token is stored, so a database leak does not hand an
    attacker usable sessions.  Rows are kept after revocation for audit; the
    retention worker prunes expired ones.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: SHA-256 of the raw token. The raw value is never persisted.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    #: Rotation family: a reused token invalidates the whole family.
    family_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)

    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    def __repr__(self) -> str:
        return f"<RefreshToken user={self.user_id} revoked={self.revoked_at is not None}>"
