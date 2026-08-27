"""Milestones 11-12 -- event persistence, the alert engine, and their API.

Two properties matter most here:

* An **inference never reads as a measurement**. It carries its confidence,
  its rule, and the observations behind it, all the way to the API response.
* **Alert fatigue is prevented**. Routine events do not alert, repeats group,
  and a stuck camera cannot bury every other camera's alerts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.core.tenancy import TenantScope
from app.models import Alert, Event
from app.models.enums import (
    AlertStatus,
    ConfidenceLevel,
    EventSeverity,
    EventType,
    NotificationChannel,
    UserRole,
)
from app.services import alerts as alert_service
from app.services import events as event_service
from tests import factories as f

PASSWORD = "tembo-anga-baridi-91"
BASE = datetime(2026, 5, 1, 14, 42, 0, tzinfo=UTC)


def admin_scope(user) -> TenantScope:
    from app.services.auth import build_scope

    return build_scope(user)


@pytest.fixture()
def world(db_session):
    """An organization with a store, a camera, a zone and an admin user."""
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org, name="Kariakoo Branch")
    camera = f.make_camera(db_session, store, name="Warehouse door")
    zone = f.make_zone(db_session, camera, name="Warehouse")
    user = f.make_user(
        db_session,
        org,
        email="admin@duka.co.tz",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.ORGANIZATION_ADMIN,
    )
    db_session.commit()
    return {
        "org": org,
        "store": store,
        "camera": camera,
        "zone": zone,
        "user": user,
        "scope": admin_scope(user),
    }


def vision_event(**overrides):
    """A VisionEvent from the engine, ready to persist."""
    from vision.events import EventType as VisionEventType
    from vision.events import Severity, VisionEvent

    defaults: dict = {
        "event_type": VisionEventType.RESTRICTED_AREA_ENTRY,
        "severity": Severity.HIGH,
        "camera_id": "cam-1",
        "occurred_at": BASE,
        "summary": "Person (track 42) entered zone 'Warehouse'.",
        "track_key": "cam-1:sess1:42",
        "track_id": 42,
        "object_class": "person",
        "confidence": 0.93,
        "rule_name": "zone.transition",
    }
    defaults.update(overrides)
    return VisionEvent(**defaults)


# ---------------------------------------------------------------- persistence
def test_persist_a_vision_event(db_session, world):
    stored = event_service.persist_vision_event(
        db_session,
        vision_event(camera_id=str(world["camera"].id), store_id=str(world["store"].id)),
        organization_id=world["org"].id,
    )
    assert stored is not None
    assert stored.event_type == EventType.RESTRICTED_AREA_ENTRY
    assert stored.severity == EventSeverity.HIGH
    assert stored.camera_id == world["camera"].id
    assert stored.track_id == 42
    assert stored.is_inference is False
    assert stored.dedupe_key


def test_replayed_events_are_deduplicated(db_session, world):
    """An edge box replaying its queue must not create duplicate incidents."""
    event = vision_event(camera_id=str(world["camera"].id), store_id=str(world["store"].id))
    first = event_service.persist_vision_event(db_session, event, organization_id=world["org"].id)
    second = event_service.persist_vision_event(db_session, event, organization_id=world["org"].id)
    assert first is not None
    assert second is None
    assert db_session.scalar(select(Event.id).where(Event.id == first.id)) is not None


def test_two_distinct_events_are_both_kept(db_session, world):
    """Deduplication must not swallow genuinely separate incidents."""
    for offset in (0, 60):
        event_service.persist_vision_event(
            db_session,
            vision_event(
                camera_id=str(world["camera"].id),
                occurred_at=BASE + timedelta(seconds=offset),
            ),
            organization_id=world["org"].id,
        )
    assert db_session.scalar(select(Event).where(Event.track_id == 42)) is not None
    assert len(db_session.scalars(select(Event)).all()) == 2


def test_different_tracks_are_distinct_events(db_session, world):
    for track in (42, 43):
        event_service.persist_vision_event(
            db_session,
            vision_event(
                camera_id=str(world["camera"].id),
                track_key=f"cam-1:sess1:{track}",
                track_id=track,
            ),
            organization_id=world["org"].id,
        )
    assert len(db_session.scalars(select(Event)).all()) == 2


def test_unknown_vocabulary_is_rejected_not_guessed(db_session, world):
    """A version mismatch must surface, not be silently coerced."""

    class Fake:
        value = "SOME_FUTURE_EVENT"

    event = vision_event(camera_id=str(world["camera"].id))
    event.event_type = Fake()  # type: ignore[assignment]
    assert (
        event_service.persist_vision_event(db_session, event, organization_id=world["org"].id)
        is None
    )


def test_inference_fields_survive_persistence(db_session, world):
    from vision.events import ConfidenceLevel as VisionConfidence
    from vision.events import EventType as VisionEventType

    observation = event_service.persist_vision_event(
        db_session,
        vision_event(
            camera_id=str(world["camera"].id),
            event_type=VisionEventType.PRODUCT_REMOVED,
        ),
        organization_id=world["org"].id,
    )
    inference = event_service.persist_vision_event(
        db_session,
        vision_event(
            camera_id=str(world["camera"].id),
            event_type=VisionEventType.POSSIBLE_INVENTORY_DISCREPANCY,
            confidence_level=VisionConfidence.MEDIUM,
            is_inference=True,
            rule_name="pos.unmatched_removal",
            source_event_ids=[str(observation.id)],
            occurred_at=BASE + timedelta(seconds=5),
        ),
        organization_id=world["org"].id,
    )
    assert inference.is_inference is True
    assert inference.confidence_level == ConfidenceLevel.MEDIUM
    assert inference.rule_name == "pos.unmatched_removal"
    assert str(observation.id) in inference.source_event_ids


def test_source_events_are_retrievable(db_session, world):
    from vision.events import EventType as VisionEventType

    observation = event_service.persist_vision_event(
        db_session,
        vision_event(camera_id=str(world["camera"].id), event_type=VisionEventType.PRODUCT_REMOVED),
        organization_id=world["org"].id,
    )
    inference = event_service.persist_vision_event(
        db_session,
        vision_event(
            camera_id=str(world["camera"].id),
            event_type=VisionEventType.POSSIBLE_INVENTORY_DISCREPANCY,
            source_event_ids=[str(observation.id)],
            occurred_at=BASE + timedelta(seconds=5),
        ),
        organization_id=world["org"].id,
    )
    sources = event_service.get_source_events(db_session, world["scope"], inference)
    assert [s.id for s in sources] == [observation.id]


# ---------------------------------------------------------------- alert policy
def test_routine_events_do_not_alert(db_session, world):
    """A busy shop produces thousands of line crossings a day."""
    for event_type in (
        EventType.LINE_CROSSED,
        EventType.PERSON_DETECTED,
        EventType.PERSON_EXITED_ZONE,
    ):
        event = f.make_event(
            db_session, world["camera"], event_type=event_type, severity=EventSeverity.HIGH
        )
        assert alert_service.should_alert(event) is False


def test_low_severity_events_do_not_alert(db_session, world):
    for severity in (EventSeverity.INFO, EventSeverity.LOW):
        event = f.make_event(
            db_session,
            world["camera"],
            event_type=EventType.RESTRICTED_AREA_ENTRY,
            severity=severity,
        )
        assert alert_service.should_alert(event) is False


def test_restricted_area_entry_alerts(db_session, world):
    event = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        severity=EventSeverity.HIGH,
    )
    alert = alert_service.create_alert_for_event(db_session, event, store_name="Kariakoo")
    assert alert is not None
    assert alert.severity == EventSeverity.HIGH
    assert alert.status == AlertStatus.NEW
    assert "Kariakoo" in alert.title


def test_repeats_group_into_one_alert(db_session, world):
    """Twenty minutes of loitering is one incident, not forty alerts."""
    first = None
    for index in range(6):
        event = f.make_event(
            db_session,
            world["camera"],
            event_type=EventType.LOITERING,
            severity=EventSeverity.MEDIUM,
            zone_id=world["zone"].id,
            occurred_at=BASE + timedelta(seconds=index * 30),
        )
        alert = alert_service.create_alert_for_event(db_session, event)
        first = first or alert

    alerts = db_session.scalars(select(Alert)).all()
    assert len(alerts) == 1
    assert alerts[0].delivery_status["occurrences"] == 6


def test_grouping_escalates_severity(db_session, world):
    medium = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.LOITERING,
        severity=EventSeverity.MEDIUM,
        zone_id=world["zone"].id,
        occurred_at=BASE,
    )
    alert_service.create_alert_for_event(db_session, medium)

    critical = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.LOITERING,
        severity=EventSeverity.CRITICAL,
        zone_id=world["zone"].id,
        occurred_at=BASE + timedelta(seconds=30),
    )
    alert_service.create_alert_for_event(db_session, critical)

    alert = db_session.scalars(select(Alert)).one()
    assert alert.severity == EventSeverity.CRITICAL


def test_a_stuck_camera_cannot_bury_the_alert_queue(db_session, world):
    """Rate limiting is what stops one faulty camera hiding a real incident."""
    created = 0
    for index in range(40):
        event = f.make_event(
            db_session,
            world["camera"],
            event_type=EventType.RESTRICTED_AREA_ENTRY,
            severity=EventSeverity.HIGH,
            # Distinct zones defeat grouping, isolating the rate limit.
            zone_id=None,
            track_key=f"cam:sess:{index}",
            occurred_at=BASE + timedelta(minutes=index),
        )
        if alert_service.create_alert_for_event(db_session, event) is not None:
            created += 1
    assert created <= alert_service.MAX_ALERTS_PER_CAMERA_PER_HOUR


def test_inference_alerts_are_worded_as_something_to_check(db_session, world):
    """An inference must never be presented as a finding."""
    event = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.POSSIBLE_INVENTORY_DISCREPANCY,
        severity=EventSeverity.MEDIUM,
        is_inference=True,
        confidence_level=ConfidenceLevel.LOW,
        rule_name="pos.unmatched_removal",
        source_event_ids=[str(uuid.uuid4())],
        summary="A shelf change had no matching sale within 120 seconds.",
    )
    alert = alert_service.create_alert_for_event(db_session, event)

    assert "review" in alert.title.lower()
    assert "not a finding of wrongdoing" in alert.message
    assert "low confidence" in alert.message
    for word in ("theft", "stole", "stealing", "thief"):
        assert word not in alert.message.lower()
        assert word not in alert.title.lower()


# ------------------------------------------------------------------- delivery
def test_delivery_creates_one_notification_per_channel(db_session, world):
    world["user"].phone = "+255700000001"
    db_session.flush()

    event = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        severity=EventSeverity.HIGH,
    )
    alert = alert_service.create_alert_for_event(db_session, event)
    notifications = alert_service.deliver_alert(db_session, alert)

    channels = {n.channel for n in notifications}
    assert NotificationChannel.DASHBOARD in channels
    assert NotificationChannel.WEBSOCKET in channels
    # HIGH severity reaches SMS/WhatsApp for an org admin? Admin is not in the
    # SMS role set, so only the always-on channels plus email apply.
    assert NotificationChannel.EMAIL in channels


def test_medium_severity_does_not_reach_sms(db_session, world):
    """SMS costs money and interrupts someone's evening."""
    world["user"].phone = "+255700000001"
    world["user"].role = UserRole.SECURITY_OPERATOR
    db_session.flush()

    event = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.LOITERING,
        severity=EventSeverity.MEDIUM,
    )
    alert = alert_service.create_alert_for_event(db_session, event)
    notifications = alert_service.deliver_alert(db_session, alert)
    assert NotificationChannel.SMS not in {n.channel for n in notifications}


