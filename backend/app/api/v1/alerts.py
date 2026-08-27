"""Alert endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, Request

from app.api.deps import CurrentUser, DbSession, Scope
from app.models.enums import AlertStatus, EventSeverity
from app.schemas.common import Page
from app.schemas.event import (
    AlertRead,
    AlertSummaryResponse,
    FalsePositiveReport,
    ResolveAlertRequest,
    build_event_summary,
)
from app.services import alerts as alert_service

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _to_read(alert) -> AlertRead:
    read = AlertRead.model_validate(alert)
    if alert.event is not None:
        read.event = build_event_summary(alert.event)
    return read


@router.get("", response_model=Page[AlertRead], summary="List alerts")
def list_alerts(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    alert_status: AlertStatus | None = Query(None, alias="status"),
    severity: EventSeverity | None = Query(None),
    store_id: uuid.UUID | None = Query(None),
    since: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows, total = alert_service.list_alerts(
        session,
        scope,
        status=alert_status,
        severity=severity,
        store_id=store_id,
        since=since,
        limit=limit,
        offset=offset,
    )
    return Page[AlertRead](
        items=[_to_read(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/summary", response_model=AlertSummaryResponse, summary="Alert counts")
def summary(session: DbSession, scope: Scope, _user: CurrentUser):
    return AlertSummaryResponse(**alert_service.alert_summary(session, scope))


@router.get(
    "/false-positive-rate",
    response_model=FalsePositiveReport,
    summary="Measured false-positive rate",
)
def false_positive_rate(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    since: datetime | None = Query(None),
):
    """How often operators judged an alert not worth raising.

    Counts only alerts a human explicitly judged. Reporting a rate over
    unjudged alerts would be inventing a number.
    """
    return FalsePositiveReport(**alert_service.false_positive_rate(session, scope, since=since))


@router.get("/{alert_id}", response_model=AlertRead, summary="Get an alert")
def get_alert(alert_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    return _to_read(alert_service.get_alert(session, scope, alert_id))


@router.post("/{alert_id}/acknowledge", response_model=AlertRead, summary="Acknowledge an alert")
def acknowledge(
    alert_id: uuid.UUID,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    """Claim an alert so nobody else duplicates the work."""
    alert = alert_service.acknowledge_alert(session, scope, alert_id, user)
    session.commit()
    session.refresh(alert)
    return _to_read(alert)


@router.post("/{alert_id}/resolve", response_model=AlertRead, summary="Resolve an alert")
def resolve(
    alert_id: uuid.UUID,
    payload: ResolveAlertRequest,
    request: Request,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
):
    """Close an alert and record what actually happened.

    ``was_actionable`` is the feedback loop: it is the only ground truth for
    measuring the false-positive rate, and without it threshold tuning is
    guesswork.
    """
    alert = alert_service.resolve_alert(
        session,
        scope,
        alert_id,
        user,
        notes=payload.notes,
        was_actionable=payload.was_actionable,
        dismiss=payload.dismiss,
    )
    session.commit()
    session.refresh(alert)
    return _to_read(alert)
