"""Custom detection model schemas (milestone 15)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import ModelStatus, ModelTask
from app.schemas.common import APISchema


class ModelVersionSummary(APISchema):
    id: uuid.UUID
    organization_id: uuid.UUID | None
    name: str
    version: str
    task: ModelTask
    status: ModelStatus
    class_names: list[str]
    map50: float | None
    map50_95: float | None
    trained_at: datetime | None
    activated_at: datetime | None
    created_at: datetime
    #: True when this model clears the activation mAP gate.
    activatable: bool = False


class ModelVersionRead(ModelVersionSummary):
    base_model: str | None
    image_size: int
    checksum: str | None
    metrics: dict
    dataset_name: str | None
    dataset_size: int | None
    training_config: dict
    notes: str | None
    #: Cameras currently running this model.
    camera_ids: list[uuid.UUID] = Field(default_factory=list)


class ModelRegisterMeta(BaseModel):
    """Metadata that accompanies an uploaded weights file.

    The primary registration path is the training script calling
    ``models.register_model`` directly; this is for a model trained elsewhere.
    """

    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=40)
    class_names: list[str] = Field(min_length=1, max_length=1000)
    task: ModelTask = ModelTask.DETECTION
    base_model: str | None = Field(default=None, max_length=120)
    image_size: int = Field(default=640, ge=64, le=4096)
    checksum: str | None = Field(default=None, min_length=64, max_length=64)
    map50: float | None = Field(default=None, ge=0, le=1)
    map50_95: float | None = Field(default=None, ge=0, le=1)
    precision: float | None = Field(default=None, ge=0, le=1)
    recall: float | None = Field(default=None, ge=0, le=1)
    dataset_name: str | None = Field(default=None, max_length=200)
    dataset_size: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)

    def metrics(self) -> dict:
        return {
            key: getattr(self, key)
            for key in ("map50", "map50_95", "precision", "recall")
            if getattr(self, key) is not None
        }


class CameraModelAssign(BaseModel):
    model_version_id: uuid.UUID | None = None


def build_model_summary(model, *, min_map50: float) -> ModelVersionSummary:
    data = ModelVersionSummary.model_validate(model)
    data.activatable = model.map50 is not None and model.map50 >= min_map50
    return data


def build_model_read(model, *, min_map50: float, camera_ids: list[uuid.UUID]) -> ModelVersionRead:
    data = ModelVersionRead.model_validate(model)
    data.activatable = model.map50 is not None and model.map50 >= min_map50
    data.camera_ids = camera_ids
    return data