def test_high_severity_reaches_sms_for_an_operator(db_session, world):
    world["user"].phone = "+255700000001"
    world["user"].role = UserRole.SECURITY_OPERATOR
    db_session.flush()

    event = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.AFTER_HOURS_ACTIVITY,
        severity=EventSeverity.HIGH,
    )
    alert = alert_service.create_alert_for_event(db_session, event)
    notifications = alert_service.deliver_alert(db_session, alert)
    assert NotificationChannel.SMS in {n.channel for n in notifications}


def test_a_user_without_a_phone_gets_no_sms_row(db_session, world):
    """Not an error: a user with no number simply is not an SMS recipient."""
    world["user"].role = UserRole.SECURITY_OPERATOR
    world["user"].phone = None
    db_session.flush()

    event = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.AFTER_HOURS_ACTIVITY,
        severity=EventSeverity.HIGH,
    )
    alert = alert_service.create_alert_for_event(db_session, event)
    notifications = alert_service.deliver_alert(db_session, alert)
    sms = [n for n in notifications if n.channel == NotificationChannel.SMS]
    assert sms == []


def test_local_delivery_is_marked_as_local(db_session, world):
    """A recorded message must never look like a delivered one."""
    event = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        severity=EventSeverity.HIGH,
    )
    alert = alert_service.create_alert_for_event(db_session, event)
    notifications = alert_service.deliver_alert(db_session, alert)
    assert all(n.provider_message_id.startswith("local:") for n in notifications)


def test_store_restricted_users_only_hear_about_their_stores(db_session, world):
    """A Kariakoo manager should not be woken by an alert from Mwanza."""
    other_store = f.make_store(db_session, world["org"], name="Mwanza")
    manager = f.make_user(
        db_session,
        world["org"],
        email="mwanza@duka.co.tz",
        role=UserRole.STORE_MANAGER,
    )
    manager.stores.append(other_store)
    db_session.flush()

    event = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        severity=EventSeverity.HIGH,
    )
    alert = alert_service.create_alert_for_event(db_session, event)
    notifications = alert_service.deliver_alert(db_session, alert)
    assert manager.id not in {n.user_id for n in notifications}


# ------------------------------------------------------------------ workflow
def test_acknowledge_then_resolve(db_session, world):
    event = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        severity=EventSeverity.HIGH,
    )
    alert = alert_service.create_alert_for_event(db_session, event)

    alert_service.acknowledge_alert(db_session, world["scope"], alert.id, world["user"])
    assert alert.status == AlertStatus.ACKNOWLEDGED
    assert alert.acknowledged_by_id == world["user"].id

    alert_service.resolve_alert(
        db_session,
        world["scope"],
        alert.id,
        world["user"],
        notes="Staff member restocking after hours; expected.",
        was_actionable=False,
    )
    assert alert.status == AlertStatus.RESOLVED
    assert alert.was_actionable is False
    assert alert.resolution_notes


