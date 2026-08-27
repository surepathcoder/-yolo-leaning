"""The POS provider abstraction.

Small shops in the target market run wildly different tills: a cloud POS with
an API, a spreadsheet exported nightly, a vendor system that can POST a
webhook, or nothing at all. The abstraction is deliberately narrow -- fetch
sales in a time range -- because that is the only thing correlation needs, and
a wider interface would have to be implemented badly by every provider that
cannot support it.

**Coverage is part of the contract.** A provider must report not just the
sales it found but whether it could see the whole window at all. Without that,
a till that was offline for an hour is indistinguishable from an hour with no
sales -- and the second reading would turn every shelf change in that hour
into an unexplained discrepancy. That is the single worst failure this
subsystem can have, so :class:`FetchResult` carries ``complete`` and nothing
may infer a discrepancy from an incomplete window.
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from vision.pos.types import POSLineItem, POSTransactionRecord, TransactionStatus


class POSError(Exception):
    """A provider could not deliver data."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Sales for a window, plus whether the window was fully covered."""

    transactions: tuple[POSTransactionRecord, ...] = ()
    #: False when the provider knows it could not see the whole window: the
    #: till was offline, the export was truncated, the API paged out. An
    #: incomplete window may never produce a discrepancy inference.
    complete: bool = True
    #: Why coverage is incomplete, for the reviewer and the logs.
    reason: str | None = None

    def __len__(self) -> int:
        return len(self.transactions)

    @property
    def sales(self) -> tuple[POSTransactionRecord, ...]:
        """Only transactions that actually moved stock."""
        return tuple(tx for tx in self.transactions if tx.is_sale)


class POSProvider(ABC):
    """Something that can supply sales for a time window."""

    #: Registered key, stored on every transaction this provider imports.
    key: str = "unknown"

    @abstractmethod
    def fetch(self, since: datetime, until: datetime) -> FetchResult:
        """Return sales with ``since <= transaction_at <= until``."""

    def health(self) -> tuple[bool, str]:
        """Can this provider be reached? ``(ok, detail)``."""
        return True, "not implemented"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} key={self.key!r}>"


class InMemoryPOSProvider(POSProvider):
    """Sales held in memory.

    The provider used by tests and by the demo environment, and the reference
    for what a real one must do. Kept in the shipped package rather than in
    the test tree so an integrator has a working example to read.
    """

    key = "mock"

    def __init__(
        self,
        transactions: list[POSTransactionRecord] | None = None,
        *,
        complete: bool = True,
        reason: str | None = None,
    ) -> None:
        self._transactions = list(transactions or [])
        self._complete = complete
        self._reason = reason

    def add(self, transaction: POSTransactionRecord) -> None:
        self._transactions.append(transaction)

    def set_coverage(self, complete: bool, reason: str | None = None) -> None:
        """Simulate a till outage, which is the case that matters most."""
        self._complete = complete
        self._reason = reason

    def fetch(self, since: datetime, until: datetime) -> FetchResult:
        found = tuple(tx for tx in self._transactions if since <= tx.transaction_at <= until)
        return FetchResult(transactions=found, complete=self._complete, reason=self._reason)


#: Column names accepted by the CSV provider, in the order they are tried.
#: Small shops export from wildly inconsistent tools, so a handful of common
#: spellings are accepted rather than demanding one exact header.
CSV_FIELDS: dict[str, tuple[str, ...]] = {
    "external_id": ("external_id", "id", "transaction_id", "receipt_id"),
    "transaction_at": ("transaction_at", "datetime", "timestamp", "date", "time"),
    "receipt_number": ("receipt_number", "receipt", "receipt_no"),
    "total_amount": ("total_amount", "total", "amount", "grand_total"),
    "currency": ("currency", "ccy"),
    "payment_method": ("payment_method", "payment", "tender"),
    "terminal_ref": ("terminal_ref", "terminal", "till", "lane", "register"),
    "cashier_ref": ("cashier_ref", "cashier", "operator", "staff_id"),
    "status": ("status", "state"),
    "product_ref": ("product_ref", "sku", "product_code", "item_code", "barcode"),
    "product_name": ("product_name", "item", "name", "description"),
    "quantity": ("quantity", "qty", "units"),
    "unit_price": ("unit_price", "price"),
    "line_total": ("line_total", "line_amount", "item_total"),
}


