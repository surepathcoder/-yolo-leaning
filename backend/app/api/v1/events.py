"""Event timeline endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession, Scope
from app.models.enums import EventSeverity, EventType
from app.schemas.common import Page
from app.schemas.event import (
    EventDetail,
    EventSummary,
    EventTypeInfo,
    TodaySummary,
    build_event_summary,
    build_evidence_read,
    event_type_catalog,
)
from app.services import events as event_service

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=Page[EventSummary], summary="Event timeline")
def list_events(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    store_id: uuid.UUID | None = Query(None),
    camera_id: uuid.UUID | None = Query(None),
    zone_id: uuid.UUID | None = Query(None),
    event_type: list[EventType] | None = Query(None),
    severity: list[EventSeverity] | None = Query(None),
    track_key: str | None = Query(None, max_length=120),
    product_id: uuid.UUID | None = Query(None),
    is_inference: bool | None = Query(
        None, description="true = conclusions only, false = measurements only"
    ),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """The event timeline, newest first.

    Every filter is applied in PostgreSQL. Filtering in the browser over a
    table that grows by millions of rows a month is not an option.
    """
    rows, total = event_service.list_events(
        session,
        scope,
        store_id=store_id,
        camera_id=camera_id,
        zone_id=zone_id,
        event_types=event_type,
        severities=severity,
        track_key=track_key,
        product_id=product_id,
        is_inference=is_inference,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    return Page[EventSummary](
        items=[build_event_summary(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/types", response_model=list[EventTypeInfo], summary="Event type catalogue")
def list_event_types(_user: CurrentUser):
    """Every event type and whether it is an inference. For filter UIs."""
    return event_type_catalog()


@router.get("/summary", response_model=TodaySummary, summary="Last 24 hours")
def summary(session: DbSession, scope: Scope, _user: CurrentUser):
    """Counts over a rolling 24 hours.

    Rolling rather than calendar-day: an organization's stores can span
    timezones, so "today" is genuinely ambiguous. The UI labels it plainly.
    """
    return TodaySummary(**event_service.today_summary(session, scope))


@router.get("/counts", response_model=dict[str, int], summary="Counts by event type")
def counts_by_type(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    since: datetime | None = Query(None),
    store_id: uuid.UUID | None = Query(None),
):
    return event_service.event_counts_by_type(session, scope, since=since, store_id=store_id)


@router.get("/{event_id}", response_model=EventDetail, summary="Get an event")
def get_event(event_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    """One event, with the observations behind it if it is an inference.

    Returning the source events inline is what makes a hedged conclusion
    auditable rather than an oracle: the reviewer sees what it rests on.
    """
    event = event_service.get_event(session, scope, event_id)
    sources = event_service.get_source_events(session, scope, event)

    detail = EventDetail.model_validate(event)
    detail.evidence = [build_evidence_read(item) for item in event.evidence]
    detail.source_events = [build_event_summary(item) for item in sources]
    detail.alert_id = event.alert.id if event.alert else None
    return detail
