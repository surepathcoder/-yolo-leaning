"""Deterministic object factories for tests.

Every factory takes a session, persists the object and returns it.  Values are
realistic rather than minimal, so a test that reads a field gets something
sensible instead of an empty string.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.models import (
    Alert,
    Camera,
    CountingLine,
    DetectionEvent,
    Event,
    InventoryEvent,
    InventorySnapshot,
    Organization,
    POSItem,
    POSTransaction,
    Product,
    Shelf,
    Store,
    StoreOperatingHours,
    Subscription,
    TrackingObject,
    User,
    Zone,
    ZoneEvent,
)
from app.models.enums import (
    AlertStatus,
    CameraStatus,
    EventSeverity,
    EventType,
    InventoryEventType,
    OccupancyLevel,
    PlanTier,
    UserRole,
    VideoSourceKind,
    ZoneEventType,
    ZoneType,
)

_counter = 0


def _next() -> int:
    global _counter
    _counter += 1
    return _counter


def make_organization(session: Session, **kwargs) -> Organization:
    n = _next()
    org = Organization(
        name=kwargs.pop("name", f"Test Retail {n}"),
        slug=kwargs.pop("slug", f"test-retail-{n}"),
        contact_email=kwargs.pop("contact_email", f"ops{n}@example.co.tz"),
        country=kwargs.pop("country", "TZ"),
        timezone=kwargs.pop("timezone", "Africa/Dar_es_Salaam"),
        currency=kwargs.pop("currency", "TZS"),
        **kwargs,
    )
    session.add(org)
    session.flush()
    return org


def make_subscription(session: Session, organization: Organization, **kwargs) -> Subscription:
    sub = Subscription(
        organization_id=organization.id,
        tier=kwargs.pop("tier", PlanTier.BUSINESS),
        **kwargs,
    )
    session.add(sub)
    session.flush()
    return sub


def make_user(session: Session, organization: Organization | None = None, **kwargs) -> User:
    n = _next()
    user = User(
        organization_id=organization.id if organization else None,
        email=kwargs.pop("email", f"user{n}@example.co.tz"),
        hashed_password=kwargs.pop("hashed_password", "$argon2id$placeholder"),
        full_name=kwargs.pop("full_name", f"Test User {n}"),
        role=kwargs.pop("role", UserRole.VIEWER),
        **kwargs,
    )
    session.add(user)
    session.flush()
    return user


def make_store(session: Session, organization: Organization, **kwargs) -> Store:
    n = _next()
    store = Store(
        organization_id=organization.id,
        name=kwargs.pop("name", f"Store {n}"),
        code=kwargs.pop("code", f"ST{n:03d}"),
        city=kwargs.pop("city", "Dar es Salaam"),
        country=kwargs.pop("country", "TZ"),
        timezone=kwargs.pop("timezone", "Africa/Dar_es_Salaam"),
        **kwargs,
    )
    session.add(store)
    session.flush()
    return store


def make_operating_hours(
    session: Session,
    store: Store,
    *,
    opens: time = time(8, 0),
    closes: time = time(22, 0),
    days: range | list[int] = range(7),
) -> list[StoreOperatingHours]:
    rows = [
        StoreOperatingHours(store_id=store.id, day_of_week=day, opens_at=opens, closes_at=closes)
        for day in days
    ]
    session.add_all(rows)
    session.flush()
    return rows


def make_camera(session: Session, store: Store, **kwargs) -> Camera:
    n = _next()
    camera = Camera(
        organization_id=store.organization_id,
        store_id=store.id,
        name=kwargs.pop("name", f"Camera {n}"),
        source_kind=kwargs.pop("source_kind", VideoSourceKind.RTSP),
        host=kwargs.pop("host", f"192.168.1.{100 + (n % 100)}"),
        port=kwargs.pop("port", 554),
        stream_path=kwargs.pop("stream_path", "/stream1"),
        username=kwargs.pop("username", "admin"),
        status=kwargs.pop("status", CameraStatus.ONLINE),
        target_fps=kwargs.pop("target_fps", 10.0),
        **kwargs,
    )
    session.add(camera)
    session.flush()
    return camera


def make_zone(session: Session, camera: Camera, **kwargs) -> Zone:
    n = _next()
    zone = Zone(
        organization_id=camera.organization_id,
        store_id=camera.store_id,
        camera_id=camera.id,
        name=kwargs.pop("name", f"Zone {n}"),
        zone_type=kwargs.pop("zone_type", ZoneType.RESTRICTED),
        polygon=kwargs.pop("polygon", [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]),
        **kwargs,
    )
    session.add(zone)
    session.flush()
    return zone


def make_counting_line(session: Session, camera: Camera, **kwargs) -> CountingLine:
    n = _next()
    line = CountingLine(
        organization_id=camera.organization_id,
        store_id=camera.store_id,
        camera_id=camera.id,
        name=kwargs.pop("name", f"Entrance {n}"),
        point_a=kwargs.pop("point_a", [0.0, 0.5]),
        point_b=kwargs.pop("point_b", [1.0, 0.5]),
        **kwargs,
    )
    session.add(line)
    session.flush()
    return line


def make_track(session: Session, camera: Camera, **kwargs) -> TrackingObject:
    n = _next()
    now = kwargs.pop("first_seen_at", datetime.now(UTC))
    track = TrackingObject(
        organization_id=camera.organization_id,
        store_id=camera.store_id,
        camera_id=camera.id,
        track_key=kwargs.pop("track_key", f"{camera.id}:sess1:{n}"),
        track_id=kwargs.pop("track_id", n),
        session_id=kwargs.pop("session_id", "sess1"),
        class_name=kwargs.pop("class_name", "person"),
        first_seen_at=now,
        last_seen_at=kwargs.pop("last_seen_at", now + timedelta(seconds=30)),
        **kwargs,
    )
    session.add(track)
    session.flush()
    return track


def make_detection(session: Session, camera: Camera, **kwargs) -> DetectionEvent:
    detection = DetectionEvent(
        organization_id=camera.organization_id,
        store_id=camera.store_id,
        camera_id=camera.id,
        class_id=kwargs.pop("class_id", 0),
        class_name=kwargs.pop("class_name", "person"),
        confidence=kwargs.pop("confidence", 0.91),
        bbox_x1=kwargs.pop("bbox_x1", 0.2),
        bbox_y1=kwargs.pop("bbox_y1", 0.3),
        bbox_x2=kwargs.pop("bbox_x2", 0.35),
        bbox_y2=kwargs.pop("bbox_y2", 0.8),
        detected_at=kwargs.pop("detected_at", datetime.now(UTC)),
        **kwargs,
    )
    session.add(detection)
    session.flush()
    return detection


def make_zone_event(session: Session, zone: Zone, **kwargs) -> ZoneEvent:
    n = _next()
    event = ZoneEvent(
        organization_id=zone.organization_id,
        store_id=zone.store_id,
        camera_id=zone.camera_id,
        zone_id=zone.id,
        event_type=kwargs.pop("event_type", ZoneEventType.ENTERED),
        track_key=kwargs.pop("track_key", f"{zone.camera_id}:sess1:{n}"),
        track_id=kwargs.pop("track_id", n),
        occurred_at=kwargs.pop("occurred_at", datetime.now(UTC)),
        **kwargs,
    )
    session.add(event)
    session.flush()
    return event


def make_event(session: Session, camera: Camera, **kwargs) -> Event:
    event = Event(
        organization_id=camera.organization_id,
        store_id=camera.store_id,
        camera_id=camera.id,
        event_type=kwargs.pop("event_type", EventType.PERSON_ENTERED_ZONE),
        severity=kwargs.pop("severity", EventSeverity.INFO),
        occurred_at=kwargs.pop("occurred_at", datetime.now(UTC)),
        summary=kwargs.pop("summary", "Person entered zone."),
        **kwargs,
    )
    session.add(event)
    session.flush()
    return event


def make_alert(session: Session, event: Event, **kwargs) -> Alert:
    alert = Alert(
        organization_id=event.organization_id,
        store_id=event.store_id,
        event_id=event.id,
        title=kwargs.pop("title", "Restricted area entry"),
        message=kwargs.pop("message", event.summary),
        severity=kwargs.pop("severity", EventSeverity.HIGH),
        status=kwargs.pop("status", AlertStatus.NEW),
        **kwargs,
    )
    session.add(alert)
    session.flush()
    return alert


def make_product(session: Session, organization: Organization, **kwargs) -> Product:
    n = _next()
    product = Product(
        organization_id=organization.id,
        sku=kwargs.pop("sku", f"SKU-{n:05d}"),
        name=kwargs.pop("name", f"Product {n}"),
        unit_price=kwargs.pop("unit_price", 1500),
        currency=kwargs.pop("currency", "TZS"),
        **kwargs,
    )
    session.add(product)
    session.flush()
    return product


def make_shelf(session: Session, store: Store, camera: Camera | None = None, **kwargs) -> Shelf:
    n = _next()
    shelf = Shelf(
        organization_id=store.organization_id,
        store_id=store.id,
        camera_id=camera.id if camera else None,
        name=kwargs.pop("name", f"Shelf {n}"),
        code=kwargs.pop("code", f"SH{n:03d}"),
        polygon=kwargs.pop("polygon", [[0.2, 0.2], [0.8, 0.2], [0.8, 0.7], [0.2, 0.7]]),
        **kwargs,
    )
    session.add(shelf)
    session.flush()
    return shelf


def make_snapshot(session: Session, shelf: Shelf, **kwargs) -> InventorySnapshot:
    snapshot = InventorySnapshot(
        organization_id=shelf.organization_id,
        store_id=shelf.store_id,
        shelf_id=shelf.id,
        camera_id=shelf.camera_id,
        occupancy_ratio=kwargs.pop("occupancy_ratio", 0.8),
        occupancy_level=kwargs.pop("occupancy_level", OccupancyLevel.NORMAL),
        confidence=kwargs.pop("confidence", 0.75),
        captured_at=kwargs.pop("captured_at", datetime.now(UTC)),
        **kwargs,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def make_inventory_event(session: Session, shelf: Shelf, **kwargs) -> InventoryEvent:
    event = InventoryEvent(
        organization_id=shelf.organization_id,
        store_id=shelf.store_id,
        shelf_id=shelf.id,
        camera_id=shelf.camera_id,
        event_type=kwargs.pop("event_type", InventoryEventType.PRODUCT_REMOVED),
        occurred_at=kwargs.pop("occurred_at", datetime.now(UTC)),
        confidence=kwargs.pop("confidence", 0.7),
        **kwargs,
    )
    session.add(event)
    session.flush()
    return event


def make_pos_transaction(session: Session, store: Store, **kwargs) -> POSTransaction:
    n = _next()
    tx = POSTransaction(
        organization_id=store.organization_id,
        store_id=store.id,
        provider=kwargs.pop("provider", "mock"),
        external_id=kwargs.pop("external_id", f"TX-{uuid.uuid4().hex[:10]}-{n}"),
        transaction_at=kwargs.pop("transaction_at", datetime.now(UTC)),
        total_amount=kwargs.pop("total_amount", 3000),
        currency=kwargs.pop("currency", "TZS"),
        **kwargs,
    )
    session.add(tx)
    session.flush()
    return tx


def make_pos_item(
    session: Session, transaction: POSTransaction, product: Product | None = None, **kwargs
) -> POSItem:
    item = POSItem(
        organization_id=transaction.organization_id,
        transaction_id=transaction.id,
        product_id=product.id if product else None,
        name=kwargs.pop("name", product.name if product else "Unknown item"),
        quantity=kwargs.pop("quantity", 1),
        unit_price=kwargs.pop("unit_price", 1500),
        **kwargs,
    )
    session.add(item)
    session.flush()
    return item
