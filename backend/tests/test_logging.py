"""Milestone 0 -- structured logging must never leak credentials."""

from __future__ import annotations

from app.core.logging import REDACTED, _redact_secrets


def test_sensitive_keys_are_redacted():
    event = {
        "event": "camera_connected",
        "camera_id": "cam-1",
        "password": "hunter2",
        "rtsp_password": "secret",
        "access_token": "eyJ-token",
        "api_key": "k",
    }
    redacted = _redact_secrets(None, "info", dict(event))
    assert redacted["camera_id"] == "cam-1"
    for key in ("password", "rtsp_password", "access_token", "api_key"):
        assert redacted[key] == REDACTED


def test_redaction_is_case_insensitive():
    redacted = _redact_secrets(None, "info", {"PASSWORD": "x", "Authorization": "Bearer y"})
    assert redacted["PASSWORD"] == REDACTED
    assert redacted["Authorization"] == REDACTED


def test_non_sensitive_values_untouched():
    event = {"inference_ms": 12.5, "fps": 9.8, "camera_id": "cam-2"}
    assert _redact_secrets(None, "info", dict(event)) == event