def test_acknowledging_a_closed_alert_is_rejected(db_session, world):
    from app.core.errors import ConflictError

    event = f.make_event(
        db_session,
        world["camera"],
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        severity=EventSeverity.HIGH,
    )
    alert = alert_service.create_alert_for_event(db_session, event)
    alert_service.resolve_alert(db_session, world["scope"], alert.id, world["user"])

    with pytest.raises(ConflictError):
        alert_service.acknowledge_alert(db_session, world["scope"], alert.id, world["user"])


def test_false_positive_rate_counts_only_judged_alerts(db_session, world):
    """Reporting a rate over unjudged alerts would be inventing a number."""
    for index, verdict in enumerate([True, False, False, None]):
        event = f.make_event(
            db_session,
            world["camera"],
            event_type=EventType.RESTRICTED_AREA_ENTRY,
            severity=EventSeverity.HIGH,
            track_key=f"cam:sess:{index}",
            occurred_at=BASE + timedelta(minutes=index * 20),
        )
        alert = alert_service.create_alert_for_event(db_session, event)
        if alert is not None:
            alert_service.resolve_alert(
                db_session, world["scope"], alert.id, world["user"], was_actionable=verdict
            )

    report = alert_service.false_positive_rate(db_session, world["scope"])
    assert report["judged"] == 3
    assert report["not_actionable"] == 2
    assert report["false_positive_rate"] == pytest.approx(2 / 3)


def test_false_positive_rate_is_none_when_nothing_is_judged(db_session, world):
    report = alert_service.false_positive_rate(db_session, world["scope"])
    assert report["judged"] == 0
    assert report["false_positive_rate"] is None


# ----------------------------------------------------------------------- API
@pytest.fixture()
def api(client, db_session, world):
    tokens = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@duka.co.tz", "password": PASSWORD},
    ).json()["tokens"]
    return {
        "headers": {"Authorization": f"Bearer {tokens['access_token']}"},
        **world,
    }


def test_event_timeline_endpoint(client, db_session, api):
    for index in range(3):
        f.make_event(
            db_session,
            api["camera"],
            event_type=EventType.RESTRICTED_AREA_ENTRY,
            severity=EventSeverity.HIGH,
            occurred_at=BASE + timedelta(minutes=index),
        )
    db_session.commit()

    body = client.get("/api/v1/events", headers=api["headers"]).json()
    assert body["total"] == 3
    # Newest first.
    times = [item["occurred_at"] for item in body["items"]]
    assert times == sorted(times, reverse=True)


