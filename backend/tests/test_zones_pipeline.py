"""Milestone 7 + pipeline integration.

Zone geometry is validated at the API using the same functions the engine uses
at runtime. That symmetry is the point: a shape the API accepts but the engine
rejects produces a camera that silently never fires its rules, and the
installer has no way to discover why.

The pipeline tests drive a real ``CameraPipeline`` through the real event and
alert services, with a synthetic source and a ground-truth detector so the
expected outcome is exactly knowable.
"""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import Alert, Event, EventEvidence, Zone
from app.models.enums import EventType, PlanTier, UserRole, VideoSourceKind
from tests import factories as f

PASSWORD = "tembo-anga-baridi-91"
SQUARE = [[0.35, 0.20], [0.75, 0.20], [0.75, 0.95], [0.35, 0.95]]


@pytest.fixture()
def ctx(client, db_session):
    """An organization on a BUSINESS plan with a synthetic camera."""
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
        json={
            "store_id": store["id"],
            "name": "Warehouse door",
            "source_kind": "SYNTHETIC",
        },
        headers=headers,
    ).json()
    return {
        "headers": headers,
        "store": store,
        "camera": camera,
        "organization_id": registered["organization_id"],
    }


# ------------------------------------------------------------------ zones API
def test_create_a_zone(client, ctx):
    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/zones",
        json={"name": "Warehouse", "zone_type": "RESTRICTED", "polygon": SQUARE},
        headers=ctx["headers"],
    )
    assert response.status_code == 201, response.text
    zone = response.json()
    assert zone["name"] == "Warehouse"
    assert zone["zone_type"] == "RESTRICTED"
    assert len(zone["polygon"]) == 4
    assert zone["reference_point"] == "bottom_center"


def test_a_zone_with_too_few_points_is_rejected(client, ctx):
    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/zones",
        json={"name": "Line", "polygon": [[0.1, 0.1], [0.5, 0.5]]},
        headers=ctx["headers"],
    )
    assert response.status_code == 422


def test_a_degenerate_zone_is_rejected(client, ctx):
    """Three collinear points are a line, not an area -- it would match nothing."""
    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/zones",
        json={"name": "Flat", "polygon": [[0.1, 0.1], [0.4, 0.4], [0.8, 0.8]]},
        headers=ctx["headers"],
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_POLYGON"


def test_a_tiny_zone_is_rejected_with_an_explanation(client, ctx):
    """A zone of 0.1% of the frame will never reliably contain a person."""
    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/zones",
        json={
            "name": "Speck",
            "polygon": [[0.50, 0.50], [0.505, 0.50], [0.505, 0.505], [0.50, 0.505]],
        },
        headers=ctx["headers"],
    )
    assert response.status_code == 422
    assert "too small" in response.json()["error"]["message"].lower()


def test_pixel_coordinates_are_rejected(client, ctx):
    """A common mistake: pasting pixels into a normalised field."""
    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/zones",
        json={
            "name": "Pixels",
            "polygon": [[100, 200], [400, 200], [400, 600], [100, 600]],
        },
        headers=ctx["headers"],
    )
    assert response.status_code == 422


def test_concave_zones_are_accepted(client, ctx):
    """Installers draw L-shapes constantly -- around a counter, or a corner."""
    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/zones",
        json={
            "name": "L shape",
            "polygon": [
                [0.05, 0.05],
                [0.60, 0.05],
                [0.60, 0.35],
                [0.95, 0.35],
                [0.95, 0.95],
                [0.05, 0.95],
            ],
        },
        headers=ctx["headers"],
    )
    assert response.status_code == 201
    assert len(response.json()["polygon"]) == 6


def test_update_and_delete_a_zone(client, db_session, ctx):
    zone = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/zones",
        json={"name": "Warehouse", "polygon": SQUARE},
        headers=ctx["headers"],
    ).json()

    updated = client.patch(
        f"/api/v1/zones/{zone['id']}",
        json={"name": "Back store", "loitering_threshold_seconds": 45},
        headers=ctx["headers"],
    ).json()
    assert updated["name"] == "Back store"
    assert updated["loitering_threshold_seconds"] == 45

    assert client.delete(f"/api/v1/zones/{zone['id']}", headers=ctx["headers"]).status_code == 200
    assert client.get("/api/v1/zones", headers=ctx["headers"]).json() == []
    # Soft-deleted, not destroyed: the events it produced still reference it.
    assert db_session.get(Zone, uuid.UUID(zone["id"])) is not None


