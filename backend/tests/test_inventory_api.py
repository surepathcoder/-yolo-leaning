"""Milestone 14 -- products, shelves and inventory history.

Two properties get the most attention here, because they are what stop this
feature from being confidently wrong.

*Occlusion is honoured end to end.* A reading taken while the shelf was
blocked is stored, but never becomes the shelf's status and never counts
towards "empty". The status endpoint must say UNKNOWN, not zero.

*Tenants cannot see each other's stock.* A competitor's shelf levels are
commercially sensitive in exactly the way that ends the product if leaked, so
every listing and every by-id read is checked against a second organization.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.enums import PlanTier

PASSWORD = "tembo-anga-baridi-91"
SHELF_POLY = [[0.2, 0.4], [0.8, 0.4], [0.8, 0.6], [0.2, 0.6]]
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def register(client, db_session, *, email: str, org: str) -> dict:
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": "Shop Owner",
            "organization_name": org,
        },
    ).json()
    headers = {"Authorization": f"Bearer {registered['tokens']['access_token']}"}

    from app.models import Subscription

    subscription = db_session.scalar(
        select(Subscription).where(
            Subscription.organization_id == uuid.UUID(registered["organization_id"])
        )
    )
    subscription.tier = PlanTier.BUSINESS
    db_session.commit()

    store = client.post(
        "/api/v1/stores",
        json={"name": "Kariakoo", "code": "DSM-01", "timezone": "Africa/Dar_es_Salaam"},
        headers=headers,
    ).json()
    camera = client.post(
        "/api/v1/cameras",
        json={"store_id": store["id"], "name": "Aisle 3", "source_kind": "SYNTHETIC"},
        headers=headers,
    ).json()
    return {
        "headers": headers,
        "store": store,
        "camera": camera,
        "organization_id": registered["organization_id"],
    }


@pytest.fixture()
def ctx(client, db_session):
    return register(client, db_session, email="owner@duka.co.tz", org="Duka la Amina")


@pytest.fixture()
def other(client, db_session):
    return register(client, db_session, email="rival@shop.co.tz", org="Rival Shop")


def make_shelf(client, ctx, **overrides) -> dict:
    payload = {
        "store_id": ctx["store"]["id"],
        "camera_id": ctx["camera"]["id"],
        "name": "Aisle 3 top",
        "code": "A3-T",
        "polygon": SHELF_POLY,
    }
    payload.update(overrides)
    response = client.post("/api/v1/shelves", json=payload, headers=ctx["headers"])
    assert response.status_code == 201, response.text
    return response.json()


def post_snapshot(client, ctx, shelf_id, **overrides) -> dict:
    payload = {
        "occupancy_ratio": 0.9,
        "occupancy_level": "FULL",
        "confidence": 0.95,
        "is_occluded": False,
        "captured_at": NOW.isoformat(),
    }
    payload.update(overrides)
    response = client.post(
        f"/api/v1/shelves/{shelf_id}/snapshots", json=payload, headers=ctx["headers"]
    )
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ products


def test_create_and_list_a_product(client, ctx):
    created = client.post(
        "/api/v1/products",
        json={"sku": "AZAM-500", "name": "Azam Cola 500ml", "unit_price": "1200.00"},
        headers=ctx["headers"],
    )
    assert created.status_code == 201, created.text
    assert created.json()["sku"] == "AZAM-500"

    listed = client.get("/api/v1/products", headers=ctx["headers"]).json()
    assert [p["sku"] for p in listed] == ["AZAM-500"]


def test_duplicate_sku_is_rejected_with_a_clear_message(client, ctx):
    body = {"sku": "AZAM-500", "name": "Azam Cola"}
    client.post("/api/v1/products", json=body, headers=ctx["headers"])
    clash = client.post("/api/v1/products", json=body, headers=ctx["headers"])
    assert clash.status_code == 409
    assert "AZAM-500" in clash.json()["error"]["message"]


def test_the_same_sku_is_free_in_another_organization(client, ctx, other):
    body = {"sku": "AZAM-500", "name": "Azam Cola"}
    assert client.post("/api/v1/products", json=body, headers=ctx["headers"]).status_code == 201
    assert client.post("/api/v1/products", json=body, headers=other["headers"]).status_code == 201


def test_products_are_searchable(client, ctx):
    for sku, name in [("AZAM-500", "Azam Cola 500ml"), ("KIL-500", "Kilimanjaro Water")]:
        client.post("/api/v1/products", json={"sku": sku, "name": name}, headers=ctx["headers"])
    found = client.get("/api/v1/products?search=Kilimanjaro", headers=ctx["headers"]).json()
    assert [p["sku"] for p in found] == ["KIL-500"]


def test_deleting_a_product_is_soft(client, ctx, db_session):
    from app.models import Product

    product = client.post(
        "/api/v1/products", json={"sku": "X-1", "name": "Thing"}, headers=ctx["headers"]
    ).json()
    assert (
        client.delete(f"/api/v1/products/{product['id']}", headers=ctx["headers"]).status_code
        == 200
    )
    assert client.get("/api/v1/products", headers=ctx["headers"]).json() == []
    # The events that referenced it still point at a real row.
    assert db_session.get(Product, uuid.UUID(product["id"])) is not None


def test_products_are_isolated_between_tenants(client, ctx, other):
    product = client.post(
        "/api/v1/products", json={"sku": "X-1", "name": "Thing"}, headers=ctx["headers"]
    ).json()
    assert client.get("/api/v1/products", headers=other["headers"]).json() == []
    assert (
        client.get(f"/api/v1/products/{product['id']}", headers=other["headers"]).status_code == 404
    )


# ------------------------------------------------------------------- shelves


def test_create_and_read_a_shelf(client, ctx):
    shelf = make_shelf(client, ctx)
    assert shelf["code"] == "A3-T"
    assert shelf["polygon"] == SHELF_POLY
    assert shelf["monitoring_enabled"] is True

    fetched = client.get(f"/api/v1/shelves/{shelf['id']}", headers=ctx["headers"]).json()
    assert fetched["id"] == shelf["id"]


def test_a_shelf_can_be_planned_before_its_camera_exists(client, ctx):
    shelf = make_shelf(client, ctx, camera_id=None, code="A4-T")
    assert shelf["camera_id"] is None


def test_shelf_polygon_is_validated_with_the_engine_rules(client, ctx):
    """A shape the engine would reject must not be storable."""
    too_small = [[0.50, 0.50], [0.51, 0.50], [0.51, 0.51], [0.50, 0.51]]
    response = client.post(
        "/api/v1/shelves",
        json={
            "store_id": ctx["store"]["id"],
            "name": "Tiny",
            "code": "TINY",
            "polygon": too_small,
        },
        headers=ctx["headers"],
    )
    assert response.status_code == 422
    assert "too small" in response.json()["error"]["message"].lower()


def test_thresholds_must_stay_ordered(client, ctx):
    response = client.post(
        "/api/v1/shelves",
        json={
            "store_id": ctx["store"]["id"],
            "name": "Backwards",
            "code": "BW",
            "polygon": SHELF_POLY,
            "low_threshold": 0.1,
            "empty_threshold": 0.5,
        },
        headers=ctx["headers"],
    )
    assert response.status_code == 422


def test_duplicate_shelf_code_in_one_store_is_rejected(client, ctx):
    make_shelf(client, ctx)
    clash = client.post(
        "/api/v1/shelves",
        json={
            "store_id": ctx["store"]["id"],
            "name": "Another",
            "code": "A3-T",
            "polygon": SHELF_POLY,
        },
        headers=ctx["headers"],
    )
    assert clash.status_code == 409


def test_a_camera_from_another_store_is_refused(client, ctx, db_session):
    second_store = client.post(
        "/api/v1/stores",
        json={"name": "Mwanza", "code": "MWZ-01", "timezone": "Africa/Dar_es_Salaam"},
        headers=ctx["headers"],
    ).json()
    response = client.post(
        "/api/v1/shelves",
        json={
            "store_id": second_store["id"],
            "camera_id": ctx["camera"]["id"],  # belongs to the Kariakoo store
            "name": "Wrong store",
            "code": "WS-1",
            "polygon": SHELF_POLY,
        },
        headers=ctx["headers"],
    )
    assert response.status_code == 422
    assert "different store" in response.json()["error"]["message"]


def test_update_and_delete_a_shelf(client, ctx, db_session):
    from app.models import Shelf

    shelf = make_shelf(client, ctx)
    updated = client.patch(
        f"/api/v1/shelves/{shelf['id']}",
        json={"name": "Aisle 3 middle", "low_threshold": 0.4},
        headers=ctx["headers"],
    ).json()
    assert updated["name"] == "Aisle 3 middle"
    assert updated["low_threshold"] == 0.4

    assert (
        client.delete(f"/api/v1/shelves/{shelf['id']}", headers=ctx["headers"]).status_code == 200
    )
    assert client.get("/api/v1/shelves", headers=ctx["headers"]).json() == []
    # Soft-deleted: its snapshots and events still reference it.
    assert db_session.get(Shelf, uuid.UUID(shelf["id"])) is not None


def test_shelf_products_can_be_attached(client, ctx):
    product = client.post(
        "/api/v1/products", json={"sku": "AZAM-500", "name": "Azam Cola"}, headers=ctx["headers"]
    ).json()
    shelf = make_shelf(client, ctx, product_ids=[product["id"]])
    assert [p["sku"] for p in shelf["products"]] == ["AZAM-500"]


def test_a_shelf_cannot_borrow_another_tenants_product(client, ctx, other):
    theirs = client.post(
        "/api/v1/products", json={"sku": "THEIRS", "name": "Theirs"}, headers=other["headers"]
    ).json()
    response = client.post(
        "/api/v1/shelves",
        json={
            "store_id": ctx["store"]["id"],
            "name": "Borrowed",
            "code": "B-1",
            "polygon": SHELF_POLY,
            "product_ids": [theirs["id"]],
        },
        headers=ctx["headers"],
    )
    assert response.status_code == 404


def test_shelves_are_isolated_between_tenants(client, ctx, other):
    shelf = make_shelf(client, ctx)
    assert client.get("/api/v1/shelves", headers=other["headers"]).json() == []
    assert client.get(f"/api/v1/shelves/{shelf['id']}", headers=other["headers"]).status_code == 404


# ------------------------------------------------------ readings and status


def test_a_reading_sets_the_shelf_status(client, ctx):
    shelf = make_shelf(client, ctx)
    post_snapshot(client, ctx, shelf["id"], occupancy_ratio=0.9, occupancy_level="FULL")

    status = client.get("/api/v1/inventory/status", headers=ctx["headers"]).json()
    assert len(status) == 1
    assert status[0]["occupancy_level"] == "FULL"
    assert status[0]["occupancy_ratio"] == 0.9
    assert status[0]["last_reading_at"] is not None


def test_a_shelf_never_read_reports_unknown_not_empty(client, ctx):
    """A new shelf is not an empty shelf."""
    make_shelf(client, ctx)
    status = client.get("/api/v1/inventory/status", headers=ctx["headers"]).json()
    assert status[0]["occupancy_level"] == "UNKNOWN"
    assert status[0]["occupancy_ratio"] is None
    assert status[0]["last_reading_at"] is None


def test_an_occluded_reading_never_becomes_the_status(client, ctx):
    """The behaviour the whole feature's credibility rests on."""
    shelf = make_shelf(client, ctx)
    post_snapshot(client, ctx, shelf["id"], occupancy_ratio=0.9, occupancy_level="FULL")
    post_snapshot(
        client,
        ctx,
        shelf["id"],
        occupancy_ratio=0.0,
        occupancy_level="UNKNOWN",
        is_occluded=True,
        captured_at=(NOW + timedelta(minutes=5)).isoformat(),
    )

    status = client.get("/api/v1/inventory/status", headers=ctx["headers"]).json()[0]
    # The last time we could actually see the shelf, it was full.
    assert status["occupancy_level"] == "FULL"
    assert status["occupancy_ratio"] == 0.9
    # But the operator is told the view is currently blocked.
    assert status["is_occluded"] is True


