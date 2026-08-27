"""Organization -- the tenant root -- and its subscription."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import (
    GUID,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
)
from app.models.enums import PlanTier, SubscriptionStatus
from app.models.mixins import enum_column

if TYPE_CHECKING:
    from app.models.store import Store
    from app.models.user import User


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A customer business. The root of every tenant scope.

    Nothing in the system is visible across organizations except to a
    ``SUPER_ADMIN``, and such access is always written to the audit log.
    """

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: URL-safe identifier used in invitations and support tooling.
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="TZ")

    # Localisation defaults inherited by stores that do not override them.
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Africa/Dar_es_Salaam"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="TZS")
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="en")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Free-form per-tenant configuration (branding, feature toggles, event
    #: rule overrides). Structured settings belong in dedicated columns.
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    subscription: Mapped[Subscription | None] = relationship(
        back_populates="organization", uselist=False, cascade="all, delete-orphan"
    )
    stores: Mapped[list[Store]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    users: Mapped[list[User]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Organization {self.slug}>"


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Billing-ready plan record.

    No payment provider is integrated yet.  This exists so that plan limits are
    enforced through one service from the beginning, instead of being sprinkled
    through the codebase when billing is added later.
    """

    __tablename__ = "subscriptions"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    tier: Mapped[PlanTier] = mapped_column(
        enum_column(PlanTier), nullable=False, default=PlanTier.TRIAL
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        enum_column(SubscriptionStatus), nullable=False, default=SubscriptionStatus.ACTIVE
    )
    started_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    trial_ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: Per-organization overrides of the tier defaults, e.g. ``{"max_cameras": 40}``.
    #: Resolution order is: override -> tier default -> unlimited.
    #: Enforced only by app.services.plan_limits.
    limit_overrides: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    #: Retained for a future payment provider; unused today.
    external_customer_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    seats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    organization: Mapped[Organization] = relationship(back_populates="subscription")

    def __repr__(self) -> str:
        return f"<Subscription {self.tier} {self.status}>"
