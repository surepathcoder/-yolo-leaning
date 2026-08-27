"""Milestone 0 -- the API error envelope."""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.core.errors import (
    AppError,
    CameraConnectionError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
)


def test_app_error_payload_shape():
    err = CameraConnectionError(details={"camera_id": "cam-1"})
    payload = err.to_payload()
    assert payload == {
        "error": {
            "code": ErrorCode.CAMERA_CONNECTION_FAILED,
            "message": "Unable to connect to the camera.",
            "details": {"camera_id": "cam-1"},
        }
    }
    assert err.status_code == 502


def test_error_subclass_status_codes():
    assert NotFoundError().status_code == 404
    assert PermissionDeniedError().status_code == 403


def test_internal_message_is_not_in_payload():
    err = AppError(internal_message="OperationalError: password authentication failed")
    assert "password authentication" not in str(err.to_payload())


@pytest.fixture()
def error_client(app):
    router = APIRouter()

    @router.get("/_test/app-error")
    def raise_app_error():
        raise NotFoundError("Camera not found.", details={"camera_id": "cam-9"})

    @router.get("/_test/boom")
    def raise_unhandled():
        raise RuntimeError("secret internal detail: db password is hunter2")

    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_app_error_is_rendered_as_envelope(error_client):
    response = error_client.get("/_test/app-error")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND
    assert response.json()["error"]["details"]["camera_id"] == "cam-9"


def test_unhandled_exception_hides_internals(error_client):
    response = error_client.get("/_test/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == ErrorCode.INTERNAL_ERROR
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    assert body["error"]["details"]["incident_id"]


def test_unknown_route_uses_the_envelope(error_client):
    response = error_client.get("/_test/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND
