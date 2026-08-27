"""Store management.

Every function takes an explicit :class:`~app.core.tenancy.TenantScope`. There
is no ambient "current organization" — a service that could be called without
a scope is a service that will eventually be called without one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.tenancy import TenantScope
from app.models import Camera, Store, StoreOperatingHours
from app.models.enums import AuditAction, CameraStatus
from app.services.audit import record_audit
from app.services.plan_limits import LimitKey, enforce_limit

log = get_logger(__name__)


def _base_query():
    return select(Store).where(Store.deleted_at.is_(None))


def list_stores(
    session: Session,
    scope: TenantScope,
    *,
    include_inactive: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Store], int]:
    """Stores visible to the caller, newest first. Returns ``(rows, total)``."""
    scope.require(Permission.STORE_READ)

    statement = scope.apply(_base_query(), Store, store_column="id")
    if not include_inactive:
        statement = statement.where(Store.is_active.is_(True))

    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = session.scalars(
        statement.options(selectinload(Store.operating_hours))
        .order_by(Store.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total


def get_store(session: Session, scope: TenantScope, store_id: uuid.UUID) -> Store:
    """Fetch one store, or raise.

    A store in another organization raises ``NOT_FOUND`` rather than
    ``FORBIDDEN``: telling a caller "that exists but is not yours" leaks the
    existence of another tenant's data.
    """
    scope.require(Permission.STORE_READ)
    store = session.scalar(
        scope.apply(_base_query(), Store, store_column="id")
        .where(Store.id == store_id)
        .options(selectinload(Store.operating_hours))
    )
    if store is None:
        raise NotFoundError("Store not found.", details={"store_id": str(store_id)})
    return store


def create_store(
    session: Session,
    scope: TenantScope,
    *,
    name: str,
    code: str,
    timezone: str | None = None,
    **fields,
) -> Store:
    scope.require(Permission.STORE_WRITE)
    organization_id = scope.organization_id
    if organization_id is None:
        raise NotFoundError("An organization is required to create a store.")

    enforce_limit(session, organization_id, LimitKey.MAX_STORES)

    existing = session.scalar(
        _base_query().where(Store.organization_id == organization_id, Store.code == code)
    )
    if existing is not None:
        raise ConflictError(
            f"A store with the code '{code}' already exists.",
            details={"field": "code"},
        )

    store = Store(
        organization_id=organization_id,
        name=name.strip(),
        code=code.strip(),
        timezone=timezone,
        **fields,
    )
    session.add(store)
    session.flush()

    record_audit(
        session,
        action=AuditAction.CREATE,
        organization_id=organization_id,
        user=None,
        resource_type="store",
        resource_id=str(store.id),
        description=f"Store '{store.name}' created",
    )
    log.info("store_created", store_id=str(store.id), code=store.code)
    return store


def update_store(session: Session, scope: TenantScope, store_id: uuid.UUID, **changes) -> Store:
    scope.require(Permission.STORE_WRITE)
    store = get_store(session, scope, store_id)
    scope.require_store(store.id)

    applied: dict[str, object] = {}
    for field, value in changes.items():
        if value is None or not hasattr(store, field):
            continue
        if getattr(store, field) != value:
            applied[field] = value
            setattr(store, field, value)

    if applied:
        session.flush()
        record_audit(
            session,
            action=AuditAction.UPDATE,
            organization_id=store.organization_id,
            user=None,
            resource_type="store",
            resource_id=str(store.id),
            description=f"Store '{store.name}' updated",
            changes=applied,
        )
    return store


def delete_store(session: Session, scope: TenantScope, store_id: uuid.UUID) -> None:
    """Soft-delete a store and disable its cameras.

    Never a hard delete: the events and evidence the store produced remain the
    customer's record of what happened, and removing the row would orphan
    them. Cameras are disabled so no pipeline keeps running for a store that
    no longer exists.
    """
    scope.require(Permission.STORE_DELETE)
    store = get_store(session, scope, store_id)
    scope.require_store(store.id)

    now = datetime.now(UTC)
    store.deleted_at = now
    store.is_active = False

    cameras = session.scalars(
        select(Camera).where(Camera.store_id == store.id, Camera.deleted_at.is_(None))
    ).all()
    for camera in cameras:
        camera.is_enabled = False
        camera.status = CameraStatus.DISABLED

    session.flush()
    record_audit(
        session,
        action=AuditAction.DELETE,
        organization_id=store.organization_id,
        user=None,
        resource_type="store",
        resource_id=str(store.id),
        description=f"Store '{store.name}' deleted ({len(cameras)} camera(s) disabled)",
    )
    log.info("store_deleted", store_id=str(store.id), cameras_disabled=len(cameras))


# ---------------------------------------------------------------------------
# Operating hours
# ---------------------------------------------------------------------------


def set_operating_hours(
    session: Session,
    scope: TenantScope,
    store_id: uuid.UUID,
    days: list[dict],
) -> list[StoreOperatingHours]:
    """Replace a store's weekly hours.

    Replacement rather than merge: partial updates to a weekly schedule are
    how stores end up with a Tuesday that nobody remembers configuring. The
    caller sends the whole week.

    Days omitted from ``days`` are treated as **closed**, which matches the
    schedule engine's fail-safe reading.
    """
    scope.require(Permission.STORE_WRITE)
    store = get_store(session, scope, store_id)
    scope.require_store(store.id)

    for entry in days:
        day = int(entry["day_of_week"])
        if not 0 <= day <= 6:
            raise ConflictError("day_of_week must be 0 (Monday) to 6 (Sunday).")
        if not entry.get("is_closed") and (
            entry.get("opens_at") is None or entry.get("closes_at") is None
        ):
            raise ConflictError(
                f"Day {day} is marked open but has no opening times.",
                details={"day_of_week": day},
            )

    for existing in list(store.operating_hours):
        session.delete(existing)
    session.flush()

    rows = [
        StoreOperatingHours(
            store_id=store.id,
            day_of_week=int(entry["day_of_week"]),
            opens_at=entry.get("opens_at"),
            closes_at=entry.get("closes_at"),
            is_closed=bool(entry.get("is_closed", False)),
        )
        for entry in days
    ]
    session.add_all(rows)
    session.flush()

    record_audit(
        session,
        action=AuditAction.CONFIG_CHANGED,
        organization_id=store.organization_id,
        user=None,
        resource_type="store",
        resource_id=str(store.id),
        description=f"Operating hours updated for '{store.name}'",
        changes={"days_configured": len(rows)},
    )
    return rows


def default_operating_hours(opens: time = time(8, 0), closes: time = time(22, 0)) -> list[dict]:
    """A sensible seven-day default for a new store."""
    return [
        {"day_of_week": day, "opens_at": opens, "closes_at": closes, "is_closed": False}
        for day in range(7)
    ]


def build_schedule(store: Store, organization_timezone: str = "UTC"):
    """Build the vision engine's schedule object from a store row.

    Lives here rather than in ``vision/`` because it is the translation point
    between the ORM and the engine, and ``vision/`` must not import models.
    """
    from vision.events import OperatingSchedule

    return OperatingSchedule.from_rows(
        store.timezone or organization_timezone,
        [
            {
                "day_of_week": hours.day_of_week,
                "opens_at": hours.opens_at,
                "closes_at": hours.closes_at,
                "is_closed": hours.is_closed,
            }
            for hours in store.operating_hours
        ],
    )
