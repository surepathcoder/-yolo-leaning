"""Milestone 0 -- health, readiness and liveness endpoints."""

from __future__ import annotations

import app.api.v1.health as health_module


def test_liveness_touches_no_dependencies(client, monkeypatch):
    def explode():  # pragma: no cover - must not be called
        raise AssertionError("liveness must not touch the database")

    monkeypatch.setattr(health_module, "check_database", explode)
    response = client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_health_reports_ok_when_dependencies_are_up(client, monkeypatch):
    monkeypatch.setattr(health_module, "check_database", lambda: (True, None))
    monkeypatch.setattr(health_module, "check_redis", lambda: (True, None))

    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "connected"
    assert body["redis"] == "connected"
    assert body["version"]
    assert body["environment"] == "test"
    assert body["uptime_seconds"] >= 0


def test_health_reports_degraded_when_redis_is_down(client, monkeypatch):
    monkeypatch.setattr(health_module, "check_database", lambda: (True, None))
    monkeypatch.setattr(health_module, "check_redis", lambda: (False, "connection refused"))

    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["redis"] == "unavailable"
    # The internal error string must not be exposed.
    assert "connection refused" not in response.text


def test_readiness_returns_503_when_a_dependency_is_down(client, monkeypatch):
    monkeypatch.setattr(health_module, "check_database", lambda: (False, "boom"))
    monkeypatch.setattr(health_module, "check_redis", lambda: (True, None))

    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"ready": False, "checks": {"database": False, "redis": True}}


def test_request_id_header_is_returned(client):
    response = client.get("/live")
    assert response.headers.get("X-Request-ID")


def test_request_id_is_echoed_when_supplied(client):
    response = client.get("/live", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


def test_root_endpoint(client):
    body = client.get("/").json()
    assert body["name"] == "VisionGuard"
    assert body["health"] == "/health"


def test_openapi_schema_is_served(client):
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"].startswith("VisionGuard")
