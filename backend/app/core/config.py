"""Central application configuration.

All runtime configuration comes from environment variables (optionally loaded
from a ``.env`` file at the repository root).  Nothing in the codebase should
read ``os.environ`` directly -- import :func:`get_settings` instead so that
configuration stays testable and overridable.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repository root: this file is backend/app/core/config.py
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Typed application settings."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("VISIONGUARD_ENV_FILE", str(REPO_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    APP_NAME: str = "VisionGuard"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = "development"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # ------------------------------------------------------------- database
    DATABASE_URL: str = "postgresql+psycopg://visionguard:visionguard@localhost:5434/visionguard"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_ECHO: bool = False

    # ---------------------------------------------------------------- redis
    REDIS_URL: str = "redis://localhost:6380/0"

    # ----------------------------------------------------------- auth / jwt
    JWT_SECRET: str = "change-me-in-production-this-is-not-a-real-secret"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    # Key used to encrypt camera credentials at rest (Fernet, urlsafe base64 32 bytes).
    # Empty in development -> a deterministic dev key is derived from JWT_SECRET.
    CREDENTIALS_ENCRYPTION_KEY: str = ""

    # ----------------------------------------------------------------- cors
    # NoDecode: pydantic-settings would otherwise JSON-decode the raw env value
    # before our validator runs, rejecting the friendlier comma-separated form.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    # ---------------------------------------------------------------- vision
    YOLO_MODEL_PATH: str = str(REPO_ROOT / "yolo26n.pt")
    YOLO_CONFIDENCE_THRESHOLD: float = 0.35
    YOLO_IOU_THRESHOLD: float = 0.45
    YOLO_DEVICE: str = "auto"
    YOLO_IMAGE_SIZE: int = 640
    VISION_FPS: float = 10.0
    VISION_FRAME_QUEUE_SIZE: int = 8
    TRACKER_TYPE: Literal["bytetrack", "botsort"] = "bytetrack"

    # --------------------------------------------------------------- storage
    VIDEO_STORAGE_PATH: str = str(REPO_ROOT / "data" / "media")
    EVIDENCE_STORAGE_PATH: str = str(REPO_ROOT / "data" / "evidence")
    EVENT_RETENTION_DAYS: int = 90
    EVIDENCE_RETENTION_DAYS: int = 30
    SNAPSHOT_RETENTION_DAYS: int = 30

    # ---------------------------------------------------------------- events
    LOITERING_THRESHOLD_SECONDS: int = 60
    LINE_CROSSING_COOLDOWN_SECONDS: float = 3.0
    POS_CORRELATION_WINDOW_SECONDS: int = 120

    # --------------------------------------------------------------- logging
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "console"

    # ------------------------------------------------------------ locale/tz
    DEFAULT_TIMEZONE: str = "Africa/Dar_es_Salaam"
    DEFAULT_CURRENCY: str = "TZS"
    DEFAULT_LOCALE: str = "en"

    # ----------------------------------------------------------- rate limits
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 10
    RATE_LIMIT_DEFAULT_PER_MINUTE: int = 300

    # ------------------------------------------------------------- edge sync
    EDGE_MODE: bool = False
    EDGE_CLOUD_API_URL: str = ""
    EDGE_API_KEY: str = ""
    EDGE_QUEUE_PATH: str = str(REPO_ROOT / "data" / "edge_queue.db")

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Allow ``CORS_ORIGINS=http://a,http://b`` in .env files."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "YOLO_MODEL_PATH",
        "VIDEO_STORAGE_PATH",
        "EVIDENCE_STORAGE_PATH",
        "EDGE_QUEUE_PATH",
        mode="after",
    )
    @classmethod
    def _resolve_against_repo_root(cls, value: str) -> str:
        """Interpret a relative path as relative to the project, not the cwd.

        ``.env`` naturally contains ``./yolo26n.pt``. The API is launched from
        ``backend/``, workers from the repo root, and Docker from ``/app`` --
        so a cwd-relative path resolves to a different file (or none) depending
        on how the process happened to be started. Anchoring to the repo root
        makes one ``.env`` correct everywhere.
        """
        if not value:
            return value
        path = Path(value)
        if path.is_absolute():
            return str(path)
        return str((REPO_ROOT / path).resolve())

    @field_validator("YOLO_CONFIDENCE_THRESHOLD", "YOLO_IOU_THRESHOLD")
    @classmethod
    def _check_unit_interval(cls, value: float) -> float:
        if not 0.0 < value <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        return value

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    def ensure_storage_dirs(self) -> None:
        """Create the storage directories the app writes to."""
        for path in (self.VIDEO_STORAGE_PATH, self.EVIDENCE_STORAGE_PATH):
            Path(path).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the settings cache -- used by tests that patch the environment."""
    get_settings.cache_clear()
    return get_settings()
