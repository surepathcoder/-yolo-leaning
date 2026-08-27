"""Health / readiness response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ComponentStatus = Literal["connected", "unavailable", "disabled"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = Field(description="Overall service status")
    database: ComponentStatus
    redis: ComponentStatus
    version: str
    environment: str
    uptime_seconds: float
    timestamp: datetime


class ReadinessResponse(BaseModel):
    ready: bool
    checks: dict[str, bool]
