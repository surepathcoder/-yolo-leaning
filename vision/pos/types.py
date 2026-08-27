"""Provider-agnostic point-of-sale records.

Every POS integration converts into these before anything else sees it. The
correlation logic must not know whether a sale arrived from a CSV export, a
webhook or a vendor REST API, or each new integration would mean new
correlation branches and a new set of bugs.

These carry an anonymous ``cashier_ref``, never a cashier's name. Correlation
needs an identifier to group by; it does not need an identity. A business that
wants to map a reference back to a person can do that in its own HR system,
deliberately. See ``docs/PRIVACY.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class TransactionStatus(StrEnum):
    """Mirrors the backend ``POSTransactionStatus`` without importing it."""

    COMPLETED = "COMPLETED"
    VOIDED = "VOIDED"
    REFUNDED = "REFUNDED"
    PENDING = "PENDING"


@dataclass(frozen=True, slots=True)
class POSLineItem:
    """One line on a sale."""

    name: str
    quantity: Decimal = Decimal(1)
    #: The POS system's own product code. Resolved against the catalogue by
    #: the backend; kept afterwards so the mapping can be re-derived if the
    #: catalogue changes.
    external_product_ref: str | None = None
    unit_price: Decimal | None = None
    total_price: Decimal | None = None
    discount_amount: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "quantity": str(self.quantity),
            "external_product_ref": self.external_product_ref,
            "unit_price": str(self.unit_price) if self.unit_price is not None else None,
            "total_price": str(self.total_price) if self.total_price is not None else None,
            "discount_amount": (
                str(self.discount_amount) if self.discount_amount is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class POSTransactionRecord:
    """One sale, as any provider reports it.

    ``provider`` and ``external_id`` together identify it. Imports are keyed
    on that pair so re-running an export or replaying a webhook cannot create
    a duplicate -- and a duplicated sale would mask a genuine discrepancy by
    "explaining" a shelf change twice.
    """

    external_id: str
    transaction_at: datetime
    provider: str = "unknown"
    status: TransactionStatus = TransactionStatus.COMPLETED
    receipt_number: str | None = None
    total_amount: Decimal = Decimal(0)
    tax_amount: Decimal | None = None
    currency: str = "TZS"
    payment_method: str | None = None
    #: Terminal or lane reference, used to prefer sales rung up near the shelf.
    terminal_ref: str | None = None
    #: Anonymous cashier reference. Not a name -- see the module docstring.
    cashier_ref: str | None = None
    items: tuple[POSLineItem, ...] = ()
    #: The payload exactly as received, for debugging an integration.
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def is_sale(self) -> bool:
        """Did this transaction actually move stock off the shelf?

        A voided or pending transaction did not. Counting one as an
        explanation for a shelf change would hide a real discrepancy behind a
        sale that never completed.
        """
        return self.status is TransactionStatus.COMPLETED

    @property
    def product_refs(self) -> frozenset[str]:
        return frozenset(
            item.external_product_ref for item in self.items if item.external_product_ref
        )

    @property
    def total_quantity(self) -> Decimal:
        return sum((item.quantity for item in self.items), Decimal(0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "provider": self.provider,
            "transaction_at": self.transaction_at.isoformat(),
            "status": self.status.value,
            "receipt_number": self.receipt_number,
            "total_amount": str(self.total_amount),
            "tax_amount": str(self.tax_amount) if self.tax_amount is not None else None,
            "currency": self.currency,
            "payment_method": self.payment_method,
            "terminal_ref": self.terminal_ref,
            "cashier_ref": self.cashier_ref,
            "items": [item.to_dict() for item in self.items],
        }
