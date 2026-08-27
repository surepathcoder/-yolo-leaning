"""Milestone 0 -- configuration management."""

from __future__ import annotations

import pytest

from app.core.config import Settings


def test_defaults_are_sane():
    settings = Settings(_env_file=None)
    assert settings.APP_NAME == "VisionGuard"
    assert settings.API_V1_PREFIX == "/api/v1"
    assert 0 < settings.YOLO_CONFIDENCE_THRESHOLD <= 1
    assert settings.DEFAULT_CURRENCY == "TZS"
    assert settings.DEFAULT_TIMEZONE == "Africa/Dar_es_Salaam"


def test_cors_origins_accepts_comma_separated_string():
    settings = Settings(_env_file=None, CORS_ORIGINS="http://a.test, http://b.test")
    assert settings.CORS_ORIGINS == ["http://a.test", "http://b.test"]


def test_cors_origins_accepts_json_list():
    settings = Settings(_env_file=None, CORS_ORIGINS='["http://a.test"]')
    assert settings.CORS_ORIGINS == ["http://a.test"]


def test_cors_origins_empty_string_is_empty_list():
    assert Settings(_env_file=None, CORS_ORIGINS="").CORS_ORIGINS == []


@pytest.mark.parametrize("bad", [0.0, -0.5, 1.5])
def test_confidence_threshold_must_be_in_unit_interval(bad):
    with pytest.raises(ValueError):
        Settings(_env_file=None, YOLO_CONFIDENCE_THRESHOLD=bad)


def test_is_production_flag():
    assert Settings(_env_file=None, ENVIRONMENT="production").is_production is True
    assert Settings(_env_file=None, ENVIRONMENT="development").is_production is False


def test_ensure_storage_dirs_creates_paths(tmp_path):
    settings = Settings(
        _env_file=None,
        VIDEO_STORAGE_PATH=str(tmp_path / "media"),
        EVIDENCE_STORAGE_PATH=str(tmp_path / "evidence"),
    )
    settings.ensure_storage_dirs()
    assert (tmp_path / "media").is_dir()
    assert (tmp_path / "evidence").is_dir()