def test_a_shelf_seen_only_while_blocked_reports_unknown(client, ctx):
    shelf = make_shelf(client, ctx)
    post_snapshot(
        client, ctx, shelf["id"], occupancy_ratio=0.0, occupancy_level="UNKNOWN", is_occluded=True
    )
    status = client.get("/api/v1/inventory/status", headers=ctx["headers"]).json()[0]
    assert status["occupancy_level"] == "UNKNOWN"
    assert status["occupancy_ratio"] is None


def test_occluded_readings_are_still_stored(client, ctx):
    """They are a true record of what the camera saw."""
    shelf = make_shelf(client, ctx)
    post_snapshot(
        client, ctx, shelf["id"], occupancy_ratio=0.0, occupancy_level="UNKNOWN", is_occluded=True
    )
    page = client.get("/api/v1/inventory/snapshots", headers=ctx["headers"]).json()
    assert page["total"] == 1
    assert page["items"][0]["is_occluded"] is True


def test_a_reading_keeps_the_time_it_was_captured(client, ctx):
    """Edge pipelines post late; the event belongs to when it happened."""
    shelf = make_shelf(client, ctx)
    captured = NOW - timedelta(hours=6)
    post_snapshot(client, ctx, shelf["id"], captured_at=captured.isoformat())
    page = client.get("/api/v1/inventory/snapshots", headers=ctx["headers"]).json()
    assert page["items"][0]["captured_at"].startswith("2026-01-01T06:00")