def test_event_response_marks_observations_and_inferences(client, db_session, api):
    f.make_event(
        db_session,
        api["camera"],
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        severity=EventSeverity.HIGH,
    )
    f.make_event(
        db_session,
        api["camera"],
        event_type=EventType.POSSIBLE_INVENTORY_DISCREPANCY,
        severity=EventSeverity.MEDIUM,
        is_inference=True,
        confidence_level=ConfidenceLevel.LOW,
        occurred_at=BASE + timedelta(minutes=1),
    )
    db_session.commit()

    items = client.get("/api/v1/events", headers=api["headers"]).json()["items"]
    by_type = {item["event_type"]: item for item in items}
    assert by_type["RESTRICTED_AREA_ENTRY"]["is_inference"] is False
    assert by_type["POSSIBLE_INVENTORY_DISCREPANCY"]["is_inference"] is True
    assert by_type["POSSIBLE_INVENTORY_DISCREPANCY"]["confidence_level"] == "LOW"


def test_event_detail_exposes_the_kind_discriminator(client, db_session, api):
    event = f.make_event(
        db_session,
        api["camera"],
        event_type=EventType.POSSIBLE_INVENTORY_DISCREPANCY,
        severity=EventSeverity.MEDIUM,
        is_inference=True,
        confidence_level=ConfidenceLevel.MEDIUM,
    )
    db_session.commit()

    body = client.get(f"/api/v1/events/{event.id}", headers=api["headers"]).json()
    assert body["kind"] == "inference"
    assert body["is_inference"] is True


def test_event_detail_returns_the_supporting_observations(client, db_session, api):
    observation = f.make_event(db_session, api["camera"], event_type=EventType.PRODUCT_REMOVED)
    inference = f.make_event(
        db_session,
        api["camera"],
        event_type=EventType.POSSIBLE_INVENTORY_DISCREPANCY,
        severity=EventSeverity.MEDIUM,
        is_inference=True,
        source_event_ids=[str(observation.id)],
        occurred_at=BASE + timedelta(minutes=1),
    )
    db_session.commit()

    body = client.get(f"/api/v1/events/{inference.id}", headers=api["headers"]).json()
    assert len(body["source_events"]) == 1
    assert body["source_events"][0]["event_type"] == "PRODUCT_REMOVED"


def test_timeline_filters(client, db_session, api):
    f.make_event(db_session, api["camera"], event_type=EventType.LINE_CROSSED)
    f.make_event(
        db_session,
        api["camera"],
        event_type=EventType.LOITERING,
        severity=EventSeverity.MEDIUM,
        track_key="cam:sess:42",
        occurred_at=BASE + timedelta(minutes=1),
    )
    db_session.commit()

    filtered = client.get("/api/v1/events?event_type=LOITERING", headers=api["headers"]).json()
    assert filtered["total"] == 1

    by_severity = client.get("/api/v1/events?severity=MEDIUM", headers=api["headers"]).json()
    assert by_severity["total"] == 1

    by_track = client.get("/api/v1/events?track_key=cam:sess:42", headers=api["headers"]).json()
    assert by_track["total"] == 1


def test_timeline_filters_by_inference(client, db_session, api):
    f.make_event(db_session, api["camera"], event_type=EventType.LINE_CROSSED)
    f.make_event(
        db_session,
        api["camera"],
        event_type=EventType.POSSIBLE_INVENTORY_DISCREPANCY,
        is_inference=True,
        occurred_at=BASE + timedelta(minutes=1),
    )
    db_session.commit()

    assert (
        client.get("/api/v1/events?is_inference=true", headers=api["headers"]).json()["total"] == 1
    )
    assert (
        client.get("/api/v1/events?is_inference=false", headers=api["headers"]).json()["total"] == 1
    )


def test_event_types_catalogue(client, api):
    catalogue = client.get("/api/v1/events/types", headers=api["headers"]).json()
    inference_types = {e["value"] for e in catalogue if e["is_inference"]}
    assert "POSSIBLE_INVENTORY_DISCREPANCY" in inference_types
    assert "LINE_CROSSED" not in inference_types


def test_alerts_endpoint_and_workflow(client, db_session, api):
    event = f.make_event(
        db_session,
        api["camera"],
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        severity=EventSeverity.HIGH,
    )
    alert = alert_service.create_alert_for_event(db_session, event)
    db_session.commit()

    listing = client.get("/api/v1/alerts", headers=api["headers"]).json()
    assert listing["total"] == 1
    assert listing["items"][0]["occurrences"] == 1

    acknowledged = client.post(
        f"/api/v1/alerts/{alert.id}/acknowledge", headers=api["headers"]
    ).json()
    assert acknowledged["status"] == "ACKNOWLEDGED"

    resolved = client.post(
        f"/api/v1/alerts/{alert.id}/resolve",
        json={"notes": "Cleaner on site", "was_actionable": False},
        headers=api["headers"],
    ).json()
    assert resolved["status"] == "RESOLVED"
    assert resolved["was_actionable"] is False


