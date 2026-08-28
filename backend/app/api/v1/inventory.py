"""Product, shelf and inventory endpoints.

Shelves are configured against a store and optionally a camera, so the
listings are flat and filtered rather than nested under a camera: an operator
reviewing stock thinks in aisles, not in cameras.

Every read here reports occupancy as a ratio and a coarse level. There is
deliberately no endpoint that returns a unit count, because the vision engine
cannot produce one honestly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, DbSession, Scope
from app.models.enums import InventoryEventType
from app.schemas.common import MessageResponse, Page
from app.schemas.inventory import (
    InventoryEventRead,
    InventorySnapshotRead,
    InventorySummary,
    ProductCreate,
    ProductDetectionRead,
    ProductRead,
    ProductUpdate,
    ShelfCreate,
    ShelfRead,
    ShelfStatus,
    ShelfUpdate,
    SnapshotIngest,
)
from app.services import inventory as inventory_service

router = APIRouter(tags=["inventory"])


# ------------------------------------------------------------------ products
@router.get("/products", response_model=list[ProductRead], summary="List products")
def list_products(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    category: str | None = Query(None, max_length=120),
    search: str | None = Query(None, max_length=120),
    include_inactive: bool = Query(False),
):
    return [
        ProductRead.model_validate(product)
        for product in inventory_service.list_products(
            session,
            scope,
            category=category,
            search=search,
            include_inactive=include_inactive,
        )
    ]


@router.post(
    "/products",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a product",
)
def create_product(payload: ProductCreate, session: DbSession, scope: Scope, _user: CurrentUser):
    product = inventory_service.create_product(session, scope, **payload.model_dump())
    session.commit()
    return ProductRead.model_validate(product)


@router.get("/products/{product_id}", response_model=ProductRead, summary="Get a product")
def get_product(product_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    return ProductRead.model_validate(inventory_service.get_product(session, scope, product_id))


@router.patch("/products/{product_id}", response_model=ProductRead, summary="Update a product")
def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    product = inventory_service.update_product(
        session, scope, product_id, **payload.model_dump(exclude_unset=True)
    )
    session.commit()
    return ProductRead.model_validate(product)


@router.delete("/products/{product_id}", response_model=MessageResponse, summary="Remove a product")
def delete_product(product_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    inventory_service.delete_product(session, scope, product_id)
    session.commit()
    return MessageResponse(message="Product removed.")


# ------------------------------------------------------------------- shelves
@router.get("/shelves", response_model=list[ShelfRead], summary="List shelves")
def list_shelves(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    store_id: uuid.UUID | None = Query(None),
    camera_id: uuid.UUID | None = Query(None),
):
    return [
        ShelfRead.model_validate(shelf)
        for shelf in inventory_service.list_shelves(
            session, scope, store_id=store_id, camera_id=camera_id
        )
    ]


@router.post(
    "/shelves",
    response_model=ShelfRead,
    status_code=status.HTTP_201_CREATED,
    summary="Configure a shelf",
)
def create_shelf(payload: ShelfCreate, session: DbSession, scope: Scope, _user: CurrentUser):
    """Create a shelf.

    The outline is validated with the vision engine's own geometry functions,
    so a shape that would never produce a usable reading is rejected here with
    an explanation rather than accepted and silently ignored at runtime.
    """
    shelf = inventory_service.create_shelf(session, scope, **payload.model_dump())
    session.commit()
    return ShelfRead.model_validate(shelf)


@router.get("/shelves/{shelf_id}", response_model=ShelfRead, summary="Get a shelf")
def get_shelf(shelf_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    return ShelfRead.model_validate(inventory_service.get_shelf(session, scope, shelf_id))


@router.patch("/shelves/{shelf_id}", response_model=ShelfRead, summary="Update a shelf")
def update_shelf(
    shelf_id: uuid.UUID,
    payload: ShelfUpdate,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    shelf = inventory_service.update_shelf(
        session, scope, shelf_id, **payload.model_dump(exclude_unset=True)
    )
    session.commit()
    return ShelfRead.model_validate(shelf)


@router.delete("/shelves/{shelf_id}", response_model=MessageResponse, summary="Remove a shelf")
def delete_shelf(shelf_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    inventory_service.delete_shelf(session, scope, shelf_id)
    session.commit()
    return MessageResponse(message="Shelf removed.")


# -------------------------------------------------------------------- status
@router.get(
    "/inventory/status",
    response_model=list[ShelfStatus],
    summary="Current state of every shelf",
)
def inventory_status(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    store_id: uuid.UUID | None = Query(None),
):
    """Every shelf's latest known state.

    A shelf that has never been read, or whose recent readings were all
    blocked, reports ``UNKNOWN`` rather than a stale or invented number.
    """
    shelves = inventory_service.list_shelves(session, scope, store_id=store_id)
    return [
        ShelfStatus.model_validate(inventory_service.shelf_status(session, scope, shelf))
        for shelf in shelves
    ]


@router.get("/inventory/summary", response_model=InventorySummary, summary="Inventory overview")
def inventory_summary(session: DbSession, scope: Scope, _user: CurrentUser):
    return InventorySummary.model_validate(inventory_service.summary(session, scope))


# ------------------------------------------------------------------- history
@router.get(
    "/inventory/snapshots",
    response_model=Page[InventorySnapshotRead],
    summary="Shelf reading history",
)
def list_snapshots(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    shelf_id: uuid.UUID | None = Query(None),
    since: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows, total = inventory_service.list_snapshots(
        session, scope, shelf_id=shelf_id, since=since, limit=limit, offset=offset
    )
    return Page[InventorySnapshotRead](
        items=[InventorySnapshotRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/inventory/product-detections",
    response_model=Page[ProductDetectionRead],
    summary="Custom-model product detections",
)
def list_product_detections(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    camera_id: uuid.UUID | None = Query(None),
    shelf_id: uuid.UUID | None = Query(None),
    product_id: uuid.UUID | None = Query(None),
    since: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """What the assigned custom model has recognised, newest first.

    ``product_id`` is null on a row whose class name matches no catalogue
    product yet -- the detection is kept regardless.
    """
    rows, total = inventory_service.list_product_detections(
        session,
        scope,
        camera_id=camera_id,
        shelf_id=shelf_id,
        product_id=product_id,
        since=since,
        limit=limit,
        offset=offset,
    )
    return Page[ProductDetectionRead](
        items=[ProductDetectionRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/inventory/events",
    response_model=Page[InventoryEventRead],
    summary="Inventory event history",
)
def list_events(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    shelf_id: uuid.UUID | None = Query(None),
    event_type: InventoryEventType | None = Query(None),
    since: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Confirmed changes in shelf state.

    Each row says what changed, not who changed it or why. ``track_key`` is
    an anonymous per-camera-session id and must not be presented as an
    attribution -- see ``docs/PRIVACY.md``.
    """
    rows, total = inventory_service.list_events(
        session,
        scope,
        shelf_id=shelf_id,
        event_type=event_type,
        since=since,
        limit=limit,
        offset=offset,
    )
    return Page[InventoryEventRead](
        items=[InventoryEventRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/shelves/{shelf_id}/snapshots",
    response_model=InventorySnapshotRead,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a shelf reading",
)
def ingest_snapshot(
    shelf_id: uuid.UUID,
    payload: SnapshotIngest,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    """Record a reading taken by a pipeline.

    Edge pipelines post readings back when connectivity allows, so this
    accepts a ``captured_at`` in the past rather than stamping arrival time --
    an event must be attributable to when it happened.
    """
    snapshot = inventory_service.record_snapshot(session, scope, shelf_id, **payload.model_dump())
    session.commit()
    return InventorySnapshotRead.model_validate(snapshot)
