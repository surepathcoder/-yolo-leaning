"""Milestone 1 -- database models and constraints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    Camera,
    Event,
    Store,
    User,
)
from app.models.enums import (
    INFERENCE_EVENT_TYPES,
    SEVERITY_ORDER,
    CameraStatus,
    ConfidenceLevel,
    EventSeverity,
    EventType,
    OccupancyLevel,
)
from tests import factories as f


# ------------------------------------------------------------------ basics
def test_uuid_primary_keys_are_generated(db_session):
    org = f.make_organization(db_session)
    assert isinstance(org.id, uuid.UUID)
    assert org.id.version == 4


def test_timestamps_are_populated(db_session):
    org = f.make_organization(db_session)
    assert org.created_at is not None
    assert org.updated_at is not None


def test_soft_delete_marker(db_session):
    org = f.make_organization(db_session)
    assert org.is_deleted is False
    org.deleted_at = datetime.now(UTC)
    db_session.flush()
    assert org.is_deleted is True


# ------------------------------------------------------------- constraints
def test_organization_slug_is_globally_unique(db_session):
    f.make_organization(db_session, slug="dupe-slug")
    with pytest.raises(IntegrityError):
        f.make_organization(db_session, slug="dupe-slug")


def test_user_email_is_unique_per_organization(db_session):
    org = f.make_organization(db_session)
    f.make_user(db_session, org, email="same@example.co.tz")
    with pytest.raises(IntegrityError):
        f.make_user(db_session, org, email="same@example.co.tz")


def test_same_email_may_exist_in_two_organizations(db_session):
    """One person can legitimately operate two unrelated businesses."""
    org_a = f.make_organization(db_session)
    org_b = f.make_organization(db_session)
    f.make_user(db_session, org_a, email="owner@example.co.tz")
    f.make_user(db_session, org_b, email="owner@example.co.tz")
    users = db_session.scalars(select(User).where(User.email == "owner@example.co.tz")).all()
    assert len(users) == 2
    assert {u.organization_id for u in users} == {org_a.id, org_b.id}


def test_store_code_is_unique_per_organization(db_session):
    org = f.make_organization(db_session)
    f.make_store(db_session, org, code="DSM-01")
    with pytest.raises(IntegrityError):
        f.make_store(db_session, org, code="DSM-01")


def test_camera_name_is_unique_per_store(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    f.make_camera(db_session, store, name="Entrance")
    with pytest.raises(IntegrityError):
        f.make_camera(db_session, store, name="Entrance")


def test_operating_hours_unique_per_day(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    f.make_operating_hours(db_session, store, days=[0])
    with pytest.raises(IntegrityError):
        f.make_operating_hours(db_session, store, days=[0])


def test_pos_transaction_external_id_is_unique_per_provider(db_session):
    """Re-importing a CSV must not duplicate a sale and mask a discrepancy."""
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    f.make_pos_transaction(db_session, store, provider="mock", external_id="TX-1")
    with pytest.raises(IntegrityError):
        f.make_pos_transaction(db_session, store, provider="mock", external_id="TX-1")


def test_alert_is_one_per_event(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    event = f.make_event(db_session, camera)
    f.make_alert(db_session, event)
    with pytest.raises(IntegrityError):
        f.make_alert(db_session, event)


# ------------------------------------------------------------ relationships
def test_organization_cascade_graph(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    f.make_zone(db_session, camera)

    db_session.refresh(org)
    assert len(org.stores) == 1
    assert org.stores[0].cameras[0].id == camera.id
    assert len(camera.zones) == 1


def test_store_operating_hours_relationship(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    f.make_operating_hours(db_session, store, opens=time(9, 0), closes=time(21, 0))
    db_session.refresh(store)
    assert len(store.operating_hours) == 7
    assert store.operating_hours[0].opens_at == time(9, 0)


def test_pos_transaction_items_relationship(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    product = f.make_product(db_session, org)
    tx = f.make_pos_transaction(db_session, store)
    f.make_pos_item(db_session, tx, product, quantity=2)
    db_session.refresh(tx)
    assert len(tx.items) == 1
    assert tx.items[0].product_id == product.id


def test_event_evidence_and_alert_relationship(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    event = f.make_event(db_session, camera, severity=EventSeverity.HIGH)
    alert = f.make_alert(db_session, event)
    db_session.refresh(event)
    assert event.alert is not None
    assert event.alert.id == alert.id


# ------------------------------------------------ observation vs inference
def test_events_default_to_observations(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    event = f.make_event(db_session, camera)
    assert event.is_inference is False
    assert event.source_event_ids == []


def test_inference_event_records_its_sources(db_session):
    """An inference must be auditable back to the observations behind it."""
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)

    observation = f.make_event(db_session, camera, event_type=EventType.PRODUCT_REMOVED)
    inference = f.make_event(
        db_session,
        camera,
        event_type=EventType.POSSIBLE_INVENTORY_DISCREPANCY,
        is_inference=True,
        confidence_level=ConfidenceLevel.MEDIUM,
        rule_name="pos.unmatched_removal",
        source_event_ids=[str(observation.id)],
        severity=EventSeverity.MEDIUM,
    )

    assert inference.is_inference is True
    assert str(observation.id) in inference.source_event_ids
    assert inference.rule_name == "pos.unmatched_removal"
    assert inference.confidence_level == ConfidenceLevel.MEDIUM


def test_inference_event_types_are_hedged():
    """Inference names must not assert wrongdoing."""
    for event_type in INFERENCE_EVENT_TYPES:
        assert event_type.value.startswith(("POSSIBLE_", "POTENTIAL_"))

    forbidden = {"THEFT", "STEAL", "SHOPLIFT", "CRIMINAL", "GUILTY"}
    for event_type in EventType:
        assert not any(word in event_type.value for word in forbidden), event_type


def test_inference_events_are_queryable_separately(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    f.make_event(db_session, camera)
    f.make_event(db_session, camera, is_inference=True)

    observations = db_session.scalars(
        select(Event).where(Event.organization_id == org.id, Event.is_inference.is_(False))
    ).all()
    inferences = db_session.scalars(
        select(Event).where(Event.organization_id == org.id, Event.is_inference.is_(True))
    ).all()
    assert len(observations) == 1
    assert len(inferences) == 1


# -------------------------------------------------------------- enum values
def test_severity_ordering_is_total():
    ordered = sorted(EventSeverity, key=lambda s: SEVERITY_ORDER[s])
    assert ordered == [
        EventSeverity.INFO,
        EventSeverity.LOW,
        EventSeverity.MEDIUM,
        EventSeverity.HIGH,
        EventSeverity.CRITICAL,
    ]
    assert len(SEVERITY_ORDER) == len(EventSeverity)


def test_enum_columns_store_values_not_names(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store, status=CameraStatus.ERROR)
    db_session.flush()
    raw = db_session.execute(select(Camera.status).where(Camera.id == camera.id)).scalar_one()
    assert raw == CameraStatus.ERROR
    assert CameraStatus.ERROR.value == "ERROR"


def test_occupancy_level_includes_unknown():
    """Shelf occupancy must be able to say "I don't know"."""
    assert OccupancyLevel.UNKNOWN in set(OccupancyLevel)


# ------------------------------------------------------- computed properties
def test_camera_is_operational(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store, status=CameraStatus.ONLINE)
    assert camera.is_operational is True

    camera.status = CameraStatus.ERROR
    assert camera.is_operational is False

    camera.status = CameraStatus.ONLINE
    camera.is_enabled = False
    assert camera.is_operational is False


def test_track_duration(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    start = datetime.now(UTC)
    track = f.make_track(
        db_session, camera, first_seen_at=start, last_seen_at=start + timedelta(seconds=84)
    )
    assert track.duration_seconds == pytest.approx(84.0)


def test_detection_bbox_property(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    detection = f.make_detection(db_session, camera)
    assert detection.bbox == (0.2, 0.3, 0.35, 0.8)


# ----------------------------------------------------------------- security
def test_camera_password_is_not_stored_in_plain_text_field(db_session):
    """The model must have no plaintext password column at all."""
    columns = {c.name for c in Camera.__table__.columns}
    assert "password" not in columns
    assert "encrypted_password" in columns
    # The connection URL must not be a stored column either -- credentials
    # would end up embedded in it.
    assert "rtsp_url" not in columns


def test_user_password_column_is_a_hash(db_session):
    columns = {c.name for c in User.__table__.columns}
    assert "password" not in columns
    assert "hashed_password" in columns


def test_audit_log_has_no_updated_at():
    """An audit row that can be edited is not an audit row."""
    from app.models import AuditLog

    columns = {c.name for c in AuditLog.__table__.columns}
    assert "updated_at" not in columns
    assert "deleted_at" not in columns


# ------------------------------------------------------------------ indexes
def test_tenant_scoped_tables_index_organization_id():
    """Every tenant table must have organization_id indexed."""
    from app.models import (
        Alert as _Alert,
    )
    from app.models import (
        DetectionEvent,
        InventoryEvent,
        POSTransaction,
        Zone,
        ZoneEvent,
    )

    tenant_models = [
        Store,
        Camera,
        Zone,
        Event,
        _Alert,
        DetectionEvent,
        ZoneEvent,
        InventoryEvent,
        POSTransaction,
    ]
    for model in tenant_models:
        column = model.__table__.columns["organization_id"]
        indexed = column.index or any(
            "organization_id" in idx.columns for idx in model.__table__.indexes
        )
        assert indexed, f"{model.__name__}.organization_id is not indexed"


def test_events_table_has_time_ordered_indexes():
    index_names = {idx.name for idx in Event.__table__.indexes}
    assert "ix_events_org_occurred" in index_names
    assert "ix_events_org_type_occurred" in index_names
    assert "ix_events_org_severity_occurred" in index_names


def test_all_models_have_uuid_primary_keys():
    import app.models as models_module
    from app.db.base import GUID
    from app.models import __all__ as exported

    for name in exported:
        obj = getattr(models_module, name)
        table = getattr(obj, "__table__", None)
        if table is None:
            continue  # association tables are exported as Table objects
        pk_columns = list(table.primary_key.columns)
        assert pk_columns, f"{name} has no primary key"
        if len(pk_columns) == 1:
            assert isinstance(pk_columns[0].type, GUID), f"{name}.id is not a GUID"


def test_all_tenant_tables_declare_organization_id():
    """A tenant-owned table without organization_id cannot be safely scoped."""

    exempt = {
        "organizations",  # is the tenant
        "permissions",  # global catalogue
        "roles",  # nullable org (system roles)
        "users",  # nullable org (SUPER_ADMIN)
        "refresh_tokens",  # scoped through the user
        "camera_streams",  # scoped through the camera
        "store_operating_hours",  # scoped through the store
        "pos_items",  # has organization_id but via transaction too
        "audit_logs",  # nullable org (pre-auth failures)
        "system_settings",  # nullable org (platform defaults)
        "model_versions",  # nullable org (shared pretrained models)
        "subscriptions",  # keyed by organization_id directly
        "role_permissions",
        "user_stores",
        "shelf_products",
    }
    from app.db.base import Base

    missing = [
        name
        for name, table in Base.metadata.tables.items()
        if name not in exempt and "organization_id" not in table.columns
    ]
    assert missing == [], f"tables missing organization_id: {missing}"