def test_alert_summary_endpoint(client, db_session, api):
    for index in range(2):
        event = f.make_event(
            db_session,
            api["camera"],
            event_type=EventType.RESTRICTED_AREA_ENTRY,
            severity=EventSeverity.HIGH,
            track_key=f"cam:sess:{index}",
            zone_id=None,
            occurred_at=BASE + timedelta(minutes=index * 20),
        )
        alert_service.create_alert_for_event(db_session, event)
    db_session.commit()

    body = client.get("/api/v1/alerts/summary", headers=api["headers"]).json()
    assert body["open_total"] == 2
    assert body["high_priority_open"] == 2


def test_events_are_isolated_between_tenants(client, db_session, api):
    other_org = f.make_organization(db_session)
    other_camera = f.make_camera(db_session, f.make_store(db_session, other_org))
    f.make_event(db_session, other_camera, summary="Not yours")
    f.make_event(db_session, api["camera"], summary="Yours")
    db_session.commit()

    items = client.get("/api/v1/events", headers=api["headers"]).json()["items"]
    assert [item["summary"] for item in items] == ["Yours"]


def test_event_endpoints_require_authentication(client):
    assert client.get("/api/v1/events").status_code == 401
    assert client.get("/api/v1/alerts").status_code == 401


def test_analyst_cannot_acknowledge_alerts(client, db_session, api):
    """Analysts read; they do not work the alert queue."""
    f.make_user(
        db_session,
        api["org"],
        email="analyst@duka.co.tz",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.ANALYST,
    )
    event = f.make_event(
        db_session,
        api["camera"],
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        severity=EventSeverity.HIGH,
    )
    alert = alert_service.create_alert_for_event(db_session, event)
    db_session.commit()

    tokens = client.post(
        "/api/v1/auth/login",
        json={"email": "analyst@duka.co.tz", "password": PASSWORD},
    ).json()["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = client.post(f"/api/v1/alerts/{alert.id}/acknowledge", headers=headers)
    assert response.status_code == 403


# --------------------------------------------------------- end-to-end ingest
def test_ingest_persists_and_escalates(db_session, world):
    """The single entry point a pipeline sink uses."""
    event, alert = event_service.ingest_vision_event(
        db_session,
        vision_event(camera_id=str(world["camera"].id), store_id=str(world["store"].id)),
        organization_id=world["org"].id,
    )
    assert event is not None
    assert alert is not None
    assert alert.event_id == event.id


def test_ingesting_a_routine_event_creates_no_alert(db_session, world):
    from vision.events import EventType as VisionEventType
    from vision.events import Severity

    event, alert = event_service.ingest_vision_event(
        db_session,
        vision_event(
            camera_id=str(world["camera"].id),
            event_type=VisionEventType.LINE_CROSSED,
            severity=Severity.INFO,
        ),
        organization_id=world["org"].id,
    )
    assert event is not None
    assert alert is None


def test_retention_prunes_old_events(db_session, world):
    f.make_event(db_session, world["camera"], occurred_at=datetime.now(UTC) - timedelta(days=200))
    f.make_event(db_session, world["camera"], occurred_at=datetime.now(UTC))
    db_session.flush()

    deleted = event_service.prune_old_events(
        db_session, organization_id=world["org"].id, retention_days=90
    )
    assert deleted == 1
    assert len(db_session.scalars(select(Event)).all()) == 1


def test_vision_and_backend_event_vocabularies_agree():
    """A drift here would silently drop events at the ingest boundary."""
    from vision.events import EventType as VisionEventType
    from vision.events import Severity as VisionSeverity

    assert {e.value for e in VisionEventType} == {e.value for e in EventType}
    assert {s.value for s in VisionSeverity} == {s.value for s in EventSeverity}


def test_vision_and_backend_agree_on_which_types_are_inferences():
    from app.models.enums import INFERENCE_EVENT_TYPES as BACKEND_INFERENCES
    from vision.events import INFERENCE_EVENT_TYPES as VISION_INFERENCES

    assert {e.value for e in VISION_INFERENCES} == {e.value for e in BACKEND_INFERENCES}
