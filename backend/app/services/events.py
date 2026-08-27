"""Event persistence and querying.

This is the translation point between the vision engine's plain
:class:`~vision.events.VisionEvent` dataclasses and the database. The vision
engine knows nothing about SQLAlchemy; this module knows nothing about
detection.

Idempotency matters here more than usual. An edge box that loses connectivity
replays its queued events when it reconnects, and a replayed event must not
become a second incident on the timeline. Every persisted event therefore
carries a ``dedupe_key`` derived from what it *is*, not when it arrived.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.tenancy import TenantScope
from app.models import Alert, Camera, Event, EventEvidence
from app.models.enums import (
    INFERENCE_EVENT_TYPES,
    ConfidenceLevel,
    EventSeverity,
    EventType,
)

log = get_logger(__name__)


def make_dedupe_key(
    *,
    organization_id: uuid.UUID,
    camera_id: str | None,
    event_type: str,
    occurred_at: datetime,
    track_key: str | None,
    zone_id: str | None = None,
) -> str:
    """A stable identity for one occurrence.

    The timestamp is included at **second** resolution: the same event
    replayed from an edge queue carries the same second, while a genuinely
    separate incident a minute later does not collide. Sub-second resolution
    would let float drift in serialisation create duplicates.
    """
    parts = [
        str(organization_id),
        camera_id or "",
        event_type,
        occurred_at.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
        track_key or "",
        zone_id or "",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


def _to_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def persist_vision_event(
    session: Session,
    vision_event,
    *,
    organization_id: uuid.UUID,
    edge_device_id: str | None = None,
) -> Event | None:
    """Store one event from the vision engine.

    Returns ``None`` when the event is a duplicate of one already stored,
    which is the normal outcome when an edge box replays its queue.
    """
    camera_id = _to_uuid(vision_event.camera_id)
    store_id = _to_uuid(vision_event.store_id)

    dedupe = make_dedupe_key(
        organization_id=organization_id,
        camera_id=vision_event.camera_id,
        event_type=vision_event.event_type.value,
        occurred_at=vision_event.occurred_at,
        track_key=vision_event.track_key,
        zone_id=vision_event.zone_id,
    )

    existing = session.scalar(select(Event).where(Event.dedupe_key == dedupe))
    if existing is not None:
        log.debug(
            "event_deduplicated",
            dedupe_key=dedupe,
            event_type=vision_event.event_type.value,
        )
        return None

    try:
        event_type = EventType(vision_event.event_type.value)
        severity = EventSeverity(vision_event.severity.value)
    except ValueError:
        # The vision engine emitted a vocabulary the backend does not know.
        # Dropping it silently would hide a genuine version mismatch.
        log.error(
            "unknown_event_vocabulary",
            event_type=str(vision_event.event_type),
            severity=str(vision_event.severity),
        )
        return None

    confidence_level = None
    if vision_event.confidence_level is not None:
        confidence_level = ConfidenceLevel(vision_event.confidence_level.value)

    event = Event(
        organization_id=organization_id,
        store_id=store_id,
        camera_id=camera_id,
        zone_id=_to_uuid(vision_event.zone_id),
        shelf_id=_to_uuid(vision_event.shelf_id),
        product_id=_to_uuid(vision_event.product_id),
        event_type=event_type,
        severity=severity,
        is_inference=vision_event.is_inference or event_type in INFERENCE_EVENT_TYPES,
        confidence=vision_event.confidence,
        confidence_level=confidence_level,
        rule_name=vision_event.rule_name,
        source_event_ids=list(vision_event.source_event_ids),
        track_key=vision_event.track_key,
        track_id=vision_event.track_id,
        object_class=vision_event.object_class,
        occurred_at=vision_event.occurred_at,
        ended_at=vision_event.ended_at,
        duration_seconds=vision_event.duration_seconds,
        summary=vision_event.summary[:500],
        event_metadata=dict(vision_event.metadata),
        edge_device_id=edge_device_id,
        dedupe_key=dedupe,
    )
    session.add(event)
    session.flush()
    return event


def ingest_vision_event(
    session: Session,
    vision_event,
    *,
    organization_id: uuid.UUID,
    edge_device_id: str | None = None,
    create_alerts: bool = True,
) -> tuple[Event | None, Alert | None]:
    """Persist an event and escalate it if policy says so.

    The single entry point a pipeline sink uses. Returns
    ``(event, alert)``; either may be ``None``.
    """
    from app.services.alerts import create_alert_for_event, store_name_for

    event = persist_vision_event(
        session,
        vision_event,
        organization_id=organization_id,
        edge_device_id=edge_device_id,
    )
    if event is None:
        return None, None

    alert = None
    if create_alerts:
        alert = create_alert_for_event(
            session, event, store_name=store_name_for(session, event.store_id)
        )

    _publish_realtime(event, alert)
    return event, alert


def publish_event_update(event: Event) -> None:
    """Re-broadcast an event whose derived state changed (e.g. evidence attached).

    Best-effort, like :func:`_publish_realtime`: the database row is the truth
    and a Redis outage costs only the live refresh.
    """
    try:
        from app.websocket.broadcast import publish
        from app.websocket.manager import event_message

        publish(event_message(event))
    except Exception as exc:
        log.warning("realtime_publish_skipped", error=str(exc)[:200])


def _publish_realtime(event: Event, alert: Alert | None) -> None:
    """Push the event (and any alert) to connected dashboards.

    Best-effort by design: the event is already persisted, so a Redis outage
    costs live updates, not data. Importing here rather than at module level
    keeps the vision worker free of the WebSocket layer when it runs on an
    edge box with no dashboard attached.
    """
    try:
        from app.websocket.broadcast import publish
        from app.websocket.manager import alert_message, event_message

        publish(event_message(event))
        if alert is not None:
            publish(alert_message(alert))
    except Exception as exc:
        log.warning("realtime_publish_skipped", error=str(exc)[:200])


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_events(
    session: Session,
    scope: TenantScope,
    *,
    store_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
    zone_id: uuid.UUID | None = None,
    event_types: list[EventType] | None = None,
    severities: list[EventSeverity] | None = None,
    track_key: str | None = None,
    product_id: uuid.UUID | None = None,
    is_inference: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Event], int]:
    """The event timeline, newest first.

    Every filter the dashboard's timeline offers is here, because filtering in
    the browser over an events table that grows by millions of rows a month is
    not an option.
    """
    scope.require(Permission.EVENT_READ)

    statement = scope.apply(select(Event), Event)
    if store_id is not None:
        scope.require_store(store_id)
        statement = statement.where(Event.store_id == store_id)
    if camera_id is not None:
        statement = statement.where(Event.camera_id == camera_id)
    if zone_id is not None:
        statement = statement.where(Event.zone_id == zone_id)
    if event_types:
        statement = statement.where(Event.event_type.in_(event_types))
    if severities:
        statement = statement.where(Event.severity.in_(severities))
    if track_key:
        statement = statement.where(Event.track_key == track_key)
    if product_id is not None:
        statement = statement.where(Event.product_id == product_id)
    if is_inference is not None:
        statement = statement.where(Event.is_inference.is_(is_inference))
    if since is not None:
        statement = statement.where(Event.occurred_at >= since)
    if until is not None:
        statement = statement.where(Event.occurred_at <= until)

    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = session.scalars(
        statement.options(selectinload(Event.evidence))
        .order_by(Event.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total


def get_event(session: Session, scope: TenantScope, event_id: uuid.UUID) -> Event:
    scope.require(Permission.EVENT_READ)
    event = session.scalar(
        scope.apply(select(Event), Event)
        .where(Event.id == event_id)
        .options(selectinload(Event.evidence), selectinload(Event.alert))
    )
    if event is None:
        raise NotFoundError("Event not found.", details={"event_id": str(event_id)})
    return event


def get_source_events(session: Session, scope: TenantScope, event: Event) -> list[Event]:
    """The observations an inference was drawn from.

    This is what makes an inference auditable rather than an oracle. The UI
    shows these under any hedged conclusion so a reviewer can judge it.
    """
    if not event.source_event_ids:
        return []
    ids = [uid for uid in (_to_uuid(v) for v in event.source_event_ids) if uid]
    if not ids:
        return []
    return list(
        session.scalars(
            scope.apply(select(Event), Event).where(Event.id.in_(ids)).order_by(Event.occurred_at)
        ).all()
    )


def event_counts_by_type(
    session: Session,
    scope: TenantScope,
    *,
    since: datetime | None = None,
    store_id: uuid.UUID | None = None,
) -> dict[str, int]:
    scope.require(Permission.EVENT_READ)
    statement = scope.apply(select(Event.event_type, func.count()), Event)
    if since is not None:
        statement = statement.where(Event.occurred_at >= since)
    if store_id is not None:
        statement = statement.where(Event.store_id == store_id)
    return {
        event_type.value: int(count)
        for event_type, count in session.execute(statement.group_by(Event.event_type))
    }


def today_summary(session: Session, scope: TenantScope) -> dict:
    """Counts for the dashboard's "today" tiles.

    Uses a rolling 24 hours rather than a calendar day: stores span several
    timezones and a single organization's "today" is genuinely ambiguous.
    The UI labels it as the last 24 hours.
    """
    scope.require(Permission.EVENT_READ)
    since = datetime.now(UTC) - timedelta(hours=24)

    base = scope.apply(select(func.count()).select_from(Event), Event).where(
        Event.occurred_at >= since
    )
    total = int(session.scalar(base) or 0)
    inferences = int(session.scalar(base.where(Event.is_inference.is_(True))) or 0)
    high = int(
        session.scalar(base.where(Event.severity.in_([EventSeverity.HIGH, EventSeverity.CRITICAL])))
        or 0
    )
    crossings = int(session.scalar(base.where(Event.event_type == EventType.LINE_CROSSED)) or 0)
    return {
        "window_hours": 24,
        "events": total,
        "inferences": inferences,
        "high_severity": high,
        "line_crossings": crossings,
    }


def prune_old_events(session: Session, *, organization_id: uuid.UUID, retention_days: int) -> int:
    """Delete events past their retention window. Returns how many.

    Evidence rows cascade with the event, and the retention worker removes the
    files separately. Retention is a privacy obligation, not housekeeping --
    see ``docs/PRIVACY.md``.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    rows = session.scalars(
        select(Event).where(Event.organization_id == organization_id, Event.occurred_at < cutoff)
    ).all()
    for row in rows:
        session.delete(row)
    session.flush()
    if rows:
        log.info(
            "events_pruned",
            organization_id=str(organization_id),
            deleted=len(rows),
            retention_days=retention_days,
        )
    return len(rows)


