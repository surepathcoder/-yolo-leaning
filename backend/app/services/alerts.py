"""The alert engine.

Not every event is an alert. An alert is the subset a person is expected to
look at, and it carries workflow state so a team can divide the work without
two people chasing the same incident.

Two principles shape this module:

**Alert fatigue is the failure mode.** A system that raises fifty alerts a day
gets ignored, and then it raises the one that mattered and that gets ignored
too. Routing is therefore conservative: severity thresholds per channel,
per-camera rate limits, and grouping of repeats into one alert with a count.

**An inference is never presented as a fact.** An alert built from an
``is_inference`` event is worded as something to check, carries its confidence
level, and links to the observations behind it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.tenancy import TenantScope
from app.models import Alert, Event, Notification, Store, User
from app.models.enums import (
    SEVERITY_ORDER,
    AlertStatus,
    AuditAction,
    EventSeverity,
    EventType,
    NotificationChannel,
    NotificationStatus,
    UserRole,
)
from app.services.audit import record_audit
from app.services.notifications import (
    NotificationMessage,
    NotifierRegistry,
    build_default_registry,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Routing policy
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChannelPolicy:
    """When a channel is used."""

    channel: NotificationChannel
    min_severity: EventSeverity
    #: Roles that receive this channel. Empty means every user in scope.
    roles: frozenset[UserRole] = field(default_factory=frozenset)

    def accepts(self, severity: EventSeverity) -> bool:
        return SEVERITY_ORDER[severity] >= SEVERITY_ORDER[self.min_severity]


#: The default routing table.
#:
#: Dashboard and WebSocket take everything -- they are free and non-intrusive.
#: Email starts at MEDIUM, SMS and WhatsApp at HIGH, because those cost money
#: and interrupt someone's evening. Getting this wrong in the noisy direction
#: is how a security product gets switched off.
DEFAULT_POLICIES: list[ChannelPolicy] = [
    ChannelPolicy(NotificationChannel.DASHBOARD, EventSeverity.INFO),
    ChannelPolicy(NotificationChannel.WEBSOCKET, EventSeverity.LOW),
    ChannelPolicy(
        NotificationChannel.EMAIL,
        EventSeverity.MEDIUM,
        frozenset({UserRole.ORGANIZATION_ADMIN, UserRole.STORE_MANAGER}),
    ),
    ChannelPolicy(
        NotificationChannel.SMS,
        EventSeverity.HIGH,
        frozenset({UserRole.SECURITY_OPERATOR, UserRole.STORE_MANAGER}),
    ),
    ChannelPolicy(
        NotificationChannel.WHATSAPP,
        EventSeverity.HIGH,
        frozenset({UserRole.SECURITY_OPERATOR, UserRole.STORE_MANAGER}),
    ),
]

#: Event types that never become an alert, however severe the rule thinks they
#: are. Line crossings are the classic case: a busy shop produces thousands a
#: day and every one of them is a person walking through a door.
NON_ALERTING_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.LINE_CROSSED,
        EventType.PERSON_DETECTED,
        EventType.OBJECT_DETECTED,
        EventType.PERSON_EXITED_ZONE,
        EventType.OBJECT_EXITED_ZONE,
    }
)

#: Minimum severity for an event to become an alert at all.
ALERT_MIN_SEVERITY = EventSeverity.MEDIUM

#: Repeats of the same alert within this window update the existing one
#: instead of creating another. A person loitering in a restricted area for
#: twenty minutes is one incident, not forty.
#:
#: The window is measured against when the events **occurred**, not when they
#: were ingested. An edge box replaying an hour of queued events after an
#: outage must still produce a faithful timeline: two incidents twenty minutes
#: apart stay two alerts even though they arrive in the same second.
GROUPING_WINDOW = timedelta(minutes=10)

#: Hard ceiling per camera per hour. A camera stuck on a reflection that looks
#: like a person must not be able to bury every other camera's alerts.
MAX_ALERTS_PER_CAMERA_PER_HOUR = 20


def should_alert(event: Event) -> bool:
    """Whether an event warrants human attention."""
    if event.event_type in NON_ALERTING_EVENT_TYPES:
        return False
    return SEVERITY_ORDER[event.severity] >= SEVERITY_ORDER[ALERT_MIN_SEVERITY]


def build_title(event: Event, store_name: str | None = None) -> str:
    """A short, factual headline.

    Inferences are phrased as questions for a human, never as findings.
    """
    labels = {
        EventType.RESTRICTED_AREA_ENTRY: "Restricted area entry",
        EventType.LOITERING: "Extended presence in zone",
        EventType.AFTER_HOURS_ACTIVITY: "Activity outside opening hours",
        EventType.OCCUPANCY_THRESHOLD_EXCEEDED: "Zone occupancy exceeded",
        EventType.DWELL_THRESHOLD_EXCEEDED: "Dwell time exceeded",
        EventType.LOW_STOCK: "Low stock",
        EventType.SHELF_EMPTY: "Shelf empty",
        EventType.PRODUCT_REMOVED: "Product removed from shelf",
        EventType.POSSIBLE_INVENTORY_DISCREPANCY: "Possible inventory discrepancy - review",
        EventType.POSSIBLE_EXIT_WITHOUT_PAYMENT: "Possible exit without payment - review",
        EventType.POTENTIAL_SECURITY_CONCERN: "Potential security concern - review",
        EventType.CAMERA_OFFLINE: "Camera offline",
        EventType.PIPELINE_ERROR: "Camera processing error",
    }
    title = labels.get(event.event_type, event.event_type.value.replace("_", " ").title())
    return f"{title} - {store_name}" if store_name else title


def build_message(event: Event) -> str:
    """The alert body.

    For an inference, the wording makes the epistemic status explicit: what
    was observed, what rule fired, and that a person needs to decide.
    """
    if not event.is_inference:
        return event.summary

    confidence = event.confidence_level.value.lower() if event.confidence_level else "low"
    return (
        f"{event.summary}\n\n"
        f"This is an automated inference at {confidence} confidence, drawn from "
        f"{len(event.source_event_ids)} observation(s) by rule "
        f"'{event.rule_name or 'unknown'}'. It is not a finding of wrongdoing - "
        f"please review the linked evidence before acting."
    )


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def _recent_alert_count(session: Session, camera_id: uuid.UUID | None) -> int:
    if camera_id is None:
        return 0
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    return int(
        session.scalar(
            select(func.count())
            .select_from(Alert)
            .join(Event, Alert.event_id == Event.id)
            .where(Event.camera_id == camera_id, Alert.created_at >= cutoff)
        )
        or 0
    )


def _find_groupable(session: Session, event: Event) -> Alert | None:
    """An open alert for the same incident, close enough in time to group into.

    Matched on event type, camera and zone, and on the *event* timestamps
    being within :data:`GROUPING_WINDOW` of each other. A closed alert is
    never grouped into: once an operator has resolved something, a recurrence
    is genuinely new and deserves its own row.
    """
    window_start = event.occurred_at - GROUPING_WINDOW
    window_end = event.occurred_at + GROUPING_WINDOW
    return session.scalar(
        select(Alert)
        .join(Event, Alert.event_id == Event.id)
        .where(
            Alert.organization_id == event.organization_id,
            Alert.status.in_([AlertStatus.NEW, AlertStatus.ACKNOWLEDGED]),
            Event.id != event.id,
            Event.occurred_at >= window_start,
            Event.occurred_at <= window_end,
            Event.event_type == event.event_type,
            Event.camera_id == event.camera_id,
            Event.zone_id == event.zone_id,
        )
        .order_by(Alert.created_at.desc())
    )


def create_alert_for_event(
    session: Session,
    event: Event,
    *,
    store_name: str | None = None,
    force: bool = False,
) -> Alert | None:
    """Escalate an event to an alert, if policy says it warrants one.

    Returns ``None`` when the event does not qualify, is grouped into an
    existing alert, or the camera has hit its hourly ceiling.
    """
    if not force and not should_alert(event):
        return None

    if event.alert is not None:
        return event.alert

    existing = _find_groupable(session, event)
    if existing is not None:
        # Group: bump the count and the severity if this one is worse, rather
        # than adding a second row for the same ongoing incident.
        occurrences = int(existing.delivery_status.get("occurrences", 1)) + 1
        existing.delivery_status = {**existing.delivery_status, "occurrences": occurrences}
        if SEVERITY_ORDER[event.severity] > SEVERITY_ORDER[existing.severity]:
            existing.severity = event.severity
        session.flush()
        log.info(
            "alert_grouped",
            alert_id=str(existing.id),
            event_type=event.event_type.value,
            occurrences=occurrences,
        )
        return None

    if _recent_alert_count(session, event.camera_id) >= MAX_ALERTS_PER_CAMERA_PER_HOUR:
        log.warning(
            "alert_rate_limited",
            camera_id=str(event.camera_id) if event.camera_id else None,
            event_type=event.event_type.value,
            detail="hourly ceiling reached; alert suppressed",
        )
        return None

    alert = Alert(
        organization_id=event.organization_id,
        store_id=event.store_id,
        event_id=event.id,
        title=build_title(event, store_name),
        message=build_message(event),
        severity=event.severity,
        status=AlertStatus.NEW,
        delivery_status={"occurrences": 1},
    )
    session.add(alert)
    session.flush()

    log.info(
        "alert_created",
        alert_id=str(alert.id),
        event_id=str(event.id),
        event_type=event.event_type.value,
        severity=event.severity.value,
        is_inference=event.is_inference,
    )
    return alert


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _recipients(session: Session, alert: Alert, policy: ChannelPolicy) -> list[User]:
    """Users who should receive this alert on this channel.

    Store-restricted users only hear about their own stores: a manager of the
    Kariakoo branch should not be woken by an alert from Mwanza.
    """
    statement = select(User).where(
        User.organization_id == alert.organization_id,
        User.is_active.is_(True),
        User.deleted_at.is_(None),
    )
    if policy.roles:
        statement = statement.where(User.role.in_(list(policy.roles)))

    users = session.scalars(statement.options(selectinload(User.stores))).all()
    if alert.store_id is None:
        return list(users)
    return [
        user
        for user in users
        if not user.stores or any(store.id == alert.store_id for store in user.stores)
    ]


def _destination_for(user: User, channel: NotificationChannel) -> str | None:
    if channel in (NotificationChannel.EMAIL,):
        return user.email
    if channel in (NotificationChannel.SMS, NotificationChannel.WHATSAPP):
        return user.phone
    if channel in (NotificationChannel.DASHBOARD, NotificationChannel.WEBSOCKET):
        return str(user.id)
    return None


def deliver_alert(
    session: Session,
    alert: Alert,
    *,
    registry: NotifierRegistry | None = None,
    policies: list[ChannelPolicy] | None = None,
) -> list[Notification]:
    """Fan an alert out across the channels its severity qualifies for.

    One ``Notification`` row per user per channel, so a failure on SMS is
    visible and retriable without re-sending the email.
    """
    registry = registry or build_default_registry()
    policies = policies or DEFAULT_POLICIES
    created: list[Notification] = []
    delivery_status = dict(alert.delivery_status)

    for policy in policies:
        if not policy.accepts(alert.severity):
            continue

        notifier = registry.get(policy.channel)
        channel_results: list[str] = []

        for user in _recipients(session, alert, policy):
            destination = _destination_for(user, policy.channel)
            if not destination:
                # A user with no phone number simply does not get SMS. Not an
                # error, and not worth a failed notification row.
                continue

            message = NotificationMessage(
                subject=alert.title,
                body=alert.message,
                destination=destination,
                severity=alert.severity.value,
                alert_id=alert.id,
                organization_id=alert.organization_id,
                data={"alert_id": str(alert.id), "event_id": str(alert.event_id)},
            )
            result = notifier.send(message)

            notification = Notification(
                organization_id=alert.organization_id,
                user_id=user.id,
                alert_id=alert.id,
                channel=policy.channel,
                status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
                subject=alert.title[:200],
                body=alert.message,
                destination=destination[:400],
                sent_at=datetime.now(UTC) if result.success else None,
                attempts=1,
                error_message=result.error[:500] if result.error else None,
                provider_message_id=result.provider_message_id,
            )
            session.add(notification)
            created.append(notification)
            channel_results.append("SENT" if result.success else "FAILED")

        if channel_results:
            delivery_status[policy.channel.value] = (
                "SENT" if "SENT" in channel_results else "FAILED"
            )

    alert.delivery_status = delivery_status
    session.flush()
    log.info(
        "alert_delivered",
        alert_id=str(alert.id),
        notifications=len(created),
        channels=list(delivery_status),
    )
    return created


# ---------------------------------------------------------------------------
# Query and workflow
# ---------------------------------------------------------------------------


def list_alerts(
    session: Session,
    scope: TenantScope,
    *,
    status: AlertStatus | None = None,
    severity: EventSeverity | None = None,
    store_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Alert], int]:
    scope.require(Permission.ALERT_READ)

    statement = scope.apply(select(Alert), Alert)
    if status is not None:
        statement = statement.where(Alert.status == status)
    if severity is not None:
        statement = statement.where(Alert.severity == severity)
    if store_id is not None:
        scope.require_store(store_id)
        statement = statement.where(Alert.store_id == store_id)
    if since is not None:
        statement = statement.where(Alert.created_at >= since)

    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = session.scalars(
        statement.options(selectinload(Alert.event))
        .order_by(Alert.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total


def get_alert(session: Session, scope: TenantScope, alert_id: uuid.UUID) -> Alert:
    scope.require(Permission.ALERT_READ)
    alert = session.scalar(
        scope.apply(select(Alert), Alert)
        .where(Alert.id == alert_id)
        .options(selectinload(Alert.event))
    )
    if alert is None:
        raise NotFoundError("Alert not found.", details={"alert_id": str(alert_id)})
    return alert


def acknowledge_alert(
    session: Session, scope: TenantScope, alert_id: uuid.UUID, user: User
) -> Alert:
    """Claim an alert so nobody else duplicates the work."""
    scope.require(Permission.ALERT_ACKNOWLEDGE)
    alert = get_alert(session, scope, alert_id)

    if alert.status in (AlertStatus.RESOLVED, AlertStatus.DISMISSED):
        raise ConflictError("This alert has already been closed.")
    if alert.status == AlertStatus.ACKNOWLEDGED:
        return alert

    alert.status = AlertStatus.ACKNOWLEDGED
    alert.acknowledged_by_id = user.id
    alert.acknowledged_at = datetime.now(UTC)
    session.flush()

    record_audit(
        session,
        action=AuditAction.UPDATE,
        organization_id=alert.organization_id,
        user=user,
        resource_type="alert",
        resource_id=str(alert.id),
        description=f"Alert acknowledged: {alert.title}",
    )
    return alert


def resolve_alert(
    session: Session,
    scope: TenantScope,
    alert_id: uuid.UUID,
    user: User,
    *,
    notes: str | None = None,
    was_actionable: bool | None = None,
    dismiss: bool = False,
) -> Alert:
    """Close an alert.

    ``was_actionable`` is the feedback loop that makes the system improvable:
    it is the operator's verdict on whether the alert was worth raising, and
    it is the only ground truth available for measuring the false-positive
    rate. Without it, tuning thresholds is guesswork.
    """
    scope.require(Permission.ALERT_RESOLVE)
    alert = get_alert(session, scope, alert_id)

    alert.status = AlertStatus.DISMISSED if dismiss else AlertStatus.RESOLVED
    alert.resolved_by_id = user.id
    alert.resolved_at = datetime.now(UTC)
    if notes:
        alert.resolution_notes = notes
    if was_actionable is not None:
        alert.was_actionable = was_actionable
    session.flush()

    record_audit(
        session,
        action=AuditAction.UPDATE,
        organization_id=alert.organization_id,
        user=user,
        resource_type="alert",
        resource_id=str(alert.id),
        description=f"Alert {alert.status.value.lower()}: {alert.title}",
        changes={"was_actionable": was_actionable, "has_notes": bool(notes)},
    )
    log.info(
        "alert_closed",
        alert_id=str(alert.id),
        status=alert.status.value,
        was_actionable=was_actionable,
    )
    return alert


def alert_summary(session: Session, scope: TenantScope) -> dict:
    """Counts for the dashboard's alert tiles."""
    scope.require(Permission.ALERT_READ)

    by_status = {status.value: 0 for status in AlertStatus}
    statement = scope.apply(select(Alert.status, func.count()), Alert).group_by(Alert.status)
    for status, count in session.execute(statement):
        by_status[status.value] = int(count)

    by_severity = {severity.value: 0 for severity in EventSeverity}
    open_statement = scope.apply(
        select(Alert.severity, func.count()).where(
            Alert.status.in_([AlertStatus.NEW, AlertStatus.ACKNOWLEDGED])
        ),
        Alert,
    ).group_by(Alert.severity)
    for severity, count in session.execute(open_statement):
        by_severity[severity.value] = int(count)

    return {
        "by_status": by_status,
        "open_by_severity": by_severity,
        "open_total": by_status[AlertStatus.NEW.value] + by_status[AlertStatus.ACKNOWLEDGED.value],
        "high_priority_open": by_severity[EventSeverity.HIGH.value]
        + by_severity[EventSeverity.CRITICAL.value],
    }


def false_positive_rate(
    session: Session, scope: TenantScope, *, since: datetime | None = None
) -> dict:
    """How often operators judged an alert not worth raising.

    Only closed alerts with an explicit verdict count. Reporting a rate over
    unjudged alerts would be inventing data.
    """
    scope.require(Permission.ANALYTICS_READ)
    statement = scope.apply(
        select(Alert.was_actionable, func.count()).where(Alert.was_actionable.is_not(None)),
        Alert,
    )
    if since is not None:
        statement = statement.where(Alert.created_at >= since)

    counts = {True: 0, False: 0}
    for actionable, count in session.execute(statement.group_by(Alert.was_actionable)):
        counts[bool(actionable)] = int(count)

    judged = counts[True] + counts[False]
    return {
        "judged": judged,
        "actionable": counts[True],
        "not_actionable": counts[False],
        "false_positive_rate": (counts[False] / judged) if judged else None,
    }


def store_name_for(session: Session, store_id: uuid.UUID | None) -> str | None:
    if store_id is None:
        return None
    return session.scalar(select(Store.name).where(Store.id == store_id))