@dataclass
class CSVPOSProvider(POSProvider):
    """Sales from a CSV export.

    The realistic integration for a shop whose till has no API: one row per
    line item, rows sharing a transaction id belong to one sale.

    A row that cannot be parsed is skipped and counted rather than aborting
    the import. A single malformed line in a nightly export must not cost the
    whole night's sales -- but the window is then marked **incomplete**, so the
    rows that were lost cannot silently become unexplained shelf changes.
    """

    path: Path
    key: str = "csv"
    #: Rows whose timestamp carries no zone are read as this offset from UTC.
    #: Defaults to naive-is-UTC; set it for a till exporting local time.
    assume_tzinfo: Any = None
    skipped: list[str] = field(default_factory=list)

    def _column(self, row: dict[str, str], field_name: str) -> str | None:
        for candidate in CSV_FIELDS[field_name]:
            for key in row:
                if key and key.strip().lower() == candidate:
                    value = (row[key] or "").strip()
                    if value:
                        return value
        return None

    def _parse_time(self, value: str) -> datetime:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            if self.assume_tzinfo is None:
                raise POSError(f"timestamp {value!r} has no timezone and assume_tzinfo is unset")
            moment = moment.replace(tzinfo=self.assume_tzinfo)
        return moment

    def _decimal(self, value: str | None, default: Decimal | None = None) -> Decimal | None:
        if value is None:
            return default
        try:
            return Decimal(value.replace(",", ""))
        except (InvalidOperation, ValueError) as exc:
            raise POSError(f"{value!r} is not a number") from exc

    def fetch(self, since: datetime, until: datetime) -> FetchResult:
        if not self.path.exists():
            raise POSError(f"POS export not found: {self.path}")

        grouped: dict[str, dict[str, Any]] = {}
        self.skipped = []

        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            for number, row in enumerate(csv.DictReader(handle), start=2):
                try:
                    external_id = self._column(row, "external_id")
                    raw_time = self._column(row, "transaction_at")
                    if not external_id or not raw_time:
                        raise POSError("missing transaction id or timestamp")
                    moment = self._parse_time(raw_time)

                    entry = grouped.setdefault(
                        external_id,
                        {
                            "external_id": external_id,
                            "transaction_at": moment,
                            "receipt_number": self._column(row, "receipt_number"),
                            "total_amount": self._decimal(
                                self._column(row, "total_amount"), Decimal(0)
                            ),
                            "currency": self._column(row, "currency") or "TZS",
                            "payment_method": self._column(row, "payment_method"),
                            "terminal_ref": self._column(row, "terminal_ref"),
                            "cashier_ref": self._column(row, "cashier_ref"),
                            "status": self._status(self._column(row, "status")),
                            "items": [],
                            "raw_payload": {"source": str(self.path)},
                        },
                    )
                    name = self._column(row, "product_name")
                    ref = self._column(row, "product_ref")
                    if name or ref:
                        entry["items"].append(
                            POSLineItem(
                                name=name or ref or "item",
                                external_product_ref=ref,
                                quantity=self._decimal(self._column(row, "quantity"), Decimal(1))
                                or Decimal(1),
                                unit_price=self._decimal(self._column(row, "unit_price")),
                                total_price=self._decimal(self._column(row, "line_total")),
                            )
                        )
                except (POSError, ValueError) as exc:
                    self.skipped.append(f"row {number}: {exc}")

        transactions = tuple(
            POSTransactionRecord(
                provider=self.key,
                external_id=entry["external_id"],
                transaction_at=entry["transaction_at"],
                receipt_number=entry["receipt_number"],
                total_amount=entry["total_amount"] or Decimal(0),
                currency=entry["currency"],
                payment_method=entry["payment_method"],
                terminal_ref=entry["terminal_ref"],
                cashier_ref=entry["cashier_ref"],
                status=entry["status"],
                items=tuple(entry["items"]),
                raw_payload=entry["raw_payload"],
            )
            for entry in grouped.values()
            if since <= entry["transaction_at"] <= until
        )

        return FetchResult(
            transactions=transactions,
            complete=not self.skipped,
            reason=(
                f"{len(self.skipped)} row(s) in the export could not be read"
                if self.skipped
                else None
            ),
        )

    @staticmethod
    def _status(value: str | None) -> TransactionStatus:
        if not value:
            return TransactionStatus.COMPLETED
        try:
            return TransactionStatus(value.strip().upper())
        except ValueError:
            # An unrecognised status is not assumed to be a completed sale:
            # treating an unknown state as "sold" would let it explain away a
            # shelf change it may have nothing to do with.
            return TransactionStatus.PENDING

    def health(self) -> tuple[bool, str]:
        if not self.path.exists():
            return False, f"export not found: {self.path}"
        return True, f"reading {self.path}"


#: Providers available by key. The backend resolves a store's configured
#: provider through this, so adding an integration is one registration.
PROVIDERS: dict[str, type[POSProvider]] = {
    InMemoryPOSProvider.key: InMemoryPOSProvider,
    CSVPOSProvider.key: CSVPOSProvider,
}
