"""Product, shelf and inventory schemas.

Shelf polygons are **normalised 0..1**, on the same reasoning as zones: the
shelf an installer drew on a 1920x1080 preview must stay correct when the
pipeline drops to a sub-stream.

Occupancy is exposed as a ratio and a coarse level, never as a unit count.
The vision engine cannot count units on a packed shelf, and a response field
called ``quantity`` would invite every consumer to believe it could.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import InventoryEventType, OccupancyLevel
from app.schemas.common import APISchema

NormalisedCoordinate = Annotated[float, Field(ge=0.0, le=1.0)]
Point = Annotated[list[NormalisedCoordinate], Field(min_length=2, max_length=2)]

Ratio = Annotated[float, Field(ge=0.0, le=1.0)]


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


class ProductBase(BaseModel):
    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=120)
    barcode: str | None = Field(default=None, max_length=64)
    unit: str = Field(default="each", max_length=20)
    description: str | None = Field(default=None, max_length=2000)
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="TZS", pattern="^[A-Z]{3}$")
    #: The class name a custom-trained model emits for this product. Null
    #: until such a model exists -- see milestone 15.
    detection_class_name: str | None = Field(default=None, max_length=80)
    low_stock_threshold: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("sku")
    @classmethod
    def _clean_sku(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("sku cannot be blank")
        return cleaned


class ProductCreate(ProductBase):
    model_config = ConfigDict(extra="forbid")


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str | None = Field(default=None, min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=120)
    barcode: str | None = Field(default=None, max_length=64)
    unit: str | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=2000)
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str | None = Field(default=None, pattern="^[A-Z]{3}$")
    detection_class_name: str | None = Field(default=None, max_length=80)
    low_stock_threshold: int | None = Field(default=None, ge=0, le=1_000_000)
    is_active: bool | None = None


class ProductRead(APISchema):
    id: uuid.UUID
    organization_id: uuid.UUID
    sku: str
    name: str
    brand: str | None
    category: str | None
    barcode: str | None
    unit: str
    description: str | None
    unit_price: Decimal | None
    currency: str
    detection_class_name: str | None
    image_path: str | None
    low_stock_threshold: int | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Shelves
# ---------------------------------------------------------------------------


class ShelfBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=40)
    location: str | None = Field(default=None, max_length=200)
    #: At least three points, normalised 0..1. Null means the whole frame,
    #: which is only sensible for a camera pointed at one shelf.
    polygon: list[Point] | None = Field(default=None, min_length=3, max_length=64)

    #: Coverage measured when the shelf is known full. Null until calibrated;
    #: raw coverage is then reported uncorrected.
    baseline_occupancy: Ratio | None = None
    low_threshold: Ratio = 0.35
    empty_threshold: Ratio = 0.12
    sample_interval_seconds: int = Field(default=60, ge=5, le=3600)
    monitoring_enabled: bool = True

    @field_validator("code")
    @classmethod
    def _clean_code(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("code cannot be blank")
        return cleaned


class ShelfCreate(ShelfBase):
    model_config = ConfigDict(extra="forbid")

    store_id: uuid.UUID
    #: The camera that watches this shelf. Optional so a shelf can be planned
    #: before the camera covering it is installed; it reports UNKNOWN until
    #: one is attached.
    camera_id: uuid.UUID | None = None
    #: Products expected on this shelf. Optional: an uncalibrated shelf still
    #: reports occupancy, it just cannot say what is on it.
    product_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


class ShelfUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, min_length=1, max_length=40)
    location: str | None = Field(default=None, max_length=200)
    camera_id: uuid.UUID | None = None
    polygon: list[Point] | None = Field(default=None, min_length=3, max_length=64)
    baseline_occupancy: Ratio | None = None
    low_threshold: Ratio | None = None
    empty_threshold: Ratio | None = None
    sample_interval_seconds: int | None = Field(default=None, ge=5, le=3600)
    monitoring_enabled: bool | None = None
    is_active: bool | None = None
    product_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)


class ShelfRead(APISchema):
    id: uuid.UUID
    organization_id: uuid.UUID
    store_id: uuid.UUID
    camera_id: uuid.UUID | None
    name: str
    code: str
    location: str | None
    polygon: list | None
    baseline_occupancy: float | None
    low_threshold: float
    empty_threshold: float
    sample_interval_seconds: int
    monitoring_enabled: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    products: list[ProductRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Readings and events
# ---------------------------------------------------------------------------


class InventorySnapshotRead(APISchema):
    """One measurement of one shelf.

    ``is_occluded`` is part of the contract, not a debugging field: a snapshot
    taken while somebody stood at the shelf carries an ``UNKNOWN`` level, and
    a consumer that ignores the flag will read that as a stockout.
    """

    id: uuid.UUID
    shelf_id: uuid.UUID
    camera_id: uuid.UUID | None
    occupancy_ratio: float
    occupancy_level: OccupancyLevel
    confidence: float
    is_occluded: bool
    product_counts: dict
    detected_object_count: int | None
    snapshot_path: str | None
    captured_at: datetime
    method: str


class InventoryEventRead(APISchema):
    """A confirmed change in a shelf's state.

    An observation about a shelf, not about a person. ``track_key`` is
    anonymous and local to one camera session; it is context for a human
    reviewer, never an attribution of who moved the stock.
    """

    id: uuid.UUID
    shelf_id: uuid.UUID
    camera_id: uuid.UUID | None
    product_id: uuid.UUID | None
    event_type: InventoryEventType
    quantity_delta: int | None
    confidence: float
    occupancy_before: float | None
    occupancy_after: float | None
    track_key: str | None
    occurred_at: datetime
    event_metadata: dict


class ProductDetectionRead(APISchema):
    """One product recognised by a custom model (milestone 15).

    ``product_id`` is null when the model's class name does not match any
    catalogue product's ``detection_class_name`` -- the box is still recorded,
    it just is not linked to a SKU yet.
    """

    id: uuid.UUID
    camera_id: uuid.UUID | None
    shelf_id: uuid.UUID | None
    product_id: uuid.UUID | None
    model_version_id: uuid.UUID | None
    class_name: str
    confidence: float
    bbox_x1: float
    bbox_y1: float
    bbox_x2: float
    bbox_y2: float
    detected_at: datetime


class ShelfStatus(APISchema):
    """A shelf's latest known state, for the dashboard."""

    shelf_id: uuid.UUID
    name: str
    code: str
    camera_id: uuid.UUID | None
    monitoring_enabled: bool
    occupancy_ratio: float | None
    occupancy_level: OccupancyLevel
    confidence: float | None
    is_occluded: bool
    #: Null when the shelf has never been read -- a new shelf, or a camera
    #: that has not run since it was drawn. Distinct from "read as empty".
    last_reading_at: datetime | None


class InventorySummary(APISchema):
    """Counts for the inventory overview."""

    shelves_total: int
    shelves_monitored: int
    #: Shelves whose latest usable reading was at or below their thresholds.
    shelves_low: int
    shelves_empty: int
    #: Shelves currently reporting UNKNOWN, including never-read ones. Shown
    #: rather than hidden: "we cannot see 6 of your shelves" is information
    #: the operator needs.
    shelves_unknown: int
    products_total: int
    events_24h: int


class SnapshotIngest(BaseModel):
    """A reading submitted by a pipeline.

    Pipelines run on the customer's premises and post readings back, so this
    is a write path from a partly-trusted source: every field is bounded here
    rather than trusted to be sane.
    """

    model_config = ConfigDict(extra="forbid")

    occupancy_ratio: Ratio
    occupancy_level: OccupancyLevel
    confidence: Ratio = 0.0
    is_occluded: bool = False
    product_counts: dict[str, int] = Field(default_factory=dict)
    detected_object_count: int | None = Field(default=None, ge=0, le=100_000)
    captured_at: datetime
    method: str = Field(default="occupancy", max_length=40)
