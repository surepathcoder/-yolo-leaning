"""Matching shelf observations against sales.

This is the most dangerous code in VisionGuard, so it is also the most
conservative.

The input is an observation -- stock left a shelf at 14:31 -- and the sales
near that moment. The output says whether a sale plausibly explains it. When
nothing does, the honest report is **"no sale was found"**, which is a
statement about the data. It is emphatically not a statement about a person.

The gap between those two readings is where this kind of product goes wrong.
An unexplained shelf change has many ordinary causes, and in a real shop the
innocent ones are far more common than the rest:

* staff restocking, facing up, or moving stock between displays
* a customer picking an item up and putting it back in another aisle
* an item scanned under a different SKU, or rung up at another till
* the POS clock and the camera clock disagreeing by a minute
* the till being offline for the window
* the detector simply being wrong about this frame

So: no function here returns a verdict about intent, ``UNMATCHED`` is never
phrased as an accusation, and an inference is only ever produced with the
observations it rests on attached, hedged in its own name
(``POSSIBLE_INVENTORY_DISCREPANCY``), for a human to check.

**The rule that matters most:** if POS coverage for the window is incomplete,
the result is ``UNCERTAIN`` and no discrepancy may be inferred. A till that was
switched off must never turn an afternoon of ordinary trading into a list of
suspicions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from vision.pos.provider import FetchResult
from vision.pos.types import POSTransactionRecord

#: Seconds either side of an observation that are searched.
DEFAULT_WINDOW_SECONDS = 120

#: Sales happen *after* stock leaves the shelf: a customer picks an item up,
#: walks to the till and queues. So the window is asymmetric -- much more time
#: is allowed after the observation than before it. A symmetric window either
#: misses the queue or invites matches from before the item was even touched.
DEFAULT_LOOKBACK_RATIO = 0.25

#: At or above this, a sale is taken to explain the change.
DEFAULT_MATCH_THRESHOLD = 0.65

#: Between this and the match threshold the evidence is real but weak.
DEFAULT_UNCERTAIN_THRESHOLD = 0.35

#: How much each signal contributes. Time dominates because it is the only
#: signal that is always available and always meaningful. Quantity is weighted
#: low deliberately: occupancy is a coverage ratio, so inferring "two units"
#: from a 20% drop is guesswork, and it must not be able to carry a match on
#: its own.
DEFAULT_WEIGHTS: dict[str, float] = {
    "time": 0.5,
    "product": 0.35,
    "quantity": 0.15,
}


class CorrelationStatus(StrEnum):
    """Mirrors the backend enum without importing it."""

    MATCHED = "MATCHED"
    UNMATCHED = "UNMATCHED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class ShelfChange:
    """The observation being explained.

    Deliberately not the ORM row: correlation stays runnable with no database,
    and the fields here are the only ones the matching actually uses.
    """

    shelf_id: str
    occurred_at: datetime
    #: Change in occupancy. Negative means stock left the shelf.
    delta: float
    store_id: str | None = None
    camera_id: str | None = None
    shelf_name: str = ""
    #: POS product refs for the products stocked on this shelf. Empty when the
    #: shelf has no catalogue mapping, which makes the product signal
    #: unavailable rather than failed.
    expected_product_refs: frozenset[str] = frozenset()
    #: Terminal nearest this shelf, where the layout is known.
    expected_terminal_ref: str | None = None
    #: Anonymous tracks near the shelf. Context for a human only -- never an
    #: input to the score. See docs/PRIVACY.md.
    nearby_track_keys: tuple[str, ...] = ()

    @property
    def is_removal(self) -> bool:
        return self.delta < 0


@dataclass
class CorrelationConfig:
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    lookback_ratio: float = DEFAULT_LOOKBACK_RATIO
    match_threshold: float = DEFAULT_MATCH_THRESHOLD
    uncertain_threshold: float = DEFAULT_UNCERTAIN_THRESHOLD
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    #: Occupancy drop taken to represent roughly one unit. Only used to sanity
    #: check quantity, never to report a number of units.
    unit_occupancy_hint: float = 0.15

    @property
    def lookback(self) -> timedelta:
        return timedelta(seconds=self.window_seconds * self.lookback_ratio)

    @property
    def lookahead(self) -> timedelta:
        return timedelta(seconds=self.window_seconds)


@dataclass(frozen=True, slots=True)
class CorrelationOutcome:
    """What correlation concluded, and why.

    ``explanation`` is written for a shop manager, not an engineer, and is
    phrased so it cannot be quoted as an accusation.
    """

    status: CorrelationStatus
    score: float
    explanation: str
    window_seconds: int
    candidates_considered: int
    #: Per-signal contributions, so the score is auditable rather than magic.
    score_breakdown: dict[str, float] = field(default_factory=dict)
    matched_transaction: POSTransactionRecord | None = None
    #: True when a discrepancy inference may be raised from this result.
    #: Always False for MATCHED and for any incomplete window.
    supports_inference: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "score": round(self.score, 4),
            "explanation": self.explanation,
            "window_seconds": self.window_seconds,
            "candidates_considered": self.candidates_considered,
            "score_breakdown": {k: round(v, 4) for k, v in self.score_breakdown.items()},
            "matched_external_id": (
                self.matched_transaction.external_id if self.matched_transaction else None
            ),
            "supports_inference": self.supports_inference,
        }


def _time_score(change: ShelfChange, tx: POSTransactionRecord, config: CorrelationConfig) -> float:
    """1.0 at the observation, falling to 0 at the window edge.

    Asymmetric: a sale shortly *after* stock left the shelf is the expected
    ordering, so it decays gently. A sale *before* the shelf changed is far
    less explanatory and decays fast.
    """
    offset = (tx.transaction_at - change.occurred_at).total_seconds()
    limit = config.lookahead.total_seconds() if offset >= 0 else config.lookback.total_seconds()
    if limit <= 0:
        return 0.0
    ratio = abs(offset) / limit
    return max(0.0, 1.0 - ratio)


def _product_score(change: ShelfChange, tx: POSTransactionRecord) -> float | None:
    """Did the sale include something this shelf stocks?

    Returns ``None`` when the signal is **unavailable** -- the shelf has no
    catalogue mapping, or the sale has no line items. Unavailable is not zero:
    scoring a missing signal as a failure would push every shelf in an
    uncatalogued shop towards "unexplained".
    """
    if not change.expected_product_refs or not tx.items:
        return None
    return 1.0 if change.expected_product_refs & tx.product_refs else 0.0


def _quantity_score(
    change: ShelfChange, tx: POSTransactionRecord, config: CorrelationConfig
) -> float | None:
    """Is the size of the shelf change consistent with the quantity sold?

    A weak signal, and treated as one. Occupancy is a coverage ratio, so the
    implied unit count is an estimate built on an estimate. It can support a
    match; it must never carry one.
    """
    if not tx.items or config.unit_occupancy_hint <= 0:
        return None
    sold = float(tx.total_quantity or Decimal(0))
    if sold <= 0:
        return None
    implied = abs(change.delta) / config.unit_occupancy_hint
    if implied <= 0:
        return None
    ratio = min(implied, sold) / max(implied, sold)
    return max(0.0, min(1.0, ratio))


def score_transaction(
    change: ShelfChange, tx: POSTransactionRecord, config: CorrelationConfig
) -> tuple[float, dict[str, float]]:
    """Score one candidate sale against one shelf change.

    Unavailable signals are dropped from both the numerator and the divisor,
    so a shop with no product catalogue is judged on the signals it does have
    rather than being penalised for the ones it cannot provide.
    """
    signals: dict[str, float | None] = {
        "time": _time_score(change, tx, config),
        "product": _product_score(change, tx),
        "quantity": _quantity_score(change, tx, config),
    }

    breakdown: dict[str, float] = {}
    total = 0.0
    weight_used = 0.0
    for name, value in signals.items():
        if value is None:
            continue
        weight = config.weights.get(name, 0.0)
        breakdown[name] = value
        total += value * weight
        weight_used += weight

    score = total / weight_used if weight_used else 0.0

    # A terminal match is a tie-breaker, not a signal in its own right: shops
    # rearrange tills, and the mapping is often stale.
    if change.expected_terminal_ref and tx.terminal_ref == change.expected_terminal_ref:
        score = min(1.0, score + 0.05)
        breakdown["terminal"] = 1.0

    return score, breakdown


def correlate(
    change: ShelfChange,
    fetched: FetchResult,
    config: CorrelationConfig | None = None,
) -> CorrelationOutcome:
    """Decide whether a sale explains a shelf change.

    Only removals are correlated. A shelf gaining stock is a delivery or a
    restock, and looking for a sale to explain it would be nonsense.
    """
    config = config or CorrelationConfig()

    if not change.is_removal:
        return CorrelationOutcome(
            status=CorrelationStatus.UNCERTAIN,
            score=0.0,
            explanation=(
                "Stock was added to this shelf, so there is no sale to match it "
                "against. This is what restocking looks like."
            ),
            window_seconds=config.window_seconds,
            candidates_considered=0,
        )

    window_start = change.occurred_at - config.lookback
    window_end = change.occurred_at + config.lookahead
    candidates = [tx for tx in fetched.sales if window_start <= tx.transaction_at <= window_end]

    # The rule that matters most. An incomplete window cannot support a
    # discrepancy, whether or not a candidate happened to be found in the part
    # of it we could see.
    if not fetched.complete:
        detail = fetched.reason or "sales data for this period is incomplete"
        best = _best(change, candidates, config)
        if best and best[1] >= config.match_threshold:
            tx, score, breakdown = best
            return CorrelationOutcome(
                status=CorrelationStatus.MATCHED,
                score=score,
                explanation=(
                    f"Matched to sale {tx.external_id} "
                    f"{_offset_phrase(change, tx)}. Note that {detail}."
                ),
                window_seconds=config.window_seconds,
                candidates_considered=len(candidates),
                score_breakdown=breakdown,
                matched_transaction=tx,
            )
        return CorrelationOutcome(
            status=CorrelationStatus.UNCERTAIN,
            score=best[1] if best else 0.0,
            explanation=(
                f"Cannot say whether a sale explains this: {detail}. "
                "No conclusion should be drawn from the absence of a matching "
                "sale while the till data is incomplete."
            ),
            window_seconds=config.window_seconds,
            candidates_considered=len(candidates),
            score_breakdown=best[2] if best else {},
        )

    if not candidates:
        return CorrelationOutcome(
            status=CorrelationStatus.UNMATCHED,
            score=0.0,
            explanation=(
                f"No sale was recorded within {config.window_seconds} seconds of this "
                f"change on {change.shelf_name or 'this shelf'}. Restocking, staff "
                "moving stock, an item put back elsewhere, a mis-scanned item or a "
                "detection error all produce this result."
            ),
            window_seconds=config.window_seconds,
            candidates_considered=0,
            supports_inference=True,
        )

    tx, score, breakdown = _best(change, candidates, config)

    if score >= config.match_threshold:
        return CorrelationOutcome(
            status=CorrelationStatus.MATCHED,
            score=score,
            explanation=(f"Matched to sale {tx.external_id} {_offset_phrase(change, tx)}."),
            window_seconds=config.window_seconds,
            candidates_considered=len(candidates),
            score_breakdown=breakdown,
            matched_transaction=tx,
        )

    if score >= config.uncertain_threshold:
        return CorrelationOutcome(
            status=CorrelationStatus.UNCERTAIN,
            score=score,
            explanation=(
                f"{len(candidates)} sale(s) were recorded nearby but none clearly "
                f"matches this change. The closest is {tx.external_id} "
                f"{_offset_phrase(change, tx)}."
            ),
            window_seconds=config.window_seconds,
            candidates_considered=len(candidates),
            score_breakdown=breakdown,
        )

    return CorrelationOutcome(
        status=CorrelationStatus.UNMATCHED,
        score=score,
        explanation=(
            f"{len(candidates)} sale(s) were recorded nearby but none is a plausible "
            "explanation for this change. Restocking, staff moving stock, an item "
            "put back elsewhere, a mis-scanned item or a detection error all "
            "produce this result."
        ),
        window_seconds=config.window_seconds,
        candidates_considered=len(candidates),
        score_breakdown=breakdown,
        supports_inference=True,
    )


def _best(
    change: ShelfChange,
    candidates: list[POSTransactionRecord],
    config: CorrelationConfig,
) -> tuple[POSTransactionRecord, float, dict[str, float]] | None:
    """The highest-scoring candidate, or ``None`` when there are none."""
    if not candidates:
        return None
    scored = [(tx, *score_transaction(change, tx, config)) for tx in candidates]
    # Ties break towards the sale closest in time -- the more explanatory one.
    scored.sort(
        key=lambda row: (
            -row[1],
            abs((row[0].transaction_at - change.occurred_at).total_seconds()),
        )
    )
    return scored[0]


def _offset_phrase(change: ShelfChange, tx: POSTransactionRecord) -> str:
    seconds = (tx.transaction_at - change.occurred_at).total_seconds()
    if abs(seconds) < 1:
        return "at the same moment"
    magnitude = int(abs(seconds))
    unit = "second" if magnitude == 1 else "seconds"
    return f"{magnitude} {unit} {'later' if seconds > 0 else 'earlier'}"