def test_snapshots_are_paginated_newest_first(client, ctx):
    shelf = make_shelf(client, ctx)
    for i in range(3):
        post_snapshot(
            client,
            ctx,
            shelf["id"],
            occupancy_ratio=i / 10,
            occupancy_level="LOW",
            captured_at=(NOW + timedelta(minutes=i)).isoformat(),
        )
    page = client.get("/api/v1/inventory/snapshots?limit=2", headers=ctx["headers"]).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["items"][0]["occupancy_ratio"] == 0.2  # newest first


def test_snapshot_ratio_is_bounded(client, ctx):
    shelf = make_shelf(client, ctx)
    response = client.post(
        f"/api/v1/shelves/{shelf['id']}/snapshots",
        json={
            "occupancy_ratio": 1.5,
            "occupancy_level": "FULL",
            "captured_at": NOW.isoformat(),
        },
        headers=ctx["headers"],
    )
    assert response.status_code == 422


def test_readings_are_isolated_between_tenants(client, ctx, other):
    shelf = make_shelf(client, ctx)
    post_snapshot(client, ctx, shelf["id"])
    assert client.get("/api/v1/inventory/snapshots", headers=other["headers"]).json()["total"] == 0
    assert (
        client.post(
            f"/api/v1/shelves/{shelf['id']}/snapshots",
            json={
                "occupancy_ratio": 0.1,
                "occupancy_level": "LOW",
                "captured_at": NOW.isoformat(),
            },
            headers=other["headers"],
        ).status_code
        == 404
    )


