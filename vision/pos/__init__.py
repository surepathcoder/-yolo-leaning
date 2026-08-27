"""Point-of-sale integration and camera/POS correlation.

Reports whether a sale explains a shelf change. It never reports why one does
not -- see ``correlation.py`` and ``docs/PRIVACY.md``.
"""

from vision.pos.correlation import (
    DEFAULT_MATCH_THRESHOLD,
    DEFAULT_UNCERTAIN_THRESHOLD,
    DEFAULT_WINDOW_SECONDS,
    CorrelationConfig,
    CorrelationOutcome,
    CorrelationStatus,
    ShelfChange,
    correlate,
    score_transaction,
)
from vision.pos.provider import (
    PROVIDERS,
    CSVPOSProvider,
    FetchResult,
    InMemoryPOSProvider,
    POSError,
    POSProvider,
)
from vision.pos.types import POSLineItem, POSTransactionRecord, TransactionStatus

__all__ = [
    "DEFAULT_MATCH_THRESHOLD",
    "DEFAULT_UNCERTAIN_THRESHOLD",
    "DEFAULT_WINDOW_SECONDS",
    "PROVIDERS",
    "CSVPOSProvider",
    "CorrelationConfig",
    "CorrelationOutcome",
    "CorrelationStatus",
    "FetchResult",
    "InMemoryPOSProvider",
    "POSError",
    "POSLineItem",
    "POSProvider",
    "POSTransactionRecord",
    "ShelfChange",
    "TransactionStatus",
    "correlate",
    "score_transaction",
]
