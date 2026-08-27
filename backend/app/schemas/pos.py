"""POS and correlation schemas.

A correlation result is the most easily misread payload this API returns, so
its response carries its own reasoning: the per-signal `score_breakdown`, the
number of candidates considered, the window searched, and a plain-language
`explanation`. A consumer that renders only `status` will show "UNMATCHED" with
no context, and UNMATCHED read without context is the failure this whole
subsystem is designed around.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import CorrelationStatus, POSTransactionStatus
from app.schemas.common import APISchema


class POSItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    external_product_ref: str | None = Field(default=None, max_length=120)
    quantity: Decimal = Field(default=Decimal(1), ge=0, max_digits=12, decimal_places=3)
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    total_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    discount_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)


class POSTransactionIn(BaseModel):
    """One sale being imported.

    ``transaction_at`` must carry a timezone. A naive timestamp would be read
    as UTC and could be hours out, and correlation runs on a two-minute
    window -- silent drift would make every match wrong in the same direction,
    which is worse than making none.
    """

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=120)
    transaction_at: datetime
    status: POSTransactionStatus = POSTransactionStatus.COMPLETED
    receipt_number: str | None = Field(default=None, max_length=80)
    total_amount: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    tax_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="TZS", pattern="^[A-Z]{3}$")
    payment_method: str | None = Field(default=None, max_length=40)
    terminal_ref: str | None = Field(default=None, max_length=80)
    #: Anonymous. Not a cashier's name -- see docs/PRIVACY.md.
    cashier_ref: str | None = Field(default=None, max_length=80)
    items: list[POSItemIn] = Field(default_factory=list, max_length=500)

    @field_validator("transaction_at")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError(
                "transaction_at must include a timezone offset, "
                "otherwise sales cannot be aligned with camera observations"
            )
        return value


class POSImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: uuid.UUID
    provider: str = Field(default="manual", max_length=40)
    transactions: list[POSTransactionIn] = Field(min_length=1, max_length=1000)


class POSImportResult(APISchema):
    created: int
    #: Already imported. Not an error: re-running an export is normal, and
    #: skipping is what stops a duplicate from explaining a change twice.
    skipped: int
    store_id: uuid.UUID
    provider: str


class POSItemRead(APISchema):
    id: uuid.UUID
    product_id: uuid.UUID | None
    external_product_ref: str | None
    name: str
    quantity: Decimal
    unit_price: Decimal | None
    total_price: Decimal | None
    discount_amount: Decimal | None


class POSTransactionRead(APISchema):
    id: uuid.UUID
    store_id: uuid.UUID
    provider: str
    external_id: str
    receipt_number: str | None
    transaction_at: datetime
    status: POSTransactionStatus
    total_amount: Decimal
    currency: str
    payment_method: str | None
    terminal_ref: str | None
    cashier_ref: str | None
    imported_at: datetime | None
    items: list[POSItemRead] = Field(default_factory=list)


class CorrelationRead(APISchema):
    """The outcome of matching a shelf observation against sales.

    ``status`` alone is not the answer. ``UNMATCHED`` means no sale was found
    in the window -- a statement about the data, not about a person. Render
    ``explanation`` alongside it.
    """

    id: uuid.UUID
    store_id: uuid.UUID
    inventory_event_id: uuid.UUID
    pos_transaction_id: uuid.UUID | None
    event_id: uuid.UUID | None
    status: CorrelationStatus
    score: float
    window_seconds: int
    candidates_considered: int
    score_breakdown: dict
    explanation: str
    correlated_at: datetime
    reviewed_by_id: uuid.UUID | None
    reviewed_at: datetime | None
    review_outcome: str | None
    review_notes: str | None


class CorrelationReview(BaseModel):
    """A human's verdict on whether the correlation was right.

    About the match, never about a person: the options are CORRECT, INCORRECT,
    EXPLAINED and UNCLEAR.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: str = Field(max_length=40)
    notes: str | None = Field(default=None, max_length=2000)


class CorrelationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: uuid.UUID | None = None
    since: datetime | None = None
    limit: int = Field(default=100, ge=1, le=500)


class CorrelationAccuracy(APISchema):
    """How correlation is performing, according to its reviewers."""

    total: int
    matched: int
    unmatched: int
    uncertain: int
    reviewed: int
    review_correct: int
    review_incorrect: int
    review_explained: int
    #: Null until a human has judged at least one result. An unmeasured system
    #: must not report "0% accurate", nor "100%".
    accuracy: float | None


class POSConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, max_length=40)
    options: dict = Field(default_factory=dict)


class POSConfigRead(APISchema):
    store_id: uuid.UUID
    provider: str | None
    options: dict
    #: Provider keys this build supports.
    available_providers: list[str]
