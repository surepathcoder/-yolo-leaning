"""Store endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, status

from app.api.deps import CurrentUser, DbSession, Scope, get_client_ip
from app.models.enums import AuditAction
from app.schemas.common import MessageResponse, Page
from app.schemas.store import (
    OperatingHoursRead,
    SetOperatingHoursRequest,
    StoreCreate,
    StoreRead,
    StoreSummary,
    StoreUpdate,
)
from app.services import stores as store_service
from app.services.audit import record_audit

router = APIRouter(prefix="/stores", tags=["stores"])


@router.get("", response_model=Page[StoreSummary], summary="List stores")
def list_stores(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_inactive: bool = Query(True),
):
    rows, total = store_service.list_stores(
        session,
        scope,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
    )
    return Page[StoreSummary](
        items=[StoreSummary.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=StoreRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a store",
)
def create_store(
    payload: StoreCreate,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    data = payload.model_dump(exclude={"use_default_hours"})
    store = store_service.create_store(session, scope, **data)

    if payload.use_default_hours:
        store_service.set_operating_hours(
            session, scope, store.id, store_service.default_operating_hours()
        )

    record_audit(
        session,
        action=AuditAction.CREATE,
        organization_id=store.organization_id,
        user=user,
        resource_type="store",
        resource_id=str(store.id),
        description=f"Store '{store.name}' created",
        ip_address=get_client_ip(request),
        request_id=getattr(request.state, "request_id", None),
    )
    session.commit()
    session.refresh(store)
    return StoreRead.model_validate(store)


@router.get("/{store_id}", response_model=StoreRead, summary="Get a store")
def get_store(store_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    return StoreRead.model_validate(store_service.get_store(session, scope, store_id))


@router.patch("/{store_id}", response_model=StoreRead, summary="Update a store")
def update_store(
    store_id: uuid.UUID,
    payload: StoreUpdate,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    store = store_service.update_store(
        session, scope, store_id, **payload.model_dump(exclude_unset=True)
    )
    record_audit(
        session,
        action=AuditAction.UPDATE,
        organization_id=store.organization_id,
        user=user,
        resource_type="store",
        resource_id=str(store.id),
        description=f"Store '{store.name}' updated",
        changes=payload.model_dump(exclude_unset=True),
        ip_address=get_client_ip(request),
    )
    session.commit()
    session.refresh(store)
    return StoreRead.model_validate(store)


@router.delete("/{store_id}", response_model=MessageResponse, summary="Delete a store")
def delete_store(
    store_id: uuid.UUID,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    """Soft-delete a store and disable its cameras.

    The store's events and evidence are retained: they are the customer's
    record of what happened, and deleting them would be destroying evidence.
    """
    store = store_service.get_store(session, scope, store_id)
    name = store.name
    organization_id = store.organization_id

    store_service.delete_store(session, scope, store_id)
    record_audit(
        session,
        action=AuditAction.DELETE,
        organization_id=organization_id,
        user=user,
        resource_type="store",
        resource_id=str(store_id),
        description=f"Store '{name}' deleted",
        ip_address=get_client_ip(request),
    )
    session.commit()
    return MessageResponse(message=f"Store '{name}' deleted. Its events and evidence are retained.")


@router.get(
    "/{store_id}/operating-hours",
    response_model=list[OperatingHoursRead],
    summary="Get operating hours",
)
def get_operating_hours(store_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    store = store_service.get_store(session, scope, store_id)
    return [
        OperatingHoursRead.model_validate(hours)
        for hours in sorted(store.operating_hours, key=lambda h: h.day_of_week)
    ]


@router.put(
    "/{store_id}/operating-hours",
    response_model=list[OperatingHoursRead],
    summary="Set operating hours",
)
def set_operating_hours(
    store_id: uuid.UUID,
    payload: SetOperatingHoursRequest,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    """Replace the whole week.

    Days omitted are treated as **closed**, which matches how the after-hours
    rule reads an unconfigured day: fail loud rather than fail silent.
    """
    rows = store_service.set_operating_hours(
        session, scope, store_id, [entry.model_dump() for entry in payload.days]
    )
    store = store_service.get_store(session, scope, store_id)
    record_audit(
        session,
        action=AuditAction.CONFIG_CHANGED,
        organization_id=store.organization_id,
        user=user,
        resource_type="store",
        resource_id=str(store_id),
        description=f"Operating hours updated for '{store.name}'",
        ip_address=get_client_ip(request),
    )
    session.commit()
    return [
        OperatingHoursRead.model_validate(row) for row in sorted(rows, key=lambda r: r.day_of_week)
    ]
