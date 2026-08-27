"""Product catalogue, shelf configuration and inventory history.

Shelf geometry is validated here with the same functions the vision engine
uses at runtime, for the reason given in ``services/zones.py``: a shelf that
the API accepts but the engine rejects becomes a shelf that silently never
reports, and nobody finds out until someone asks why stock alerts stopped.

Two rules in this module are worth stating plainly, because they are what
keep the feature honest rather than merely working.

**An occluded reading is not evidence.** Snapshots taken while something
blocked the shelf are stored -- they are a true record of what the camera
saw -- but they never establish a shelf's status, and they are excluded from
the low/empty counts. A shelf hidden behind a pallet reads UNKNOWN, and the
summary says so rather than quietly calling it stocked.

**Nothing here concludes why stock moved.** An inventory event records that
coverage changed. Restocking, a customer, a staff member tidying a display
and a detector error all produce the same row. Correlating that with the till
is milestone 16's job, and what it produces is labelled an inference.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.tenancy import TenantScope
from app.models import (
    Camera,
    InventoryEvent,
    InventorySnapshot,
    Product,
    Shelf,
)
from app.models.enums import AuditAction, InventoryEventType, OccupancyLevel
from app.services.audit import record_audit
from app.services.cameras import get_camera
from app.services.stores import get_store
from app.services.zones import InvalidGeometryError
from vision.zones import GeometryError, normalise_polygon, polygon_area

log = get_logger(__name__)

#: A shelf smaller than this fraction of the frame cannot be measured
#: reliably: at 64x64 sampling it would be a handful of grid cells, and one
#: stray detection would swing its occupancy from empty to full.
MIN_SHELF_AREA = 0.002


def validate_shelf_polygon(polygon: list | None) -> list[list[float]] | None:
    """Validate a shelf outline. ``None`` means the whole frame."""
    if polygon is None:
        return None
    try:
        points = normalise_polygon(polygon)
    except GeometryError as exc:
        raise InvalidGeometryError(str(exc)) from exc

    for x, y in points:
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise InvalidGeometryError(
                "Shelf points must be inside the camera view (0 to 1).",
                details={"point": [x, y]},
            )

    area = polygon_area(points)
    if area < MIN_SHELF_AREA:
        raise InvalidGeometryError(
            "This shelf is too small to measure reliably. "
            "Draw it around the whole shelf face you want monitored.",
            details={"area_fraction": round(area, 5)},
        )
    return [[x, y] for x, y in points]


def validate_thresholds(empty: float | None, low: float | None) -> None:
    """Thresholds must stay ordered, or every shelf reads EMPTY forever."""
    if empty is None or low is None:
        return
    if empty > low:
        raise ValidationError(
            "The empty threshold must not be higher than the low threshold.",
            details={"empty_threshold": empty, "low_threshold": low},
        )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def list_products(
    session: Session,
    scope: TenantScope,
    *,
    category: str | None = None,
    search: str | None = None,
    include_inactive: bool = False,
) -> list[Product]:
    scope.require(Permission.PRODUCT_READ)
    statement = select(Product).where(Product.deleted_at.is_(None))
    scope.require_organization(scope.organization_id)
    statement = statement.where(Product.organization_id == scope.organization_id)
    if not include_inactive:
        statement = statement.where(Product.is_active.is_(True))
    if category:
        statement = statement.where(Product.category == category)
    if search:
        term = f"%{search.strip()}%"
        statement = statement.where(Product.name.ilike(term) | Product.sku.ilike(term))
    return list(session.scalars(statement.order_by(Product.name)).all())


def get_product(session: Session, scope: TenantScope, product_id: uuid.UUID) -> Product:
    scope.require(Permission.PRODUCT_READ)
    scope.require_organization(scope.organization_id)
    product = session.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.organization_id == scope.organization_id,
            Product.deleted_at.is_(None),
        )
    )
    if product is None:
        raise NotFoundError("Product not found.", details={"product_id": str(product_id)})
    return product


def create_product(session: Session, scope: TenantScope, **fields) -> Product:
    scope.require(Permission.PRODUCT_WRITE)
    scope.require_organization(scope.organization_id)

    sku = str(fields.get("sku", "")).strip()
    _require_sku_free(session, scope.organization_id, sku)

    product = Product(organization_id=scope.organization_id, **{**fields, "sku": sku})
    session.add(product)
    session.flush()

    record_audit(
        session,
        action=AuditAction.CREATE,
        organization_id=product.organization_id,
        user=None,
        resource_type="product",
        resource_id=str(product.id),
        description=f"Product '{product.name}' ({product.sku}) added",
    )
    log.info("product_created", product_id=str(product.id), sku=product.sku)
    return product


def update_product(
    session: Session, scope: TenantScope, product_id: uuid.UUID, **changes
) -> Product:
    scope.require(Permission.PRODUCT_WRITE)
    product = get_product(session, scope, product_id)

    if changes.get("sku") and changes["sku"].strip() != product.sku:
        _require_sku_free(session, product.organization_id, changes["sku"].strip())
        changes["sku"] = changes["sku"].strip()

    applied: dict[str, object] = {}
    for field, value in changes.items():
        if value is None or not hasattr(product, field):
            continue
        if getattr(product, field) != value:
            applied[field] = value
            setattr(product, field, value)

    if applied:
        session.flush()
        record_audit(
            session,
            action=AuditAction.UPDATE,
            organization_id=product.organization_id,
            user=None,
            resource_type="product",
            resource_id=str(product.id),
            description=f"Product '{product.name}' updated",
            changes=applied,
        )
    return product


def delete_product(session: Session, scope: TenantScope, product_id: uuid.UUID) -> None:
    """Soft-delete. The events that referenced this product still exist."""
    scope.require(Permission.PRODUCT_WRITE)
    product = get_product(session, scope, product_id)
    product.deleted_at = datetime.now(UTC)
    product.is_active = False
    session.flush()
    record_audit(
        session,
        action=AuditAction.DELETE,
        organization_id=product.organization_id,
        user=None,
        resource_type="product",
        resource_id=str(product.id),
        description=f"Product '{product.name}' ({product.sku}) removed",
    )


def _require_sku_free(session: Session, organization_id: uuid.UUID, sku: str) -> None:
    """SKUs are unique per organization, enforced here for a clear message.

    The database constraint is the real guarantee; this exists so the operator
    sees "that SKU is taken" rather than a 500 from an integrity error.
    """
    clash = session.scalar(
        select(Product.id).where(
            Product.organization_id == organization_id,
            Product.sku == sku,
            Product.deleted_at.is_(None),
        )
    )
    if clash is not None:
        raise ConflictError(
            f"A product with SKU '{sku}' already exists.",
            details={"sku": sku},
        )


# ---------------------------------------------------------------------------
# Shelves
# ---------------------------------------------------------------------------


def list_shelves(
    session: Session,
    scope: TenantScope,
    *,
    store_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
) -> list[Shelf]:
    scope.require(Permission.SHELF_READ)
    statement = scope.apply(select(Shelf).where(Shelf.deleted_at.is_(None)), Shelf).options(
        selectinload(Shelf.products)
    )
    if store_id is not None:
        scope.require_store(store_id)
        statement = statement.where(Shelf.store_id == store_id)
    if camera_id is not None:
        statement = statement.where(Shelf.camera_id == camera_id)
    return list(session.scalars(statement.order_by(Shelf.code)).all())


def get_shelf(session: Session, scope: TenantScope, shelf_id: uuid.UUID) -> Shelf:
    scope.require(Permission.SHELF_READ)
    shelf = session.scalar(
        scope.apply(select(Shelf).where(Shelf.deleted_at.is_(None)), Shelf)
        .where(Shelf.id == shelf_id)
        .options(selectinload(Shelf.products))
    )
    if shelf is None:
        raise NotFoundError("Shelf not found.", details={"shelf_id": str(shelf_id)})
    return shelf


def create_shelf(
    session: Session,
    scope: TenantScope,
    *,
    store_id: uuid.UUID,
    camera_id: uuid.UUID | None = None,
    polygon: list | None = None,
    product_ids: list[uuid.UUID] | None = None,
    **fields,
) -> Shelf:
    scope.require(Permission.SHELF_WRITE)
    store = get_store(session, scope, store_id)
    scope.require_store(store.id)

    camera: Camera | None = None
    if camera_id is not None:
        camera = get_camera(session, scope, camera_id)
        if camera.store_id != store.id:
            raise ValidationError(
                "That camera belongs to a different store.",
                details={"camera_id": str(camera_id), "store_id": str(store_id)},
            )

    validate_thresholds(fields.get("empty_threshold"), fields.get("low_threshold"))
    code = str(fields.pop("code", "")).strip()
    _require_shelf_code_free(session, store.id, code)

    shelf = Shelf(
        organization_id=store.organization_id,
        store_id=store.id,
        camera_id=camera.id if camera else None,
        code=code,
        polygon=validate_shelf_polygon(polygon),
        **fields,
    )
    if product_ids:
        shelf.products = _resolve_products(session, scope, product_ids)
    session.add(shelf)
    session.flush()

    record_audit(
        session,
        action=AuditAction.CREATE,
        organization_id=shelf.organization_id,
        user=None,
        resource_type="shelf",
        resource_id=str(shelf.id),
        description=f"Shelf '{shelf.name}' ({shelf.code}) configured",
    )
    log.info("shelf_created", shelf_id=str(shelf.id), code=shelf.code)
    if shelf.camera_id:
        _notify_pipeline(session, shelf.camera_id)
    return shelf


def update_shelf(session: Session, scope: TenantScope, shelf_id: uuid.UUID, **changes) -> Shelf:
    scope.require(Permission.SHELF_WRITE)
    shelf = get_shelf(session, scope, shelf_id)
    scope.require_store(shelf.store_id)

    validate_thresholds(
        changes.get("empty_threshold", shelf.empty_threshold),
        changes.get("low_threshold", shelf.low_threshold),
    )

    if "polygon" in changes and changes["polygon"] is not None:
        changes["polygon"] = validate_shelf_polygon(changes["polygon"])

    if changes.get("code") and changes["code"].strip() != shelf.code:
        _require_shelf_code_free(session, shelf.store_id, changes["code"].strip())
        changes["code"] = changes["code"].strip()

    if changes.get("camera_id") is not None:
        camera = get_camera(session, scope, changes["camera_id"])
        if camera.store_id != shelf.store_id:
            raise ValidationError(
                "That camera belongs to a different store.",
                details={"camera_id": str(camera.id)},
            )

    product_ids = changes.pop("product_ids", None)
    if product_ids is not None:
        shelf.products = _resolve_products(session, scope, product_ids)

    previous_camera_id = shelf.camera_id
    applied: dict[str, object] = {}
    for field, value in changes.items():
        if value is None or not hasattr(shelf, field):
            continue
        if getattr(shelf, field) != value:
            applied[field] = value
            setattr(shelf, field, value)

    if applied or product_ids is not None:
        session.flush()
        record_audit(
            session,
            action=AuditAction.UPDATE,
            organization_id=shelf.organization_id,
            user=None,
            resource_type="shelf",
            resource_id=str(shelf.id),
            description=f"Shelf '{shelf.name}' updated",
            changes={k: v for k, v in applied.items() if k != "polygon"},
        )
        # Both cameras need telling: the one that lost the shelf must stop
        # measuring it, the one that gained it must start.
        for camera_id in {previous_camera_id, shelf.camera_id} - {None}:
            _notify_pipeline(session, camera_id)
    return shelf


def delete_shelf(session: Session, scope: TenantScope, shelf_id: uuid.UUID) -> None:
    """Soft-delete: the snapshots and events it produced still reference it."""
    scope.require(Permission.SHELF_WRITE)
    shelf = get_shelf(session, scope, shelf_id)
    scope.require_store(shelf.store_id)

    shelf.deleted_at = datetime.now(UTC)
    shelf.is_active = False
    shelf.monitoring_enabled = False
    session.flush()

    record_audit(
        session,
        action=AuditAction.DELETE,
        organization_id=shelf.organization_id,
        user=None,
        resource_type="shelf",
        resource_id=str(shelf.id),
        description=f"Shelf '{shelf.name}' ({shelf.code}) removed",
    )
    if shelf.camera_id:
        _notify_pipeline(session, shelf.camera_id)


def _require_shelf_code_free(session: Session, store_id: uuid.UUID, code: str) -> None:
    clash = session.scalar(
        select(Shelf.id).where(
            Shelf.store_id == store_id,
            Shelf.code == code,
            Shelf.deleted_at.is_(None),
        )
    )
    if clash is not None:
        raise ConflictError(
            f"A shelf with code '{code}' already exists in this store.",
            details={"code": code},
        )


def _resolve_products(
    session: Session, scope: TenantScope, product_ids: list[uuid.UUID]
) -> list[Product]:
    """Load products, failing on any the caller may not see.

    Silently dropping unknown ids would let one tenant probe another's
    catalogue by watching which ids survive a round-trip.
    """
    products = [get_product(session, scope, product_id) for product_id in product_ids]
    return products


# ---------------------------------------------------------------------------
# Readings and history
# ---------------------------------------------------------------------------


def record_snapshot(
    session: Session,
    scope: TenantScope,
    shelf_id: uuid.UUID,
    *,
    occupancy_ratio: float,
    occupancy_level: OccupancyLevel,
    confidence: float = 0.0,
    is_occluded: bool = False,
    product_counts: dict | None = None,
    detected_object_count: int | None = None,
    captured_at: datetime | None = None,
    method: str = "occupancy",
    snapshot_path: str | None = None,
) -> InventorySnapshot:
    """Store one reading of one shelf.

    Occluded readings are stored deliberately. They are a true record of what
    the camera could see, and discarding them would leave an unexplained gap
    in the shelf's history exactly when someone asks what happened. They are
    excluded from *status* elsewhere, which is a different question.
    """
    scope.require(Permission.SHELF_WRITE)
    shelf = get_shelf(session, scope, shelf_id)
    scope.require_store(shelf.store_id)

    snapshot = InventorySnapshot(
        organization_id=shelf.organization_id,
        store_id=shelf.store_id,
        shelf_id=shelf.id,
        camera_id=shelf.camera_id,
        occupancy_ratio=float(occupancy_ratio),
        occupancy_level=occupancy_level,
        confidence=float(confidence),
        is_occluded=bool(is_occluded),
        product_counts=product_counts or {},
        detected_object_count=detected_object_count,
        captured_at=captured_at or datetime.now(UTC),
        method=method,
        snapshot_path=snapshot_path,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def record_event(
    session: Session,
    scope: TenantScope,
    shelf_id: uuid.UUID,
    *,
    event_type: InventoryEventType,
    confidence: float = 0.0,
    occupancy_before: float | None = None,
    occupancy_after: float | None = None,
    product_id: uuid.UUID | None = None,
    quantity_delta: int | None = None,
    track_key: str | None = None,
    occurred_at: datetime | None = None,
    event_metadata: dict | None = None,
) -> InventoryEvent:
    """Record a confirmed change in a shelf's state.

    This says *what changed*, never *who changed it or why*. ``track_key`` is
    an anonymous per-session id kept as context for a human reviewer; see
    ``docs/PRIVACY.md``.
    """
    scope.require(Permission.SHELF_WRITE)
    shelf = get_shelf(session, scope, shelf_id)
    scope.require_store(shelf.store_id)

    event = InventoryEvent(
        organization_id=shelf.organization_id,
        store_id=shelf.store_id,
        shelf_id=shelf.id,
        camera_id=shelf.camera_id,
        product_id=product_id,
        event_type=event_type,
        quantity_delta=quantity_delta,
        confidence=float(confidence),
        occupancy_before=occupancy_before,
        occupancy_after=occupancy_after,
        track_key=track_key,
        occurred_at=occurred_at or datetime.now(UTC),
        event_metadata=event_metadata or {},
    )
    session.add(event)
    session.flush()
    log.info(
        "inventory_event",
        shelf_id=str(shelf.id),
        event_type=event_type.value,
        occupancy_after=occupancy_after,
    )
    return event


def list_snapshots(
    session: Session,
    scope: TenantScope,
    *,
    shelf_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[InventorySnapshot], int]:
    scope.require(Permission.INVENTORY_READ)
    statement = scope.apply(select(InventorySnapshot), InventorySnapshot)
    if shelf_id is not None:
        get_shelf(session, scope, shelf_id)  # 404s rather than returning []
        statement = statement.where(InventorySnapshot.shelf_id == shelf_id)
    if since is not None:
        statement = statement.where(InventorySnapshot.captured_at >= since)

    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    rows = session.scalars(
        statement.order_by(InventorySnapshot.captured_at.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), int(total or 0)


def list_events(
    session: Session,
    scope: TenantScope,
    *,
    shelf_id: uuid.UUID | None = None,
    event_type: InventoryEventType | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[InventoryEvent], int]:
    scope.require(Permission.INVENTORY_READ)
    statement = scope.apply(select(InventoryEvent), InventoryEvent)
    if shelf_id is not None:
        get_shelf(session, scope, shelf_id)
        statement = statement.where(InventoryEvent.shelf_id == shelf_id)
    if event_type is not None:
        statement = statement.where(InventoryEvent.event_type == event_type)
    if since is not None:
        statement = statement.where(InventoryEvent.occurred_at >= since)

    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    rows = session.scalars(
        statement.order_by(InventoryEvent.occurred_at.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), int(total or 0)


def latest_snapshot(
    session: Session, scope: TenantScope, shelf_id: uuid.UUID
) -> InventorySnapshot | None:
    """The most recent *usable* reading for a shelf.

    Occluded readings are skipped: the question "how full is this shelf" is
    not answered by a frame in which the shelf was not visible. A shelf whose
    only recent readings are occluded correctly reports UNKNOWN.
    """
    scope.require(Permission.INVENTORY_READ)
    return session.scalar(
        scope.apply(select(InventorySnapshot), InventorySnapshot)
        .where(
            InventorySnapshot.shelf_id == shelf_id,
            InventorySnapshot.is_occluded.is_(False),
            InventorySnapshot.occupancy_level != OccupancyLevel.UNKNOWN,
        )
        .order_by(InventorySnapshot.captured_at.desc())
        .limit(1)
    )


def most_recent_snapshot(
    session: Session, scope: TenantScope, shelf_id: uuid.UUID
) -> InventorySnapshot | None:
    """The most recent reading of any kind, occluded ones included."""
    scope.require(Permission.INVENTORY_READ)
    return session.scalar(
        scope.apply(select(InventorySnapshot), InventorySnapshot)
        .where(InventorySnapshot.shelf_id == shelf_id)
        .order_by(InventorySnapshot.captured_at.desc())
        .limit(1)
    )


def shelf_status(session: Session, scope: TenantScope, shelf: Shelf) -> dict:
    """A shelf's latest known state, for the dashboard.

    Two different snapshots are consulted on purpose. The occupancy figures
    come from the last reading in which the shelf was actually visible, so a
    shopper standing at the shelf does not blank the display. ``is_occluded``
    comes from the *most recent* reading of any kind, because "we cannot see
    this shelf right now" is exactly what the operator needs to know when
    deciding whether to trust the number next to it.
    """
    usable = latest_snapshot(session, scope, shelf.id)
    newest = most_recent_snapshot(session, scope, shelf.id)
    return {
        "shelf_id": shelf.id,
        "name": shelf.name,
        "code": shelf.code,
        "camera_id": shelf.camera_id,
        "monitoring_enabled": shelf.monitoring_enabled,
        "occupancy_ratio": usable.occupancy_ratio if usable else None,
        "occupancy_level": usable.occupancy_level if usable else OccupancyLevel.UNKNOWN,
        "confidence": usable.confidence if usable else None,
        "is_occluded": bool(newest.is_occluded) if newest else False,
        "last_reading_at": usable.captured_at if usable else None,
    }


def summary(session: Session, scope: TenantScope) -> dict:
    """Counts for the inventory overview."""
    scope.require(Permission.INVENTORY_READ)
    shelves = list_shelves(session, scope)

    low = empty = unknown = 0
    for shelf in shelves:
        level = shelf_status(session, scope, shelf)["occupancy_level"]
        if level is OccupancyLevel.EMPTY:
            empty += 1
        elif level is OccupancyLevel.LOW:
            low += 1
        elif level is OccupancyLevel.UNKNOWN:
            unknown += 1

    products = session.scalar(
        select(func.count())
        .select_from(Product)
        .where(
            Product.organization_id == scope.organization_id,
            Product.deleted_at.is_(None),
            Product.is_active.is_(True),
        )
    )
    day_ago = datetime.now(UTC) - timedelta(hours=24)
    events_24h = session.scalar(
        select(func.count()).select_from(
            scope.apply(
                select(InventoryEvent.id).where(InventoryEvent.occurred_at >= day_ago),
                InventoryEvent,
            ).subquery()
        )
    )

    return {
        "shelves_total": len(shelves),
        "shelves_monitored": sum(1 for s in shelves if s.monitoring_enabled),
        "shelves_low": low,
        "shelves_empty": empty,
        "shelves_unknown": unknown,
        "products_total": int(products or 0),
        "events_24h": int(events_24h or 0),
    }


def _notify_pipeline(session: Session, camera_id: uuid.UUID) -> None:
    """Push shelf changes to a running pipeline without restarting it.

    Same reasoning as ``services/zones._notify_pipeline``: a restart would
    reset every track id mid-shift. The stored configuration is the truth, so
    a pipeline that misses an update picks it up on its next start.
    """
    try:
        from app.services.pipeline_manager import build_pipeline_config, manager

        running = manager.get(camera_id)
        if running is None:
            return
        camera = session.get(Camera, camera_id)
        if camera is None:
            return
        running.pipeline.apply_config(build_pipeline_config(session, camera))
        log.info("pipeline_reconfigured", camera_id=str(camera_id))
    except Exception as exc:
        log.warning(
            "pipeline_reconfigure_failed",
            camera_id=str(camera_id),
            error=str(exc)[:200],
        )
