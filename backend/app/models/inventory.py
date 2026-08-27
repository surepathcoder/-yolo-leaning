"""Products, shelves and inventory observations."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import (
    GUID,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
)
from app.models.enums import InventoryEventType, OccupancyLevel
from app.models.mixins import enum_column

if TYPE_CHECKING:
    pass


class Product(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A product the organization sells.

    ``detection_class_name`` links a catalogue product to a class emitted by a
    custom-trained model.  It is null until a model is trained that can
    actually recognise the product -- a product without it is still fully
    usable for POS correlation and manual stock, it just is not visually
    detectable.
    """

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("organization_id", "sku", name="uq_product_org_sku"),
        Index("ix_products_org_name", "organization_id", "name"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(120), nullable=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, default="each")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Stored as an exact decimal. Never use a float for money.
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="TZS")

    #: Class name emitted by the model that detects this product, if any.
    detection_class_name: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Stock level at which LOW_STOCK is raised for manual/POS-derived counts.
    low_stock_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<Product {self.sku} {self.name}>"


#: Which products a shelf is *expected* to hold, and how many facings.
shelf_products = Table(
    "shelf_products",
    Base.metadata,
    Column("shelf_id", GUID(), ForeignKey("shelves.id", ondelete="CASCADE"), primary_key=True),
    Column("product_id", GUID(), ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
    Column("expected_facings", Integer, nullable=False, server_default="1"),
)


class Shelf(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A monitored shelf region on a camera view.

    Like a zone, the polygon is normalised 0..1.  ``baseline_occupancy`` is the
    reference "full" state captured during setup; occupancy is measured
    relative to it, because "full" for a shelf of crisps looks nothing like
    "full" for a shelf of crates.
    """

    __tablename__ = "shelves"
    __table_args__ = (
        UniqueConstraint("store_id", "code", name="uq_shelf_store_code"),
        Index("ix_shelves_camera", "camera_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: A shelf may be defined before a camera covers it.
    camera_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Normalised polygon ``[[x, y], ...]``. Null when no camera covers it yet.
    polygon: Mapped[list | None] = mapped_column(JSON, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Fraction of the polygon covered when the shelf is considered full.
    #: Captured during setup, not assumed.
    baseline_occupancy: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Occupancy ratio below which the shelf counts as LOW.
    low_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.35)
    #: Occupancy ratio below which the shelf counts as EMPTY.
    empty_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.12)
    #: Seconds between occupancy samples. Shelves change slowly; sampling every
    #: frame wastes compute and produces noise, not signal.
    sample_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    products: Mapped[list[Product]] = relationship(secondary=shelf_products, lazy="selectin")
    snapshots: Mapped[list[InventorySnapshot]] = relationship(
        back_populates="shelf", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Shelf {self.code} {self.name}>"


class InventorySnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A point-in-time estimate of a shelf's fullness.

    ``occupancy_ratio`` is a measurement with real error bars; ``confidence``
    records how much to trust it.  A snapshot taken while a person occludes the
    shelf is marked ``is_occluded`` and excluded from trend analysis rather
    than silently poisoning it.
    """

    __tablename__ = "inventory_snapshots"
    __table_args__ = (
        Index("ix_snapshots_shelf_captured", "shelf_id", "captured_at"),
        Index("ix_snapshots_org_captured", "organization_id", "captured_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    shelf_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("shelves.id", ondelete="CASCADE"), nullable=False, index=True
    )
    camera_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True
    )

    #: 0..1 fraction of the shelf polygon that appears occupied.
    occupancy_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    occupancy_level: Mapped[OccupancyLevel] = mapped_column(
        enum_column(OccupancyLevel), nullable=False, default=OccupancyLevel.UNKNOWN, index=True
    )
    #: How much to trust this estimate (0..1).
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: True when a person or object blocked the view. Excluded from trends.
    is_occluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Per-product detected counts, ``{"coca_cola_500ml": 6}``. Only populated
    #: when a custom model is active; still an estimate, never an audit count.
    product_counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    detected_object_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    snapshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False, default="occupancy")

    shelf: Mapped[Shelf] = relationship(back_populates="snapshots")

    def __repr__(self) -> str:
        return f"<InventorySnapshot {self.occupancy_level} {self.occupancy_ratio:.2f}>"


class InventoryEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A change observed on a shelf.

    Deliberately describes *movement*, not intent.  ``PRODUCT_REMOVED`` means
    the shelf holds less than it did; it says nothing about who took it or
    whether it was paid for.  Correlating with POS data happens later, in
    :class:`app.models.pos.CorrelationResult`.
    """

    __tablename__ = "inventory_events"
    __table_args__ = (
        Index("ix_inventory_events_org_occurred", "organization_id", "occurred_at"),
        Index("ix_inventory_events_shelf_occurred", "shelf_id", "occurred_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    shelf_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("shelves.id", ondelete="CASCADE"), nullable=False, index=True
    )
    camera_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True
    )
    #: Null when the model can see that *something* changed but not what.
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )

    event_type: Mapped[InventoryEventType] = mapped_column(
        enum_column(InventoryEventType), nullable=False, index=True
    )
    #: Negative for removal, positive for restock. Null when unquantifiable.
    quantity_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    occupancy_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    occupancy_after: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: The person track present when the change was observed, if any. Anonymous.
    track_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    event_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    def __repr__(self) -> str:
        return f"<InventoryEvent {self.event_type} shelf={self.shelf_id}>"


class ProductDetection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A product recognised inside a shelf region by a custom model.

    Kept separate from :class:`app.models.detection.DetectionEvent` because
    product detections come from a *different, customer-specific* model whose
    class ids mean nothing outside that model version.
    """

    __tablename__ = "product_detections"
    __table_args__ = (Index("ix_product_detections_shelf_time", "shelf_id", "detected_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shelf_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("shelves.id", ondelete="CASCADE"), nullable=True, index=True
    )
    camera_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("model_versions.id", ondelete="SET NULL"), nullable=True
    )

    class_name: Mapped[str] = mapped_column(String(80), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_x1: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y1: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_x2: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y2: Mapped[float] = mapped_column(Float, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    def __repr__(self) -> str:
        return f"<ProductDetection {self.class_name} {self.confidence:.2f}>"
