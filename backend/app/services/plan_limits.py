"""Subscription plan limits.

No payment provider is integrated. This module exists so that limits are
enforced through **one** service from the beginning, rather than being
sprinkled through the codebase when billing is eventually added. Adding a new
limit means adding a key here and one call at the point of creation.

Resolution order for any limit:

1. an explicit override on the organization's subscription
2. the tier default
3. unlimited
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import PlanLimitExceededError
from app.core.logging import get_logger
from app.models import Camera, Store, Subscription, User
from app.models.enums import PlanTier

log = get_logger(__name__)


class LimitKey:
    """Names of the limits the product enforces."""

    MAX_STORES = "max_stores"
    MAX_CAMERAS = "max_cameras"
    MAX_USERS = "max_users"
    EVENT_RETENTION_DAYS = "event_retention_days"
    EVIDENCE_RETENTION_DAYS = "evidence_retention_days"
    CUSTOM_MODELS = "custom_models"
    POS_INTEGRATION = "pos_integration"
    ANALYTICS_HISTORY_DAYS = "analytics_history_days"


#: ``None`` means unlimited. These numbers are a starting point for commercial
#: discussion, not a contract -- which is exactly why they live in one place.
TIER_LIMITS: dict[PlanTier, dict[str, int | bool | None]] = {
    PlanTier.TRIAL: {
        LimitKey.MAX_STORES: 1,
        LimitKey.MAX_CAMERAS: 2,
        LimitKey.MAX_USERS: 3,
        LimitKey.EVENT_RETENTION_DAYS: 7,
        LimitKey.EVIDENCE_RETENTION_DAYS: 3,
        LimitKey.CUSTOM_MODELS: False,
        LimitKey.POS_INTEGRATION: False,
        LimitKey.ANALYTICS_HISTORY_DAYS: 7,
    },
    PlanTier.STARTER: {
        LimitKey.MAX_STORES: 1,
        LimitKey.MAX_CAMERAS: 8,
        LimitKey.MAX_USERS: 5,
        LimitKey.EVENT_RETENTION_DAYS: 30,
        LimitKey.EVIDENCE_RETENTION_DAYS: 14,
        LimitKey.CUSTOM_MODELS: False,
        LimitKey.POS_INTEGRATION: True,
        LimitKey.ANALYTICS_HISTORY_DAYS: 30,
    },
    PlanTier.BUSINESS: {
        LimitKey.MAX_STORES: 10,
        LimitKey.MAX_CAMERAS: 50,
        LimitKey.MAX_USERS: 25,
        LimitKey.EVENT_RETENTION_DAYS: 90,
        LimitKey.EVIDENCE_RETENTION_DAYS: 30,
        LimitKey.CUSTOM_MODELS: True,
        LimitKey.POS_INTEGRATION: True,
        LimitKey.ANALYTICS_HISTORY_DAYS: 180,
    },
    PlanTier.ENTERPRISE: {
        LimitKey.MAX_STORES: None,
        LimitKey.MAX_CAMERAS: None,
        LimitKey.MAX_USERS: None,
        LimitKey.EVENT_RETENTION_DAYS: 365,
        LimitKey.EVIDENCE_RETENTION_DAYS: 90,
        LimitKey.CUSTOM_MODELS: True,
        LimitKey.POS_INTEGRATION: True,
        LimitKey.ANALYTICS_HISTORY_DAYS: 730,
    },
}

#: Applied when an organization has no subscription row at all. Deliberately
#: the most generous tier: a missing billing record is an internal data
#: problem, and locking a paying customer out of their cameras over it would
#: be a far worse failure than briefly under-charging.
FALLBACK_TIER = PlanTier.BUSINESS


@dataclass(frozen=True, slots=True)
class LimitStatus:
    """Where an organization stands against one limit."""

    key: str
    limit: int | None
    current: int

    @property
    def is_unlimited(self) -> bool:
        return self.limit is None

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(self.limit - self.current, 0)

    @property
    def is_exceeded(self) -> bool:
        return self.limit is not None and self.current >= self.limit


def _subscription(session: Session, organization_id: uuid.UUID) -> Subscription | None:
    return session.scalar(
        select(Subscription).where(Subscription.organization_id == organization_id)
    )


def resolve_tier(session: Session, organization_id: uuid.UUID) -> PlanTier:
    subscription = _subscription(session, organization_id)
    return subscription.tier if subscription else FALLBACK_TIER


def get_limit(session: Session, organization_id: uuid.UUID, key: str) -> int | bool | None:
    """Resolve one limit for an organization."""
    subscription = _subscription(session, organization_id)
    if subscription is not None and key in subscription.limit_overrides:
        return subscription.limit_overrides[key]
    tier = subscription.tier if subscription else FALLBACK_TIER
    return TIER_LIMITS.get(tier, {}).get(key)


def is_feature_enabled(session: Session, organization_id: uuid.UUID, key: str) -> bool:
    return bool(get_limit(session, organization_id, key))


#: How to count current usage for each countable limit.
_COUNTERS = {
    LimitKey.MAX_STORES: lambda: (Store, Store.organization_id, Store.deleted_at),
    LimitKey.MAX_CAMERAS: lambda: (Camera, Camera.organization_id, Camera.deleted_at),
    LimitKey.MAX_USERS: lambda: (User, User.organization_id, User.deleted_at),
}


def current_usage(session: Session, organization_id: uuid.UUID, key: str) -> int:
    """Count what the organization currently has against a countable limit.

    Soft-deleted rows do not count: a customer who deleted a camera has freed
    the slot, even though the row survives for audit.
    """
    factory = _COUNTERS.get(key)
    if factory is None:
        return 0
    model, org_column, deleted_column = factory()
    return int(
        session.scalar(
            select(func.count())
            .select_from(model)
            .where(org_column == organization_id, deleted_column.is_(None))
        )
        or 0
    )


def check_limit(session: Session, organization_id: uuid.UUID, key: str) -> LimitStatus:
    """Report standing against one limit without raising."""
    limit = get_limit(session, organization_id, key)
    numeric = limit if isinstance(limit, int) and not isinstance(limit, bool) else None
    return LimitStatus(key=key, limit=numeric, current=current_usage(session, organization_id, key))


def enforce_limit(session: Session, organization_id: uuid.UUID, key: str) -> None:
    """Raise :class:`PlanLimitExceededError` if adding one more would exceed.

    Called *before* creating the resource, so the error names the limit rather
    than surfacing as a confusing failure halfway through a transaction.
    """
    status = check_limit(session, organization_id, key)
    if status.is_exceeded:
        log.info(
            "plan_limit_exceeded",
            organization_id=str(organization_id),
            limit=key,
            current=status.current,
            maximum=status.limit,
        )
        raise PlanLimitExceededError(
            f"Your plan allows up to {status.limit} "
            f"{key.removeprefix('max_').replace('_', ' ')}. "
            "Upgrade your plan to add more.",
            details={"limit": key, "maximum": status.limit, "current": status.current},
        )


def enforce_feature(session: Session, organization_id: uuid.UUID, key: str) -> None:
    """Raise if a boolean feature is not included in the plan."""
    if not is_feature_enabled(session, organization_id, key):
        raise PlanLimitExceededError(
            "This feature is not included in your current plan.",
            details={"feature": key},
        )


def plan_summary(session: Session, organization_id: uuid.UUID) -> dict:
    """Everything the billing/settings page needs about the current plan."""
    subscription = _subscription(session, organization_id)
    tier = subscription.tier if subscription else FALLBACK_TIER
    limits = {}
    for key in TIER_LIMITS[tier]:
        value = get_limit(session, organization_id, key)
        entry: dict = {"limit": value}
        if key in _COUNTERS:
            entry["current"] = current_usage(session, organization_id, key)
        limits[key] = entry
    return {
        "tier": tier.value,
        "status": subscription.status.value if subscription else "ACTIVE",
        "trial_ends_on": (
            subscription.trial_ends_on.isoformat()
            if subscription and subscription.trial_ends_on
            else None
        ),
        "expires_on": (
            subscription.expires_on.isoformat()
            if subscription and subscription.expires_on
            else None
        ),
        "limits": limits,
    }