def test_zones_are_isolated_between_tenants(client, ctx):
    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": "other@duka.co.tz",
            "password": PASSWORD,
            "full_name": "Other",
            "organization_name": "Other Org",
        },
    ).json()
    other_headers = {"Authorization": f"Bearer {other['tokens']['access_token']}"}

    zone = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/zones",
        json={"name": "Mine", "polygon": SQUARE},
        headers=ctx["headers"],
    ).json()

    assert client.get("/api/v1/zones", headers=other_headers).json() == []
    assert client.get(f"/api/v1/zones/{zone['id']}", headers=other_headers).status_code == 404


def test_viewer_cannot_draw_zones(client, db_session, ctx):
    from app.models import Organization

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

    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/zones",
        json={"name": "Nope", "polygon": SQUARE},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert response.status_code == 403


# ------------------------------------------------------------------ lines API
def test_create_a_counting_line(client, ctx):
    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/lines",
        json={"name": "Front door", "point_a": [0.5, 0.0], "point_b": [0.5, 1.0]},
        headers=ctx["headers"],
    )
    assert response.status_code == 201
    line = response.json()
    assert line["point_a"] == [0.5, 0.0]
    assert line["cooldown_seconds"] == 3.0


def test_a_zero_length_line_is_rejected(client, ctx):
    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/lines",
        json={"name": "Point", "point_a": [0.5, 0.5], "point_b": [0.5, 0.5]},
        headers=ctx["headers"],
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_LINE"


def test_a_very_short_line_is_rejected(client, ctx):
    """Too short to count reliably; better to say so than to count wrong."""
    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/lines",
        json={"name": "Stub", "point_a": [0.50, 0.50], "point_b": [0.52, 0.51]},
        headers=ctx["headers"],
    )
    assert response.status_code == 422
    assert "too short" in response.json()["error"]["message"].lower()


def test_update_and_delete_a_line(client, ctx):
    line = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/lines",
        json={"name": "Front door", "point_a": [0.5, 0.0], "point_b": [0.5, 1.0]},
        headers=ctx["headers"],
    ).json()

    updated = client.patch(
        f"/api/v1/lines/{line['id']}",
        json={"invert_direction": True, "cooldown_seconds": 5},
        headers=ctx["headers"],
    ).json()
    assert updated["invert_direction"] is True
    assert updated["cooldown_seconds"] == 5.0

    assert client.delete(f"/api/v1/lines/{line['id']}", headers=ctx["headers"]).status_code == 200
    assert client.get("/api/v1/lines", headers=ctx["headers"]).json() == []


# --------------------------------------------------- pipeline configuration
def test_pipeline_config_is_built_from_the_database(db_session):
    """The translation from ORM rows into the engine's plain definitions."""
    from app.services.pipeline_manager import build_pipeline_config

    org = f.make_organization(db_session)
    store = f.make_store(db_session, org, timezone="Africa/Dar_es_Salaam")
    f.make_operating_hours(db_session, store)
    camera = f.make_camera(db_session, store, source_kind=VideoSourceKind.SYNTHETIC)
    f.make_zone(db_session, camera, name="Warehouse", polygon=SQUARE)
    f.make_counting_line(db_session, camera, name="Door")
    db_session.flush()

    config = build_pipeline_config(db_session, camera)
    assert config.camera_id == str(camera.id)
    assert len(config.zones) == 1
    assert config.zones[0].name == "Warehouse"
    assert len(config.lines) == 1
    assert config.schedule is not None
    assert config.schedule.timezone_name == "Africa/Dar_es_Salaam"


def test_an_invalid_zone_does_not_blind_the_camera(db_session):
    """One badly drawn zone must not stop the other rules running."""
    from app.services.pipeline_manager import build_zone_definitions

    org = f.make_organization(db_session)
    camera = f.make_camera(db_session, f.make_store(db_session, org))
    good = f.make_zone(db_session, camera, name="Good", polygon=SQUARE)
    bad = f.make_zone(db_session, camera, name="Bad", polygon=SQUARE)
    # Corrupt the stored polygon the way a bad migration or manual edit could.
    bad.polygon = [[0.1, 0.1], [0.2, 0.2]]
    db_session.flush()

    definitions = build_zone_definitions([good, bad], default_loitering=60.0)
    assert [d.name for d in definitions] == ["Good"]