# -------------------------------------------------------------------- events


def test_inventory_events_are_recorded_and_listed(client, ctx, db_session):
    from app.models import User
    from app.models.enums import InventoryEventType
    from app.services import inventory as service
    from app.services.auth import build_scope

    shelf = make_shelf(client, ctx)
    me = client.get("/api/v1/auth/me", headers=ctx["headers"]).json()
    scope = build_scope(db_session.get(User, uuid.UUID(me["id"])))

    service.record_event(
        db_session,
        scope,
        uuid.UUID(shelf["id"]),
        event_type=InventoryEventType.PRODUCT_REMOVED,
        occupancy_before=0.9,
        occupancy_after=0.4,
        confidence=0.8,
        track_key="cam:sess:7",
    )
    db_session.commit()

    page = client.get("/api/v1/inventory/events", headers=ctx["headers"]).json()
    assert page["total"] == 1
    event = page["items"][0]
    assert event["event_type"] == "PRODUCT_REMOVED"
    assert event["occupancy_before"] == 0.9
    assert event["track_key"] == "cam:sess:7"


# ------------------------------------------------------------------- summary


def test_summary_counts_shelves_by_level(client, ctx):
    empty_shelf = make_shelf(client, ctx, code="E-1")
    low_shelf = make_shelf(client, ctx, code="L-1")
    make_shelf(client, ctx, code="U-1")  # never read

    post_snapshot(client, ctx, empty_shelf["id"], occupancy_ratio=0.02, occupancy_level="EMPTY")
    post_snapshot(client, ctx, low_shelf["id"], occupancy_ratio=0.3, occupancy_level="LOW")

    summary = client.get("/api/v1/inventory/summary", headers=ctx["headers"]).json()
    assert summary["shelves_total"] == 3
    assert summary["shelves_empty"] == 1
    assert summary["shelves_low"] == 1
    assert summary["shelves_unknown"] == 1


def test_summary_reports_unseen_shelves_rather_than_hiding_them(client, ctx):
    """'We cannot see 1 of your shelves' is information the operator needs."""
    shelf = make_shelf(client, ctx)
    post_snapshot(
        client, ctx, shelf["id"], occupancy_ratio=0.0, occupancy_level="UNKNOWN", is_occluded=True
    )
    summary = client.get("/api/v1/inventory/summary", headers=ctx["headers"]).json()
    assert summary["shelves_unknown"] == 1
    assert summary["shelves_empty"] == 0


def test_summary_is_scoped_to_the_caller(client, ctx, other):
    make_shelf(client, ctx)
    summary = client.get("/api/v1/inventory/summary", headers=other["headers"]).json()
    assert summary["shelves_total"] == 0


# --------------------------------------------------------------- permissions


