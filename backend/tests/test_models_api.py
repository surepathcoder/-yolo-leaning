"""Milestone 15 -- the model management endpoints."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import Organization
from app.models.enums import PlanTier, UserRole
from tests import factories as f

PASSWORD = "tembo-anga-baridi-91"
WEIGHTS = b"PK\x03\x04 fake-yolo " + b"y" * 400


@pytest.fixture(autouse=True)
def _model_store(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "MODEL_STORAGE_PATH", str(tmp_path))
    return tmp_path


@pytest.fixture()
def ctx(client, db_session):
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "admin@duka.co.tz",
            "password": PASSWORD,
            "full_name": "Org Admin",
            "organization_name": "Duka la Amina",
        },
    ).json()
    headers = {"Authorization": f"Bearer {registered['tokens']['access_token']}"}
    org = db_session.get(Organization, uuid.UUID(registered["organization_id"]))

    sub = db_session.scalar(select(f.Subscription).where(f.Subscription.organization_id == org.id))
    sub.tier = PlanTier.BUSINESS
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    db_session.commit()
    return {"headers": headers, "org": org, "store": store, "camera": camera}


def register(client, headers, *, name="products", version="v1", map50=0.8, class_names=None):
    meta = {
        "name": name,
        "version": version,
        "class_names": class_names or ["red_carton", "blue_carton"],
    }
    if map50 is not None:
        meta["map50"] = map50
        meta["map50_95"] = 0.5
    return client.post(
        "/api/v1/models",
        files={"weights": ("best.pt", WEIGHTS, "application/octet-stream")},
        data={"metadata": json.dumps(meta)},
        headers=headers,
    )


def login(client, db_session, org, *, email, role) -> dict:
    f.make_user(db_session, org, email=email, hashed_password=hash_password(PASSWORD), role=role)
    db_session.commit()
    tokens = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()[
        "tokens"
    ]
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_register_then_list_and_get(client, ctx):
    created = register(client, ctx["headers"])
    assert created.status_code == 201, created.text
    model = created.json()
    assert model["status"] == "VALIDATED"
    assert model["activatable"] is True

    listing = client.get("/api/v1/models", headers=ctx["headers"]).json()
    assert [m["id"] for m in listing] == [model["id"]]

    detail = client.get(f"/api/v1/models/{model['id']}", headers=ctx["headers"]).json()
    assert detail["checksum"] and detail["class_names"] == ["red_carton", "blue_carton"]


def test_duplicate_registration_conflicts(client, ctx):
    register(client, ctx["headers"])
    dup = register(client, ctx["headers"])
    assert dup.status_code == 409


def test_activation_gate_and_one_active_per_name(client, ctx):
    threshold = get_settings().MODEL_MIN_MAP50
    weak = register(client, ctx["headers"], version="v1", map50=threshold - 0.1).json()
    strong = register(client, ctx["headers"], version="v2", map50=0.9).json()

    denied = client.post(f"/api/v1/models/{weak['id']}/activate", headers=ctx["headers"])
    assert denied.status_code == 409

    ok = client.post(f"/api/v1/models/{strong['id']}/activate", headers=ctx["headers"])
    assert ok.status_code == 200 and ok.json()["status"] == "ACTIVE"

    # A better v1 that then activates should archive v2.
    better_v1 = register(client, ctx["headers"], version="v3", map50=0.95).json()
    client.post(f"/api/v1/models/{better_v1['id']}/activate", headers=ctx["headers"])
    assert (
        client.get(f"/api/v1/models/{strong['id']}", headers=ctx["headers"]).json()["status"]
        == "ARCHIVED"
    )


def test_assign_and_unassign_a_camera(client, ctx):
    model = register(client, ctx["headers"], map50=0.9).json()
    client.post(f"/api/v1/models/{model['id']}/activate", headers=ctx["headers"])
    cam = ctx["camera"]["id"] if isinstance(ctx["camera"], dict) else str(ctx["camera"].id)

    assigned = client.put(f"/api/v1/models/{model['id']}/cameras/{cam}", headers=ctx["headers"])
    assert assigned.status_code == 200
    detail = client.get(f"/api/v1/models/{model['id']}", headers=ctx["headers"]).json()
    assert cam in detail["camera_ids"]

    client.delete(f"/api/v1/models/{model['id']}/cameras/{cam}", headers=ctx["headers"])
    detail = client.get(f"/api/v1/models/{model['id']}", headers=ctx["headers"]).json()
    assert detail["camera_ids"] == []


def test_another_tenant_cannot_see_or_touch_the_model(client, db_session, ctx):
    model = register(client, ctx["headers"]).json()
    outsider = login(
        client,
        db_session,
        f.make_organization(db_session),
        email="x@other.co.tz",
        role=UserRole.ORGANIZATION_ADMIN,
    )
    assert client.get("/api/v1/models", headers=outsider).json() == []
    assert client.get(f"/api/v1/models/{model['id']}", headers=outsider).status_code == 404
    assert (
        client.post(f"/api/v1/models/{model['id']}/activate", headers=outsider).status_code == 404
    )


def test_store_manager_cannot_register(client, db_session, ctx):
    manager = login(
        client, db_session, ctx["org"], email="mgr@duka.co.tz", role=UserRole.STORE_MANAGER
    )
    denied = register(client, manager)
    assert denied.status_code == 403
    # ...but may list (model:read).
    assert client.get("/api/v1/models", headers=manager).status_code == 200