def test_zone_thresholds_fall_back_to_the_global_default(db_session):
    from app.services.pipeline_manager import build_zone_definitions

    org = f.make_organization(db_session)
    camera = f.make_camera(db_session, f.make_store(db_session, org))
    zone = f.make_zone(db_session, camera, polygon=SQUARE)
    assert zone.loitering_threshold_seconds is None

    definitions = build_zone_definitions([zone], default_loitering=90.0)
    assert definitions[0].loitering_threshold_seconds == 90.0


# ------------------------------------------------------- end-to-end pipeline
@pytest.mark.slow
def test_a_synthetic_pipeline_produces_events_and_alerts(
    client, db_session, ctx, tmp_path, monkeypatch
):
    """The whole chain: frames -> tracking -> zones -> rules -> events -> alerts.

    The detector is driven by the synthetic source's ground truth, so exactly
    what should happen is known: one person walks left to right through a
    restricted zone, across a counting line, past an (unvisited) checkout
    zone and into an exit zone -- covering M18's exit-without-checkout rule
    in the same real pipeline run as the original zone/line/loitering chain.
    """
    camera_id = ctx["camera"]["id"]
    client.post(
        f"/api/v1/cameras/{camera_id}/zones",
        json={
            "name": "Warehouse",
            "zone_type": "RESTRICTED",
            "polygon": SQUARE,
            "loitering_threshold_seconds": 1,
        },
        headers=ctx["headers"],
    )
    client.post(
        f"/api/v1/cameras/{camera_id}/lines",
        json={"name": "Door", "point_a": [0.55, 0.0], "point_b": [0.55, 1.0]},
        headers=ctx["headers"],
    )
    # Off the object's fixed horizontal path (which stays at y=0.625): never
    # visited, so the exit below must fire as unaccompanied by a checkout.
    client.post(
        f"/api/v1/cameras/{camera_id}/zones",
        json={
            "name": "Checkout counter",
            "zone_type": "CHECKOUT",
            "polygon": [[0.05, 0.02], [0.95, 0.02], [0.95, 0.15], [0.05, 0.15]],
        },
        headers=ctx["headers"],
    )
    # On the path, near its end: the object reaches this well within the
    # 60-frame clip, with enough continuous presence to clear zone-entry
    # debounce.
    client.post(
        f"/api/v1/cameras/{camera_id}/zones",
        json={
            "name": "Front door",
            "zone_type": "EXIT",
            "polygon": [[0.55, 0.55], [0.80, 0.55], [0.80, 0.70], [0.55, 0.70]],
        },
        headers=ctx["headers"],
    )

    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(get_settings(), "EVIDENCE_STORAGE_PATH", str(evidence_dir))

    started = client.post(f"/api/v1/cameras/{camera_id}/pipeline/start", headers=ctx["headers"])
    assert started.status_code == 200, started.text

    # The synthetic source yields 60 frames; poll rather than sleeping blind.
    from app.services.pipeline_manager import manager as pipeline_manager

    running = pipeline_manager.get(uuid.UUID(camera_id))
    assert running is not None
    deadline = time.time() + 30
    # 4 distinct event types are expected chronologically before the object
    # reaches the exit zone: restricted-area entry, loitering, line crossed,
    # then possible-exit-without-payment.
    while time.time() < deadline and running.events_ingested < 4:
        time.sleep(0.25)

    client.post(f"/api/v1/cameras/{camera_id}/pipeline/stop", headers=ctx["headers"])

    events = db_session.scalars(select(Event)).all()
    types = {event.event_type for event in events}
    assert EventType.RESTRICTED_AREA_ENTRY in types, f"got {types}"
    assert EventType.LOITERING in types
    assert EventType.LINE_CROSSED in types
    assert EventType.POSSIBLE_EXIT_WITHOUT_PAYMENT in types, f"got {types}"

    exit_event = next(e for e in events if e.event_type == EventType.POSSIBLE_EXIT_WITHOUT_PAYMENT)
    assert exit_event.is_inference is True

    # Self-test events are stamped so nothing reads them as real observations.
    for event in events:
        assert event.event_metadata.get("synthetic") is True

    # A restricted-area entry is HIGH severity and must have escalated.
    alerts = db_session.scalars(select(Alert)).all()
    assert alerts, "a restricted-area entry should have raised an alert"
    assert not any("theft" in alert.title.lower() for alert in alerts)

    # M13: the HIGH-severity events captured evidence -- a snapshot, a
    # thumbnail and a clip -- and the files landed on disk.
    evidence = db_session.scalars(select(EventEvidence)).all()
    kinds = {item.kind.value for item in evidence}
    assert {"SNAPSHOT", "THUMBNAIL", "CLIP"} <= kinds, f"got {kinds}"
    for item in evidence:
        assert (evidence_dir / item.storage_path).is_file()
        assert item.checksum and item.size_bytes > 0