def test_a_viewer_cannot_configure_shelves(client, ctx, db_session):
    from app.core.security import hash_password
    from app.models import Organization
    from app.models.enums import UserRole
    from tests import factories as f

    org = db_session.get(Organization, uuid.UUID(ctx["organization_id"]))
    f.make_user(
        db_session,
        org,
        email="viewer@duka.co.tz",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.VIEWER,
    )
    db_session.commit()

    tokens = client.post(
        "/api/v1/auth/login",
        json={"email": "viewer@duka.co.tz", "password": PASSWORD},
    ).json()["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    denied = client.post(
        "/api/v1/shelves",
        json={
            "store_id": ctx["store"]["id"],
            "name": "Nope",
            "code": "N-1",
            "polygon": SHELF_POLY,
        },
        headers=headers,
    )
    assert denied.status_code == 403
    # But they can still read stock levels.
    assert client.get("/api/v1/inventory/status", headers=headers).status_code == 200


def test_unauthenticated_access_is_refused(client):
    assert client.get("/api/v1/shelves").status_code == 401
    assert client.get("/api/v1/inventory/summary").status_code == 401


# ------------------------------------------------------- pipeline integration


def test_shelves_reach_the_pipeline_config(client, ctx, db_session):
    """The join between the two layers.

    A shelf row must become a ShelfDefinition the vision engine can actually
    measure. If this breaks, shelves are configurable but never read, and
    nothing anywhere would report an error.
    """
    from app.models import Camera
    from app.services.pipeline_manager import build_pipeline_config

    shelf = make_shelf(client, ctx)
    camera = db_session.get(Camera, uuid.UUID(ctx["camera"]["id"]))
    config = build_pipeline_config(db_session, camera)

    assert [s.shelf_id for s in config.shelves] == [shelf["id"]]
    definition = config.shelves[0]
    assert definition.name == "Aisle 3 top"
    assert definition.polygon == [(0.2, 0.4), (0.8, 0.4), (0.8, 0.6), (0.2, 0.6)]
    assert definition.low_threshold == 0.35
    assert definition.is_monitored


def test_a_shelf_with_no_camera_is_not_in_any_pipeline(client, ctx, db_session):
    from app.models import Camera
    from app.services.pipeline_manager import build_pipeline_config

    make_shelf(client, ctx, camera_id=None, code="NC-1")
    camera = db_session.get(Camera, uuid.UUID(ctx["camera"]["id"]))
    assert build_pipeline_config(db_session, camera).shelves == []


def test_a_deleted_shelf_leaves_the_pipeline_config(client, ctx, db_session):
    from app.models import Camera
    from app.services.pipeline_manager import build_pipeline_config

    shelf = make_shelf(client, ctx)
    client.delete(f"/api/v1/shelves/{shelf['id']}", headers=ctx["headers"])
    camera = db_session.get(Camera, uuid.UUID(ctx["camera"]["id"]))
    assert build_pipeline_config(db_session, camera).shelves == []


def test_product_detection_classes_reach_the_shelf_definition(client, ctx, db_session):
    """A shelf stocked with a known product measures only that product."""
    from app.models import Camera
    from app.services.pipeline_manager import build_pipeline_config

    product = client.post(
        "/api/v1/products",
        json={"sku": "AZAM-500", "name": "Azam Cola", "detection_class_name": "bottle"},
        headers=ctx["headers"],
    ).json()
    make_shelf(client, ctx, product_ids=[product["id"]])

    camera = db_session.get(Camera, uuid.UUID(ctx["camera"]["id"]))
    config = build_pipeline_config(db_session, camera)
    assert config.shelves[0].product_classes == frozenset({"bottle"})


def test_the_engine_measures_a_shelf_built_from_the_database(client, ctx, db_session):
    """End to end: a stored shelf, the real engine, detections in, event out."""
    from app.models import Camera
    from app.services.pipeline_manager import build_pipeline_config
    from vision.inventory import InventoryEngine, InventoryObservationKind, OccupancyLevel
    from vision.types import BoundingBox, Detection

    make_shelf(client, ctx)
    camera = db_session.get(Camera, uuid.UUID(ctx["camera"]["id"]))
    config = build_pipeline_config(db_session, camera)

    engine = InventoryEngine(config.shelves, stability_samples=2)
    stocked = [
        Detection(
            class_id=0,
            class_name="bottle",
            confidence=0.9,
            bbox=BoundingBox(0.2, 0.4, 0.8, 0.6),
            timestamp=NOW,
            camera_id=str(camera.id),
        )
    ]
    for i in range(2):  # settle on a full shelf
        engine.update(stocked, NOW + timedelta(seconds=60 * i))
    observations = []
    for i in range(2, 4):  # then clear it
        observations += engine.update([], NOW + timedelta(seconds=60 * i))

    kinds = [o.kind for o in observations]
    assert InventoryObservationKind.SHELF_EMPTY in kinds
    assert engine.occupancy()[config.shelves[0].shelf_id][1] is OccupancyLevel.EMPTY