def camera_last_event(session: Session, scope: TenantScope, camera_id: uuid.UUID) -> Event | None:
    scope.require(Permission.EVENT_READ)
    return session.scalar(
        scope.apply(select(Event), Event)
        .where(Event.camera_id == camera_id)
        .order_by(Event.occurred_at.desc())
        .limit(1)
    )


def attach_evidence(
    session: Session,
    event: Event,
    *,
    kind,
    storage_path: str,
    mime_type: str = "image/jpeg",
    size_bytes: int = 0,
    checksum: str | None = None,
    width: int | None = None,
    height: int | None = None,
    duration_seconds: float | None = None,
    bbox: list | None = None,
    captured_at: datetime | None = None,
    retention_days: int = 30,
) -> EventEvidence:
    """Record an evidence artefact against an event.

    ``expires_at`` is set here rather than left to a sweeper's judgement, so
    every stored artefact has a deletion date from the moment it is created.
    """
    now = datetime.now(UTC)
    evidence = EventEvidence(
        organization_id=event.organization_id,
        event_id=event.id,
        kind=kind,
        storage_path=storage_path,
        mime_type=mime_type,
        size_bytes=size_bytes,
        checksum=checksum,
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        bbox=bbox,
        captured_at=captured_at or now,
        expires_at=now + timedelta(days=retention_days),
    )
    session.add(evidence)
    session.flush()
    return evidence


def camera_ids_for_scope(session: Session, scope: TenantScope) -> list[uuid.UUID]:
    """Cameras the caller may see. Used to filter the realtime stream."""
    return list(
        session.scalars(
            scope.apply(select(Camera.id).where(Camera.deleted_at.is_(None)), Camera)
        ).all()
    )
