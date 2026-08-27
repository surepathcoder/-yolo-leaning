"""Point-of-sale transactions and camera/POS correlation results."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import GUID, Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from app.models.enums import CorrelationStatus, POSTransactionStatus
from app.models.mixins import enum_column

if TYPE_CHECKING:
    pass


class POSTransaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A sale imported from a point-of-sale system.

    ``provider`` and ``external_id`` together are unique so re-importing the
    same CSV or replaying a webhook cannot create duplicates -- which matters
    because a duplicated sale would mask a genuine discrepancy.
    """

    __tablename__ = "pos_transactions"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_pos_provider_external"),
        Index("ix_pos_tx_store_time", "store_id", "transaction_at"),
        Index("ix_pos_tx_org_time", "organization_id", "transaction_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Registered provider key, e.g. "mock", "csv", "rest".
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    #: The POS system's own identifier for the sale.
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    receipt_number: Mapped[str | None] = mapped_column(String(80), nullable=True)

    transaction_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    status: Mapped[POSTransactionStatus] = mapped_column(
        enum_column(POSTransactionStatus), nullable=False, default=POSTransactionStatus.COMPLETED
    )

    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="TZS")
    payment_method: Mapped[str | None] = mapped_column(String(40), nullable=True)

    #: Terminal/lane reference -- used to pick the checkout zone to correlate against.
    terminal_ref: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    #: Anonymous cashier reference. Deliberately not a name: correlation needs
    #: an identifier, not an identity. See docs/PRIVACY.md.
    cashier_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)

    #: Provider payload as received, for debugging an integration.
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    imported_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    items: Mapped[list[POSItem]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<POSTransaction {self.provider}:{self.external_id}>"


class POSItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One line on a POS transaction."""

    __tablename__ = "pos_items"
    __table_args__ = (Index("ix_pos_items_product", "product_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("pos_transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Resolved catalogue product. Null when the POS SKU is unknown to us --
    #: an ordinary situation that must not break the import.
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )

    #: The POS system's SKU/product code, kept even after resolution so the
    #: mapping can be re-derived if the catalogue changes.
    external_product_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=1)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    transaction: Mapped[POSTransaction] = relationship(back_populates="items")

    def __repr__(self) -> str:
        return f"<POSItem {self.name} x{self.quantity}>"


class CorrelationResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The outcome of matching an inventory observation against POS data.

    Three outcomes, and the third is the important one:

    * ``MATCHED``   -- a sale plausibly explains the shelf change.
    * ``UNMATCHED`` -- no sale was found in the window. **This is not theft.**
      Restocking, staff moving stock, a mis-scanned item, a POS outage and a
      false detection all land here.
    * ``UNCERTAIN`` -- evidence is ambiguous, or POS data for the window is
      incomplete.

    ``UNMATCHED`` results feed a ``POSSIBLE_INVENTORY_DISCREPANCY`` inference
    only when configured thresholds are met, and that inference is always
    presented as something for a human to check.
    """

    __tablename__ = "correlation_results"
    __table_args__ = (
        Index("ix_correlation_org_status", "organization_id", "status"),
        Index("ix_correlation_inventory_event", "inventory_event_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)

    inventory_event_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("inventory_events.id", ondelete="CASCADE"), nullable=False
    )
    #: The matched sale, when there is one.
    pos_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("pos_transactions.id", ondelete="SET NULL"), nullable=True
    )
    #: The inference event this result produced, if it produced one.
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[CorrelationStatus] = mapped_column(
        enum_column(CorrelationStatus), nullable=False, index=True
    )
    #: 0..1 match strength. Only meaningful for MATCHED and UNCERTAIN.
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Seconds either side of the observation that were searched.
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    #: How many candidate transactions fell in the window.
    candidates_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Per-signal contributions, ``{"time": 0.9, "product": 1.0, "quantity": 0.5}``.
    #: Makes the score auditable rather than a magic number.
    score_breakdown: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: Plain-language explanation shown to the reviewer.
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")

    correlated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    #: A human's verdict once reviewed. Null = not reviewed. This is the
    #: feedback loop that lets correlation accuracy be measured.
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    review_outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<CorrelationResult {self.status} score={self.score:.2f}>"
