"""Zone and counting-line endpoints.

Zones and lines are drawn on a camera, so they are addressed under
``/cameras/{id}/zones``. They are also read across a whole store or
organization for the settings overview, hence the flat ``/zones`` listing.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, DbSession, Scope
from app.schemas.common import MessageResponse
from app.schemas.zone import (
    LineCreate,
    LineRead,
    LineUpdate,
    ZoneCreate,
    ZoneRead,
    ZoneUpdate,
)
from app.services import zones as zone_service

router = APIRouter(tags=["zones"])


# ---------------------------------------------------------------------- zones
@router.get("/zones", response_model=list[ZoneRead], summary="List zones")
def list_zones(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    camera_id: uuid.UUID | None = Query(None),
    store_id: uuid.UUID | None = Query(None),
):
    return [
        ZoneRead.model_validate(zone)
        for zone in zone_service.list_zones(session, scope, camera_id=camera_id, store_id=store_id)
    ]


@router.post(
    "/cameras/{camera_id}/zones",
    response_model=ZoneRead,
    status_code=status.HTTP_201_CREATED,
    summary="Draw a zone on a camera",
)
def create_zone(
    camera_id: uuid.UUID,
    payload: ZoneCreate,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    """Create a zone.

    Geometry is validated with the same functions the vision engine uses, so a
    shape that would silently never fire is rejected here with an explanation
    rather than accepted and quietly ignored at runtime.
    """
    zone = zone_service.create_zone(
        session,
        scope,
        camera_id=camera_id,
        **payload.model_dump(exclude={"name", "polygon", "zone_type"}),
        name=payload.name,
        polygon=payload.polygon,
        zone_type=payload.zone_type,
    )
    session.commit()
    session.refresh(zone)
    return ZoneRead.model_validate(zone)


@router.get(
    "/cameras/{camera_id}/zones",
    response_model=list[ZoneRead],
    summary="Zones on a camera",
)
def list_camera_zones(camera_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    return [
        ZoneRead.model_validate(zone)
        for zone in zone_service.list_zones(session, scope, camera_id=camera_id)
    ]


@router.get("/zones/{zone_id}", response_model=ZoneRead, summary="Get a zone")
def get_zone(zone_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    return ZoneRead.model_validate(zone_service.get_zone(session, scope, zone_id))


@router.patch("/zones/{zone_id}", response_model=ZoneRead, summary="Update a zone")
def update_zone(
    zone_id: uuid.UUID,
    payload: ZoneUpdate,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    """Update a zone.

    A running pipeline picks up the new geometry without restarting, so
    redrawing a zone costs no video gap and does not reset track ids.
    """
    zone = zone_service.update_zone(
        session, scope, zone_id, **payload.model_dump(exclude_unset=True)
    )
    session.commit()
    session.refresh(zone)
    return ZoneRead.model_validate(zone)


@router.delete("/zones/{zone_id}", response_model=MessageResponse, summary="Delete a zone")
def delete_zone(zone_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    zone = zone_service.get_zone(session, scope, zone_id)
    name = zone.name
    zone_service.delete_zone(session, scope, zone_id)
    session.commit()
    return MessageResponse(message=f"Zone '{name}' deleted.")


# ---------------------------------------------------------------------- lines
@router.get("/lines", response_model=list[LineRead], summary="List counting lines")
def list_lines(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    camera_id: uuid.UUID | None = Query(None),
    store_id: uuid.UUID | None = Query(None),
):
    return [
        LineRead.model_validate(line)
        for line in zone_service.list_lines(session, scope, camera_id=camera_id, store_id=store_id)
    ]


@router.post(
    "/cameras/{camera_id}/lines",
    response_model=LineRead,
    status_code=status.HTTP_201_CREATED,
    summary="Draw a counting line on a camera",
)
def create_line(
    camera_id: uuid.UUID,
    payload: LineCreate,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    line = zone_service.create_line(
        session,
        scope,
        camera_id=camera_id,
        **payload.model_dump(exclude={"name", "point_a", "point_b"}),
        name=payload.name,
        point_a=payload.point_a,
        point_b=payload.point_b,
    )
    session.commit()
    session.refresh(line)
    return LineRead.model_validate(line)


@router.get(
    "/cameras/{camera_id}/lines",
    response_model=list[LineRead],
    summary="Counting lines on a camera",
)
def list_camera_lines(camera_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    return [
        LineRead.model_validate(line)
        for line in zone_service.list_lines(session, scope, camera_id=camera_id)
    ]


@router.patch("/lines/{line_id}", response_model=LineRead, summary="Update a line")
def update_line(
    line_id: uuid.UUID,
    payload: LineUpdate,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    line = zone_service.update_line(
        session, scope, line_id, **payload.model_dump(exclude_unset=True)
    )
    session.commit()
    session.refresh(line)
    return LineRead.model_validate(line)


@router.delete("/lines/{line_id}", response_model=MessageResponse, summary="Delete a line")
def delete_line(line_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    line = zone_service.get_line(session, scope, line_id)
    name = line.name
    zone_service.delete_line(session, scope, line_id)
    session.commit()
    return MessageResponse(message=f"Counting line '{name}' deleted.")
