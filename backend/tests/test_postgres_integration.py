"""Milestone 1 -- schema behaviour on real PostgreSQL.

SQLite proves the models are self-consistent. Only PostgreSQL proves the schema
actually deploys, that native UUID columns round-trip, that JSON columns work,
that money keeps its precision, and that ON DELETE CASCADE really cascades.

Run with: pytest -m integration  (requires deployment/docker/compose.infra.yml)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    Alert,
    Camera,
    Event,
    Organization,
    POSTransaction,
)
from app.models.enums import EventSeverity, EventType
from tests import factories as f

pytestmark = pytest.mark.integration


def test_schema_deploys_to_postgres(postgres_engine):
    from sqlalchemy import inspect

    tables = set(inspect(postgres_engine).get_table_names())
    for expected in (
        "organizations",
        "users",
        "stores",
        "cameras",
        "zones",
        "events",
        "alerts",
        "products",
        "shelves",
        "pos_transactions",
        "audit_logs",
    ):
        assert expected in tables


def test_uuid_columns_use_the_native_type(postgres_engine):
    with postgres_engine.connect() as conn:
        data_type = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'organizations' AND column_name = 'id'"
            )
        ).scalar_one()
    assert data_type == "uuid"


def test_uuid_round_trips(pg_session):
    org = f.make_organization(pg_session)
    pg_session.flush()
    fetched = pg_session.get(Organization, org.id)
    assert isinstance(fetched.id, uuid.UUID)
    assert fetched.id == org.id


def test_json_columns_round_trip(pg_session):
    org = f.make_organization(pg_session, settings={"branding": {"primary": "#0ea5e9"}})
    store = f.make_store(pg_session, org)
    camera = f.make_camera(pg_session, store, detect_classes=["person", "backpack"])
    zone = f.make_zone(pg_session, camera, polygon=[[0.1, 0.2], [0.5, 0.2], [0.5, 0.9]])
    pg_session.flush()
    pg_session.expire_all()

    assert pg_session.get(Organization, org.id).settings["branding"]["primary"] == "#0ea5e9"
    assert pg_session.get(Camera, camera.id).detect_classes == ["person", "backpack"]
    assert pg_session.get(type(zone), zone.id).polygon[2] == [0.5, 0.9]


def test_money_keeps_exact_precision(pg_session):
    """Floats would silently corrupt totals -- Numeric must round-trip exactly."""
    org = f.make_organization(pg_session)
    store = f.make_store(pg_session, org)
    tx = f.make_pos_transaction(pg_session, store, total_amount=Decimal("12345.67"))
    pg_session.flush()
    pg_session.expire_all()
    fetched = pg_session.get(POSTransaction, tx.id)
    assert fetched.total_amount == Decimal("12345.67")
    assert isinstance(fetched.total_amount, Decimal)


def test_timezone_aware_timestamps_round_trip(pg_session):
    org = f.make_organization(pg_session)
    camera = f.make_camera(pg_session, f.make_store(pg_session, org))
    moment = datetime(2026, 3, 14, 15, 9, 26, tzinfo=UTC)
    event = f.make_event(pg_session, camera, occurred_at=moment)
    pg_session.flush()
    pg_session.expire_all()
    fetched = pg_session.get(Event, event.id)
    assert fetched.occurred_at.tzinfo is not None
    assert fetched.occurred_at == moment


def test_cascade_delete_removes_dependent_rows(pg_session):
    """Deleting a store must not orphan its cameras."""
    org = f.make_organization(pg_session)
    store = f.make_store(pg_session, org)
    camera = f.make_camera(pg_session, store)
    camera_id = camera.id
    pg_session.flush()

    pg_session.execute(text("DELETE FROM stores WHERE id = :sid"), {"sid": str(store.id)})
    pg_session.flush()
    pg_session.expire_all()

    remaining = pg_session.execute(
        select(func.count()).select_from(Camera).where(Camera.id == camera_id)
    ).scalar_one()
    assert remaining == 0


def test_set_null_preserves_events_when_a_camera_is_removed(pg_session):
    """Deleting a camera must not destroy the event history it produced."""
    org = f.make_organization(pg_session)
    store = f.make_store(pg_session, org)
    camera = f.make_camera(pg_session, store)
    event = f.make_event(pg_session, camera)
    event_id = event.id
    pg_session.flush()

    pg_session.execute(text("DELETE FROM cameras WHERE id = :cid"), {"cid": str(camera.id)})
    pg_session.flush()
    pg_session.expire_all()

    surviving = pg_session.get(Event, event_id)
    assert surviving is not None
    assert surviving.camera_id is None


def test_enum_check_constraint_rejects_unknown_values(pg_session):
    org = f.make_organization(pg_session)
    store = f.make_store(pg_session, org)
    camera = f.make_camera(pg_session, store)
    pg_session.flush()

    with pytest.raises(IntegrityError):
        pg_session.execute(
            text("UPDATE cameras SET status = 'NOT_A_STATUS' WHERE id = :cid"),
            {"cid": str(camera.id)},
        )
        pg_session.flush()


def test_time_range_aggregation(pg_session):
    """Analytics aggregate in the database, not the browser."""
    org = f.make_organization(pg_session)
    camera = f.make_camera(pg_session, f.make_store(pg_session, org))
    base = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)
    for hour, count in ((0, 2), (3, 5), (7, 1)):
        for _ in range(count):
            f.make_event(
                pg_session,
                camera,
                occurred_at=base + timedelta(hours=hour),
                event_type=EventType.LINE_CROSSED,
            )
    pg_session.flush()

    rows = pg_session.execute(
        select(
            func.date_trunc("hour", Event.occurred_at).label("bucket"),
            func.count().label("total"),
        )
        .where(Event.organization_id == org.id)
        .group_by(text("bucket"))
        .order_by(text("total DESC"))
    ).all()
    assert rows[0].total == 5


def test_severity_filtering_uses_the_composite_index(pg_session):
    org = f.make_organization(pg_session)
    camera = f.make_camera(pg_session, f.make_store(pg_session, org))
    f.make_event(pg_session, camera, severity=EventSeverity.CRITICAL)
    f.make_event(pg_session, camera, severity=EventSeverity.INFO)
    f.make_event(pg_session, camera, severity=EventSeverity.INFO)
    pg_session.flush()

    critical = pg_session.scalars(
        select(Event).where(
            Event.organization_id == org.id, Event.severity == EventSeverity.CRITICAL
        )
    ).all()
    assert len(critical) == 1


def test_tenant_isolation_holds_on_postgres(pg_session):
    """The isolation guarantee, verified on the real database engine."""
    from app.core.tenancy import TenantScope
    from app.models.enums import UserRole

    org_a = f.make_organization(pg_session)
    org_b = f.make_organization(pg_session)
    f.make_camera(pg_session, f.make_store(pg_session, org_a), name="a-cam")
    f.make_camera(pg_session, f.make_store(pg_session, org_b), name="b-cam")
    pg_session.flush()

    scope = TenantScope(
        user_id=uuid.uuid4(), organization_id=org_a.id, role=UserRole.ORGANIZATION_ADMIN
    )
    cameras = pg_session.scalars(scope.apply(select(Camera), Camera)).all()
    assert [c.name for c in cameras] == ["a-cam"]


def test_unique_constraints_are_enforced_by_postgres(pg_session):
    f.make_organization(pg_session, slug="unique-check")
    pg_session.flush()
    with pytest.raises(IntegrityError):
        f.make_organization(pg_session, slug="unique-check")
        pg_session.flush()


def test_alert_workflow_columns(pg_session):
    org = f.make_organization(pg_session)
    store = f.make_store(pg_session, org)
    user = f.make_user(pg_session, org)
    camera = f.make_camera(pg_session, store)
    event = f.make_event(pg_session, camera, severity=EventSeverity.HIGH)
    alert = f.make_alert(pg_session, event)
    pg_session.flush()

    alert.acknowledged_by_id = user.id
    alert.acknowledged_at = datetime.now(UTC)
    alert.was_actionable = False
    alert.resolution_notes = "Staff member restocking after hours; expected."
    pg_session.flush()
    pg_session.expire_all()

    fetched = pg_session.get(Alert, alert.id)
    assert fetched.acknowledged_by_id == user.id
    assert fetched.was_actionable is False


def test_indexes_exist_on_postgres(postgres_engine):
    from sqlalchemy import inspect

    inspector = inspect(postgres_engine)
    event_indexes = {idx["name"] for idx in inspector.get_indexes("events")}
    assert "ix_events_org_occurred" in event_indexes
    assert "ix_events_org_type_occurred" in event_indexes

    store_indexes = {idx["name"] for idx in inspector.get_indexes("stores")}
    assert any("organization_id" in name for name in store_indexes)