@pytest.mark.slow
def test_starting_a_pipeline_twice_is_rejected(client, ctx):
    camera_id = ctx["camera"]["id"]
    first = client.post(f"/api/v1/cameras/{camera_id}/pipeline/start", headers=ctx["headers"])
    assert first.status_code == 200
    try:
        second = client.post(f"/api/v1/cameras/{camera_id}/pipeline/start", headers=ctx["headers"])
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "CAMERA_ALREADY_RUNNING"
    finally:
        client.post(f"/api/v1/cameras/{camera_id}/pipeline/stop", headers=ctx["headers"])


def test_starting_a_disabled_camera_is_rejected(client, ctx):
    camera_id = ctx["camera"]["id"]
    client.post(f"/api/v1/cameras/{camera_id}/disable", headers=ctx["headers"])

    response = client.post(f"/api/v1/cameras/{camera_id}/pipeline/start", headers=ctx["headers"])
    assert response.status_code == 502
    assert "disabled" in response.json()["error"]["message"].lower()


def test_stopping_a_pipeline_that_is_not_running(client, ctx):
    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/pipeline/stop", headers=ctx["headers"]
    )
    assert response.status_code == 404


def test_pipeline_status_when_nothing_is_running(client, ctx):
    body = client.get(
        f"/api/v1/cameras/{ctx['camera']['id']}/pipeline", headers=ctx["headers"]
    ).json()
    assert body["running"] is False


def test_viewer_cannot_start_a_pipeline(client, db_session, ctx):
    from app.models import Organization

    org = db_session.get(Organization, uuid.UUID(ctx["organization_id"]))
    f.make_user(
        db_session,
        org,
        email="viewer2@duka.co.tz",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.VIEWER,
    )
    db_session.commit()
    tokens = client.post(
        "/api/v1/auth/login",
        json={"email": "viewer2@duka.co.tz", "password": PASSWORD},
    ).json()["tokens"]

    response = client.post(
        f"/api/v1/cameras/{ctx['camera']['id']}/pipeline/start",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert response.status_code == 403


# --------------------------------------------------------------- diagnostics
def test_vision_device_endpoint(client, ctx):
    body = client.get("/api/v1/system/vision/device", headers=ctx["headers"]).json()
    assert body["resolved_device"] in {"cpu", "cuda", "mps"}
    assert "model_present" in body
    assert body["tracker"] in {"bytetrack", "botsort"}


def test_vision_metrics_endpoint(client, ctx):
    body = client.get("/api/v1/system/vision/metrics", headers=ctx["headers"]).json()
    assert "pipelines_running" in body
    assert "realtime" in body


def test_plan_endpoint_reports_usage(client, ctx):
    body = client.get("/api/v1/system/plan", headers=ctx["headers"]).json()
    assert body["tier"] == "BUSINESS"
    assert body["limits"]["max_cameras"]["current"] == 1


def test_metrics_require_permission(client, db_session, ctx):
    from app.models import Organization

    org = db_session.get(Organization, uuid.UUID(ctx["organization_id"]))
    f.make_user(
        db_session,
        org,
        email="analyst@duka.co.tz",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.ANALYST,
    )
    db_session.commit()
    tokens = client.post(
        "/api/v1/auth/login",
        json={"email": "analyst@duka.co.tz", "password": PASSWORD},
    ).json()["tokens"]

    response = client.get(
        "/api/v1/system/vision/metrics",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert response.status_code == 403
