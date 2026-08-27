"""Milestone 3 -- store and camera management.

The most important tests here are the credential ones. A camera password
grants live video access independent of VisionGuard, so the guarantee is
absolute: it is never returned, in any response, in any shape.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models import AuditLog, Camera, Store
from app.models.enums import CameraStatus, PlanTier, UserRole, VideoSourceKind
from tests import factories as f

PASSWORD = "tembo-anga-baridi-91"
CAMERA_SECRET = "Adm1n!CameraP@ss"


@pytest.fixture()
def org_ctx(client, db_session):
    """A registered organization with an admin token and one store."""
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "owner@duka.co.tz",
            "password": PASSWORD,
            "full_name": "Amina Owner",
            "organization_name": "Duka la Amina",
        },
    ).json()
    headers = {"Authorization": f"Bearer {registered['tokens']['access_token']}"}

    store = client.post(
        "/api/v1/stores",
        json={"name": "Kariakoo Branch", "code": "DSM-01", "city": "Dar es Salaam"},
        headers=headers,
    ).json()
    return {
        "headers": headers,
        "store": store,
        "organization_id": registered["organization_id"],
        "registered": registered,
    }


def upgrade_plan(db_session, organization_id: str, tier: PlanTier = PlanTier.BUSINESS) -> None:
    """Lift the trial limits for tests that are not about plan limits."""
    from app.models import Subscription

    subscription = db_session.scalar(
        select(Subscription).where(Subscription.organization_id == uuid.UUID(organization_id))
    )
    subscription.tier = tier
    db_session.commit()


def make_camera_payload(store_id: str, **overrides) -> dict:
    payload = {
        "store_id": store_id,
        "name": "Warehouse door",
        "source_kind": "RTSP",
        "host": "192.168.1.64",
        "port": 554,
        "stream_path": "/Streaming/Channels/102",
        "username": "admin",
        "password": CAMERA_SECRET,
        "target_fps": 8,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------- stores
def test_create_store(client, org_ctx):
    store = org_ctx["store"]
    assert store["code"] == "DSM-01"
    assert store["organization_id"] == org_ctx["organization_id"]
    # use_default_hours defaults to true, so a new store is configured.
    assert len(store["operating_hours"]) == 7


def test_store_code_is_uppercased(client, db_session, org_ctx):
    upgrade_plan(db_session, org_ctx["organization_id"])
    response = client.post(
        "/api/v1/stores",
        json={"name": "Second", "code": "dsm-02"},
        headers=org_ctx["headers"],
    )
    assert response.json()["code"] == "DSM-02"


def test_duplicate_store_code_is_rejected(client, db_session, org_ctx):
    upgrade_plan(db_session, org_ctx["organization_id"])
    response = client.post(
        "/api/v1/stores",
        json={"name": "Duplicate", "code": "DSM-01"},
        headers=org_ctx["headers"],
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_invalid_timezone_is_rejected(client, db_session, org_ctx):
    upgrade_plan(db_session, org_ctx["organization_id"])
    response = client.post(
        "/api/v1/stores",
        json={"name": "Nowhere", "code": "TZ-01", "timezone": "Mars/Olympus"},
        headers=org_ctx["headers"],
    )
    assert response.status_code == 422


def test_list_stores(client, org_ctx):
    body = client.get("/api/v1/stores", headers=org_ctx["headers"]).json()
    assert body["total"] == 1
    assert body["items"][0]["code"] == "DSM-01"


def test_update_store(client, org_ctx):
    response = client.patch(
        f"/api/v1/stores/{org_ctx['store']['id']}",
        json={"name": "Kariakoo Main", "timezone": "Africa/Nairobi"},
        headers=org_ctx["headers"],
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Kariakoo Main"
    assert response.json()["timezone"] == "Africa/Nairobi"


def test_deleting_a_store_disables_its_cameras(client, db_session, org_ctx):
    """A deleted store must not leave pipelines running."""
    camera = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    ).json()

    response = client.delete(f"/api/v1/stores/{org_ctx['store']['id']}", headers=org_ctx["headers"])
    assert response.status_code == 200

    row = db_session.get(Camera, uuid.UUID(camera["id"]))
    db_session.refresh(row)
    assert row.is_enabled is False
    assert row.status == CameraStatus.DISABLED


def test_deleted_store_is_hidden_but_retained(client, db_session, org_ctx):
    store_id = org_ctx["store"]["id"]
    client.delete(f"/api/v1/stores/{store_id}", headers=org_ctx["headers"])

    assert client.get("/api/v1/stores", headers=org_ctx["headers"]).json()["total"] == 0
    # The row survives: its events and evidence are the customer's record.
    assert db_session.get(Store, uuid.UUID(store_id)) is not None


# -------------------------------------------------------------- operating hours
def test_set_operating_hours(client, org_ctx):
    response = client.put(
        f"/api/v1/stores/{org_ctx['store']['id']}/operating-hours",
        json={
            "days": [
                {"day_of_week": d, "opens_at": "09:00:00", "closes_at": "21:00:00"}
                for d in range(6)
            ]
            + [{"day_of_week": 6, "is_closed": True}]
        },
        headers=org_ctx["headers"],
    )
    assert response.status_code == 200
    days = response.json()
    assert len(days) == 7
    assert days[0]["opens_at"] == "09:00:00"
    assert days[6]["is_closed"] is True


def test_overnight_hours_are_accepted(client, org_ctx):
    """A bar open 20:00-04:00 is a normal business, not a validation error."""
    response = client.put(
        f"/api/v1/stores/{org_ctx['store']['id']}/operating-hours",
        json={
            "days": [
                {"day_of_week": d, "opens_at": "20:00:00", "closes_at": "04:00:00"}
                for d in range(7)
            ]
        },
        headers=org_ctx["headers"],
    )
    assert response.status_code == 200


def test_open_day_without_times_is_rejected(client, org_ctx):
    response = client.put(
        f"/api/v1/stores/{org_ctx['store']['id']}/operating-hours",
        json={"days": [{"day_of_week": 0, "is_closed": False}]},
        headers=org_ctx["headers"],
    )
    assert response.status_code == 422


def test_duplicate_day_is_rejected(client, org_ctx):
    response = client.put(
        f"/api/v1/stores/{org_ctx['store']['id']}/operating-hours",
        json={
            "days": [
                {"day_of_week": 0, "opens_at": "08:00:00", "closes_at": "20:00:00"},
                {"day_of_week": 0, "opens_at": "09:00:00", "closes_at": "21:00:00"},
            ]
        },
        headers=org_ctx["headers"],
    )
    assert response.status_code == 422


# --------------------------------------------------------------------- cameras
def test_create_camera(client, org_ctx):
    response = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    )
    assert response.status_code == 201, response.text
    camera = response.json()
    assert camera["name"] == "Warehouse door"
    assert camera["status"] == "OFFLINE"
    assert camera["has_credentials"] is True
    assert camera["connection_hint"] == "rtsp://192.168.1.64:554/Streaming/Channels/102"


# ------------------------------------------------------- credential guarantees
def test_camera_password_is_never_returned_on_create(client, org_ctx):
    response = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    )
    assert CAMERA_SECRET not in response.text
    assert "password" not in response.json()


def test_camera_password_is_never_returned_on_read(client, org_ctx):
    created = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    ).json()

    for path in (f"/api/v1/cameras/{created['id']}", "/api/v1/cameras"):
        response = client.get(path, headers=org_ctx["headers"])
        assert CAMERA_SECRET not in response.text, f"secret leaked from {path}"
        assert "encrypted_password" not in response.text


def test_camera_response_schema_has_no_password_field():
    """Absent, not masked: a field that exists can be accidentally filled."""
    from app.schemas.camera import CameraRead, CameraSummary

    for schema in (CameraRead, CameraSummary):
        fields = set(schema.model_fields)
        assert "password" not in fields
        assert "encrypted_password" not in fields
        assert not any("password" in name for name in fields if name != "has_credentials")


def test_camera_password_is_encrypted_at_rest(client, db_session, org_ctx):
    created = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    ).json()

    row = db_session.get(Camera, uuid.UUID(created["id"]))
    assert row.encrypted_password
    assert CAMERA_SECRET not in row.encrypted_password

    from app.core.security import decrypt_credential

    assert decrypt_credential(row.encrypted_password) == CAMERA_SECRET


def test_camera_password_never_reaches_the_audit_log(client, db_session, org_ctx):
    client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    )
    for entry in db_session.scalars(select(AuditLog)).all():
        assert CAMERA_SECRET not in str(entry.changes)
        assert CAMERA_SECRET not in (entry.description or "")


def test_a_pasted_rtsp_url_in_the_host_field_is_rejected(client, org_ctx):
    """Pasting a full URL would store a password in a plaintext column."""
    response = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(
            org_ctx["store"]["id"], host="rtsp://admin:secret@192.168.1.64:554/stream"
        ),
        headers=org_ctx["headers"],
    )
    assert response.status_code == 422
    assert "secret" not in response.text.replace("VALIDATION_ERROR", "")


def test_host_with_embedded_credentials_is_rejected(client, org_ctx):
    response = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"], host="admin:pw@192.168.1.64"),
        headers=org_ctx["headers"],
    )
    assert response.status_code == 422


def test_omitting_the_password_on_update_keeps_it(client, db_session, org_ctx):
    created = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    ).json()

    client.patch(
        f"/api/v1/cameras/{created['id']}",
        json={"name": "Renamed"},
        headers=org_ctx["headers"],
    )
    row = db_session.get(Camera, uuid.UUID(created["id"]))
    db_session.refresh(row)
    assert row.encrypted_password is not None
    assert row.name == "Renamed"


def test_clear_password_removes_the_credential(client, db_session, org_ctx):
    created = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    ).json()

    response = client.patch(
        f"/api/v1/cameras/{created['id']}",
        json={"clear_password": True},
        headers=org_ctx["headers"],
    )
    assert response.json()["has_credentials"] is False
    row = db_session.get(Camera, uuid.UUID(created["id"]))
    db_session.refresh(row)
    assert row.encrypted_password is None


def test_deleting_a_camera_destroys_its_credentials(client, db_session, org_ctx):
    """A deleted camera has no legitimate use for a stored secret."""
    created = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    ).json()

    client.delete(f"/api/v1/cameras/{created['id']}", headers=org_ctx["headers"])
    row = db_session.get(Camera, uuid.UUID(created["id"]))
    db_session.refresh(row)
    assert row.encrypted_password is None
    assert row.deleted_at is not None


# ------------------------------------------------------------------ validation
def test_rtsp_camera_requires_a_host(client, org_ctx):
    payload = make_camera_payload(org_ctx["store"]["id"])
    payload.pop("host")
    response = client.post("/api/v1/cameras", json=payload, headers=org_ctx["headers"])
    assert response.status_code == 422


def test_file_camera_requires_a_locator(client, org_ctx):
    response = client.post(
        "/api/v1/cameras",
        json={
            "store_id": org_ctx["store"]["id"],
            "name": "Test clip",
            "source_kind": "FILE",
        },
        headers=org_ctx["headers"],
    )
    assert response.status_code == 422


def test_stream_path_is_normalised(client, org_ctx):
    created = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"], stream_path="stream1"),
        headers=org_ctx["headers"],
    ).json()
    assert created["stream_path"] == "/stream1"


def test_duplicate_camera_name_in_a_store_is_rejected(client, org_ctx):
    client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    )
    response = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    )
    assert response.status_code == 409


def test_unknown_extra_field_is_rejected(client, org_ctx):
    response = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"], rtsp_url="rtsp://x/y"),
        headers=org_ctx["headers"],
    )
    assert response.status_code == 422


# --------------------------------------------------------------- enable/disable
def test_disable_and_enable_a_camera(client, org_ctx):
    created = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    ).json()

    disabled = client.post(
        f"/api/v1/cameras/{created['id']}/disable", headers=org_ctx["headers"]
    ).json()
    assert disabled["is_enabled"] is False
    assert disabled["status"] == "DISABLED"

    enabled = client.post(
        f"/api/v1/cameras/{created['id']}/enable", headers=org_ctx["headers"]
    ).json()
    assert enabled["is_enabled"] is True
    assert enabled["status"] == "OFFLINE"


# -------------------------------------------------------------- connection test
def test_connection_test_reports_failure_without_erroring(client, org_ctx):
    """An unreachable camera is a diagnostic answer, not a 500."""
    created = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"], host="203.0.113.1"),
        headers=org_ctx["headers"],
    ).json()

    response = client.post(f"/api/v1/cameras/{created['id']}/test", headers=org_ctx["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["message"]
    # The failure message must not carry credentials.
    assert CAMERA_SECRET not in response.text


def test_connection_test_succeeds_on_a_synthetic_camera(client, org_ctx):
    """The synthetic source lets the whole path be exercised with no hardware."""
    created = client.post(
        "/api/v1/cameras",
        json={
            "store_id": org_ctx["store"]["id"],
            "name": "Synthetic test",
            "source_kind": "SYNTHETIC",
        },
        headers=org_ctx["headers"],
    ).json()

    response = client.post(f"/api/v1/cameras/{created['id']}/test", headers=org_ctx["headers"])
    body = response.json()
    assert body["success"] is True, body
    assert body["resolution"] == [640, 480]

    refreshed = client.get(f"/api/v1/cameras/{created['id']}", headers=org_ctx["headers"]).json()
    assert refreshed["status"] == "ONLINE"


# ------------------------------------------------------------------- streams
def test_set_streams_picks_an_inference_source(client, org_ctx):
    """Unmarked: the smallest stream wins, because it is cheapest to decode."""
    created = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    ).json()

    response = client.put(
        f"/api/v1/cameras/{created['id']}/streams",
        json={
            "streams": [
                {
                    "profile": "MAIN",
                    "stream_path": "/Streaming/Channels/101",
                    "width": 1920,
                    "height": 1080,
                },
                {
                    "profile": "SUB",
                    "stream_path": "/Streaming/Channels/102",
                    "width": 640,
                    "height": 360,
                },
            ]
        },
        headers=org_ctx["headers"],
    )
    assert response.status_code == 200
    streams = {s["profile"]: s for s in response.json()}
    assert streams["SUB"]["is_inference_source"] is True
    assert streams["MAIN"]["is_inference_source"] is False


def test_two_inference_sources_are_rejected(client, org_ctx):
    created = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    ).json()
    response = client.put(
        f"/api/v1/cameras/{created['id']}/streams",
        json={
            "streams": [
                {"profile": "MAIN", "stream_path": "/a", "is_inference_source": True},
                {"profile": "SUB", "stream_path": "/b", "is_inference_source": True},
            ]
        },
        headers=org_ctx["headers"],
    )
    assert response.status_code == 409


# --------------------------------------------------------------------- health
def test_camera_health_counts(client, org_ctx):
    client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    )
    client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"], name="Second"),
        headers=org_ctx["headers"],
    )
    body = client.get("/api/v1/cameras/health", headers=org_ctx["headers"]).json()
    assert body["total"] == 2
    assert body["OFFLINE"] == 2


# ------------------------------------------------------------ tenant isolation
def test_cameras_are_isolated_between_organizations(client, db_session):
    """The guarantee that, if broken, ends the product."""
    tokens = {}
    stores = {}
    for slug in ("one", "two"):
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{slug}@example.co.tz",
                "password": PASSWORD,
                "full_name": slug,
                "organization_name": f"Org {slug}",
            },
        ).json()
        headers = {"Authorization": f"Bearer {registered['tokens']['access_token']}"}
        tokens[slug] = headers
        stores[slug] = client.post(
            "/api/v1/stores",
            json={"name": f"{slug} store", "code": f"{slug.upper()}-01"},
            headers=headers,
        ).json()
        client.post(
            "/api/v1/cameras",
            json=make_camera_payload(stores[slug]["id"], name=f"{slug} camera"),
            headers=headers,
        )

    listing = client.get("/api/v1/cameras", headers=tokens["one"]).json()
    assert listing["total"] == 1
    assert listing["items"][0]["name"] == "one camera"

    # Fetching another tenant's camera by id must look like it does not exist.
    other = client.get("/api/v1/cameras", headers=tokens["two"]).json()["items"][0]
    response = client.get(f"/api/v1/cameras/{other['id']}", headers=tokens["one"])
    assert response.status_code == 404


def test_cannot_add_a_camera_to_another_organizations_store(client):
    tokens = {}
    stores = {}
    for slug in ("alpha", "beta"):
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{slug}@example.co.tz",
                "password": PASSWORD,
                "full_name": slug,
                "organization_name": f"Org {slug}",
            },
        ).json()
        headers = {"Authorization": f"Bearer {registered['tokens']['access_token']}"}
        tokens[slug] = headers
        stores[slug] = client.post(
            "/api/v1/stores",
            json={"name": f"{slug} store", "code": f"{slug[:3].upper()}-01"},
            headers=headers,
        ).json()

    response = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(stores["beta"]["id"]),
        headers=tokens["alpha"],
    )
    assert response.status_code == 404


# ------------------------------------------------------------------ permissions
def _login_as(client, db_session, org_id, role: UserRole, email: str):
    from app.models import Organization

    org = db_session.get(Organization, uuid.UUID(org_id))
    f.make_user(
        db_session,
        org,
        email=email,
        hashed_password=hash_password(PASSWORD),
        role=role,
    )
    db_session.commit()
    tokens = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()[
        "tokens"
    ]
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_viewer_cannot_create_a_camera(client, db_session, org_ctx):
    headers = _login_as(
        client, db_session, org_ctx["organization_id"], UserRole.VIEWER, "v@duka.co.tz"
    )
    response = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"], name="Nope"),
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_viewer_can_read_cameras(client, db_session, org_ctx):
    client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"]),
        headers=org_ctx["headers"],
    )
    headers = _login_as(
        client, db_session, org_ctx["organization_id"], UserRole.VIEWER, "v2@duka.co.tz"
    )
    assert client.get("/api/v1/cameras", headers=headers).status_code == 200


def test_security_operator_cannot_edit_cameras_but_can_test_them(client, db_session, org_ctx):
    created = client.post(
        "/api/v1/cameras",
        json={
            "store_id": org_ctx["store"]["id"],
            "name": "Synthetic",
            "source_kind": "SYNTHETIC",
        },
        headers=org_ctx["headers"],
    ).json()
    headers = _login_as(
        client,
        db_session,
        org_ctx["organization_id"],
        UserRole.SECURITY_OPERATOR,
        "op@duka.co.tz",
    )
    assert (
        client.patch(
            f"/api/v1/cameras/{created['id']}", json={"name": "X"}, headers=headers
        ).status_code
        == 403
    )
    assert client.post(f"/api/v1/cameras/{created['id']}/test", headers=headers).status_code == 200


def test_camera_endpoints_require_authentication(client, org_ctx):
    assert client.get("/api/v1/cameras").status_code == 401
    assert client.get("/api/v1/cameras/health").status_code == 401
    assert client.post("/api/v1/cameras", json={}).status_code == 401
    assert client.get("/api/v1/stores").status_code == 401


# ---------------------------------------------------------------- plan limits
def test_trial_plan_caps_camera_count(client, db_session, org_ctx):
    """A new organization is on TRIAL, which allows two cameras."""
    from app.models import Subscription

    subscription = db_session.scalar(
        select(Subscription).where(
            Subscription.organization_id == uuid.UUID(org_ctx["organization_id"])
        )
    )
    assert subscription.tier == PlanTier.TRIAL

    for index in range(2):
        response = client.post(
            "/api/v1/cameras",
            json=make_camera_payload(org_ctx["store"]["id"], name=f"Cam {index}"),
            headers=org_ctx["headers"],
        )
        assert response.status_code == 201

    third = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"], name="Cam 3"),
        headers=org_ctx["headers"],
    )
    assert third.status_code == 402
    assert third.json()["error"]["code"] == "PLAN_LIMIT_EXCEEDED"
    assert third.json()["error"]["details"]["maximum"] == 2


def test_deleting_a_camera_frees_a_plan_slot(client, org_ctx):
    created = []
    for index in range(2):
        created.append(
            client.post(
                "/api/v1/cameras",
                json=make_camera_payload(org_ctx["store"]["id"], name=f"Cam {index}"),
                headers=org_ctx["headers"],
            ).json()
        )
    client.delete(f"/api/v1/cameras/{created[0]['id']}", headers=org_ctx["headers"])

    response = client.post(
        "/api/v1/cameras",
        json=make_camera_payload(org_ctx["store"]["id"], name="Replacement"),
        headers=org_ctx["headers"],
    )
    assert response.status_code == 201


def test_trial_plan_caps_store_count(client, org_ctx):
    response = client.post(
        "/api/v1/stores",
        json={"name": "Second branch", "code": "DSM-02"},
        headers=org_ctx["headers"],
    )
    assert response.status_code == 402


# ------------------------------------------------------------- video source
def test_build_video_source_never_stores_the_url(db_session):
    """The playable URL exists only inside the source object."""
    from app.services.cameras import build_video_source

    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    from app.core.security import encrypt_credential

    camera = f.make_camera(
        db_session,
        store,
        host="192.168.1.64",
        port=554,
        stream_path="/stream1",
        username="admin",
        encrypted_password=encrypt_credential(CAMERA_SECRET),
        source_kind=VideoSourceKind.RTSP,
    )
    source = build_video_source(camera)

    # The source must keep the password in memory -- it has to rebuild the URL
    # on every reconnect. The guarantee is that it is never *exposed*: not in
    # repr, not in a log line, not in the diagnostic URL, and only ever in a
    # private attribute.
    assert CAMERA_SECRET not in repr(source)
    assert CAMERA_SECRET not in str(source)
    assert source.safe_url == "rtsp://admin:***@192.168.1.64:554/stream1"
    assert CAMERA_SECRET not in source.safe_url

    public_attributes = {
        name: value for name, value in vars(source).items() if not name.startswith("_")
    }
    assert not any(CAMERA_SECRET in str(value) for value in public_attributes.values())

    # And it does assemble a working URL when asked.
    assert (
        source._build_url()
        == f"rtsp://admin:{CAMERA_SECRET.replace('!', '%21').replace('@', '%40')}@192.168.1.64:554/stream1"
    )
