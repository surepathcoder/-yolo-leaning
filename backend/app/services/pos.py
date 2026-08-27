"""POS import and camera/POS correlation.

Imports sales from a configured provider and matches them against shelf
observations. What comes out of correlation is stored as a
``CorrelationResult``, and only sometimes becomes an event.

**When an inference is raised, and when it is not.** A
``POSSIBLE_INVENTORY_DISCREPANCY`` is created only when every one of these
holds:

1. The shelf change was a **removal**. Stock arriving is a delivery.
2. POS coverage for the window was **complete**. A till that was offline must
   never turn ordinary trading into a list of suspicions -- this is the single
   most important rule in the subsystem, and it is enforced in
   ``vision/pos/correlation.py`` as well as here.
3. Correlation found **no plausible sale**.
4. The observation's own confidence clears ``POS_MIN_CONFIDENCE``. A shaky
   detection is not a foundation for a conclusion about a shop.

Even then, the event is an inference: hedged in its own name, carrying the
inventory event it rests on in ``source_event_ids``, and stored with the
plain-language explanation a reviewer reads. Every result can be reviewed by a
human, and that verdict is recorded -- it is the only way correlation accuracy
can ever be measured rather than assumed.

Nothing in this module decides that anything was taken. It decides whether the
till explains what the camera saw, and says so in those words.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.tenancy import TenantScope
from app.models import (
    CorrelationResult,
    InventoryEvent,
    POSItem,
    POSTransaction,
    Product,
    Shelf,
    Store,
)
from app.models.enums import (
    AuditAction,
    CorrelationStatus,
    InventoryEventType,
    POSTransactionStatus,
)
from app.services.audit import record_audit
from app.services.stores import get_store
from vision.events.types import ConfidenceLevel, EventType, Severity, VisionEvent
from vision.pos import (
    PROVIDERS,
    CorrelationConfig,
    FetchResult,
    POSProvider,
    POSTransactionRecord,
    ShelfChange,
    correlate,
)

log = get_logger(__name__)

#: Observations below this confidence never produce an inference. A detection
#: the engine was unsure of is a poor foundation for a conclusion about a shop.
MIN_INFERENCE_CONFIDENCE = 0.5

#: Only removals are correlated. The others describe stock arriving or a
#: change too small to name.
CORRELATED_EVENT_TYPES = frozenset(
    {InventoryEventType.PRODUCT_REMOVED, InventoryEventType.SHELF_EMPTY}
)

#: Review verdicts a human may record. Deliberately about the *correlation*,
#: not about a person: the question is "did we get the match right", never
#: "was this person stealing".
REVIEW_OUTCOMES = frozenset(
    {
        "CORRECT",  # the correlation was right
        "INCORRECT",  # it was wrong
        "EXPLAINED",  # unmatched, but the reviewer knows the innocent reason
        "UNCLEAR",  # cannot tell
    }
)


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------


def get_pos_config(store: Store) -> dict:
    """A store's POS settings, held in ``Store.settings['pos']``."""
    return dict(store.settings.get("pos") or {})


def set_pos_config(
    session: Session, scope: TenantScope, store_id: uuid.UUID, config: dict
) -> Store:
    scope.require(Permission.POS_CONFIGURE)
    store = get_store(session, scope, store_id)
    scope.require_store(store.id)

    provider = config.get("provider")
    if provider and provider not in PROVIDERS:
        raise ValidationError(
            f"Unknown POS provider '{provider}'.",
            details={"provider": provider, "known": sorted(PROVIDERS)},
        )

    settings = dict(store.settings or {})
    settings["pos"] = config
    store.settings = settings
    session.flush()

    record_audit(
        session,
        action=AuditAction.UPDATE,
        organization_id=store.organization_id,
        user=None,
        resource_type="store",
        resource_id=str(store.id),
        description=f"POS provider set to '{provider or 'none'}' for '{store.name}'",
    )
    return store


def build_provider(store: Store) -> POSProvider | None:
    """Instantiate the store's configured provider, or ``None``."""
    config = get_pos_config(store)
    key = config.get("provider")
    if not key or key not in PROVIDERS:
        return None
    options = dict(config.get("options") or {})
    provider_class = PROVIDERS[key]
    try:
        if key == "csv":
            from pathlib import Path

            return provider_class(path=Path(options["path"]))
        return provider_class()
    except Exception as exc:
        log.warning("pos_provider_build_failed", store_id=str(store.id), error=str(exc)[:200])
        return None


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_transactions(
    session: Session,
    scope: TenantScope,
    store_id: uuid.UUID,
    records: list[POSTransactionRecord],
    *,
    provider: str = "manual",
) -> tuple[list[POSTransaction], int]:
    """Store sales, skipping any already imported.

    Returns ``(created, skipped)``. Idempotency is on ``(provider,
    external_id)`` and it matters: a duplicated sale would "explain" a shelf
    change twice and mask a genuine discrepancy.
    """
    scope.require(Permission.POS_WRITE)
    store = get_store(session, scope, store_id)
    scope.require_store(store.id)

    created: list[POSTransaction] = []
    skipped = 0
    catalogue = _sku_index(session, store.organization_id)

    for record in records:
        key = record.provider if record.provider != "unknown" else provider
        existing = session.scalar(
            select(POSTransaction.id).where(
                POSTransaction.provider == key,
                POSTransaction.external_id == record.external_id,
            )
        )
        if existing is not None:
            skipped += 1
            continue

        transaction = POSTransaction(
            organization_id=store.organization_id,
            store_id=store.id,
            provider=key,
            external_id=record.external_id,
            receipt_number=record.receipt_number,
            transaction_at=record.transaction_at,
            status=POSTransactionStatus(record.status.value),
            total_amount=record.total_amount,
            tax_amount=record.tax_amount,
            currency=record.currency,
            payment_method=record.payment_method,
            terminal_ref=record.terminal_ref,
            cashier_ref=record.cashier_ref,
            raw_payload=record.raw_payload or {},
            imported_at=datetime.now(UTC),
        )
        for item in record.items:
            transaction.items.append(
                POSItem(
                    organization_id=store.organization_id,
                    product_id=catalogue.get((item.external_product_ref or "").upper()),
                    external_product_ref=item.external_product_ref,
                    name=item.name[:200],
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    total_price=item.total_price,
                    discount_amount=item.discount_amount,
                )
            )
        session.add(transaction)
        created.append(transaction)

    session.flush()
    log.info(
        "pos_import",
        store_id=str(store.id),
        provider=provider,
        created=len(created),
        skipped=skipped,
    )
    return created, skipped


def _sku_index(session: Session, organization_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Map upper-cased SKU and barcode to product id.

    An unresolved line is normal, not an error: shops sell things that are not
    in the VisionGuard catalogue, and refusing the import over it would make
    the integration unusable.
    """
    index: dict[str, uuid.UUID] = {}
    rows = session.execute(
        select(Product.id, Product.sku, Product.barcode).where(
            Product.organization_id == organization_id,
            Product.deleted_at.is_(None),
        )
    )
    for product_id, sku, barcode in rows:
        if sku:
            index[sku.upper()] = product_id
        if barcode:
            index[barcode.upper()] = product_id
    return index


def list_transactions(
    session: Session,
    scope: TenantScope,
    *,
    store_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[POSTransaction], int]:
    scope.require(Permission.POS_READ)
    statement = scope.apply(select(POSTransaction), POSTransaction)
    if store_id is not None:
        scope.require_store(store_id)
        statement = statement.where(POSTransaction.store_id == store_id)
    if since is not None:
        statement = statement.where(POSTransaction.transaction_at >= since)
    if until is not None:
        statement = statement.where(POSTransaction.transaction_at <= until)

    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    rows = session.scalars(
        statement.order_by(POSTransaction.transaction_at.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), int(total or 0)


def transactions_near(
    session: Session,
    scope: TenantScope,
    store_id: uuid.UUID,
    moment: datetime,
    window_seconds: int,
) -> FetchResult:
    """Sales already imported around ``moment``.

    Coverage is reported as complete only when this store has *any* imported
    sales at all. A store that has never imported anything is not a store with
    no sales, and treating it as one would make every shelf change look
    unexplained the moment POS is switched on.
    """
    scope.require(Permission.POS_READ)
    window = timedelta(seconds=window_seconds)
    rows = session.scalars(
        scope.apply(select(POSTransaction), POSTransaction).where(
            POSTransaction.store_id == store_id,
            POSTransaction.transaction_at >= moment - window,
            POSTransaction.transaction_at <= moment + window,
        )
    ).all()

    ever_imported = session.scalar(
        select(POSTransaction.id).where(POSTransaction.store_id == store_id).limit(1)
    )
    return FetchResult(
        transactions=tuple(_to_record(row) for row in rows),
        complete=ever_imported is not None,
        reason=None if ever_imported else "no POS data has been imported for this store",
    )


def _to_record(row: POSTransaction) -> POSTransactionRecord:
    from vision.pos import POSLineItem, TransactionStatus

    return POSTransactionRecord(
        external_id=row.external_id,
        provider=row.provider,
        transaction_at=row.transaction_at,
        status=TransactionStatus(row.status.value),
        receipt_number=row.receipt_number,
        total_amount=row.total_amount or Decimal(0),
        currency=row.currency,
        payment_method=row.payment_method,
        terminal_ref=row.terminal_ref,
        cashier_ref=row.cashier_ref,
        items=tuple(
            POSLineItem(
                name=item.name,
                external_product_ref=item.external_product_ref,
                quantity=item.quantity,
                unit_price=item.unit_price,
                total_price=item.total_price,
            )
            for item in row.items
        ),
    )


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def correlate_inventory_event(
    session: Session,
    scope: TenantScope,
    inventory_event: InventoryEvent,
    *,
    fetched: FetchResult | None = None,
    config: CorrelationConfig | None = None,
) -> CorrelationResult:
    """Match one shelf observation against sales and store the outcome."""
    scope.require(Permission.POS_READ)
    settings = get_settings()
    config = config or CorrelationConfig(window_seconds=settings.POS_CORRELATION_WINDOW_SECONDS)

    shelf = session.get(Shelf, inventory_event.shelf_id)
    if shelf is None:
        raise NotFoundError("Shelf not found.", details={"shelf_id": str(inventory_event.shelf_id)})
    scope.require_store(shelf.store_id)

    if fetched is None:
        fetched = transactions_near(
            session,
            scope,
            shelf.store_id,
            inventory_event.occurred_at,
            config.window_seconds,
        )

    change = ShelfChange(
        shelf_id=str(shelf.id),
        shelf_name=shelf.name,
        store_id=str(shelf.store_id),
        camera_id=str(shelf.camera_id) if shelf.camera_id else None,
        occurred_at=inventory_event.occurred_at,
        delta=_delta_of(inventory_event),
        expected_product_refs=frozenset(
            product.sku.upper() for product in shelf.products if product.sku
        ),
    )

    outcome = correlate(change, fetched, config)

    matched_id = None
    if outcome.matched_transaction is not None:
        matched_id = session.scalar(
            select(POSTransaction.id).where(
                POSTransaction.provider == outcome.matched_transaction.provider,
                POSTransaction.external_id == outcome.matched_transaction.external_id,
            )
        )

    result = CorrelationResult(
        organization_id=inventory_event.organization_id,
        store_id=shelf.store_id,
        inventory_event_id=inventory_event.id,
        pos_transaction_id=matched_id,
        status=CorrelationStatus(outcome.status.value),
        score=outcome.score,
        window_seconds=outcome.window_seconds,
        candidates_considered=outcome.candidates_considered,
        score_breakdown=dict(outcome.score_breakdown),
        explanation=outcome.explanation,
        correlated_at=datetime.now(UTC),
    )
    session.add(result)
    session.flush()

    if _should_infer(inventory_event, outcome):
        event = _raise_discrepancy(session, inventory_event, shelf, result, outcome)
        if event is not None:
            result.event_id = event.id
            session.flush()

    log.info(
        "correlation",
        inventory_event_id=str(inventory_event.id),
        status=result.status.value,
        score=round(result.score, 3),
        candidates=result.candidates_considered,
        inferred=result.event_id is not None,
    )
    return result


def _delta_of(inventory_event: InventoryEvent) -> float:
    """Occupancy change, negative for a removal.

    Falls back to the event type when the occupancy figures are missing, so a
    removal recorded without them is still treated as a removal rather than
    silently skipped.
    """
    before = inventory_event.occupancy_before
    after = inventory_event.occupancy_after
    if before is not None and after is not None:
        return float(after) - float(before)
    if inventory_event.event_type in CORRELATED_EVENT_TYPES:
        return -1.0
    return 1.0


def _should_infer(inventory_event: InventoryEvent, outcome) -> bool:
    """Every condition that must hold before concluding anything.

    Deliberately a single readable function: the conditions under which this
    product is willing to raise a suspicion should be legible in one place.
    """
    if inventory_event.event_type not in CORRELATED_EVENT_TYPES:
        return False
    # supports_inference is set only for an UNMATCHED result over a window the
    # provider confirmed it could see in full. Both conditions live in
    # vision/pos/correlation.py; this re-states neither, so they cannot drift.
    if not outcome.supports_inference:
        return False
    return float(inventory_event.confidence or 0.0) >= MIN_INFERENCE_CONFIDENCE


def _raise_discrepancy(
    session: Session,
    inventory_event: InventoryEvent,
    shelf: Shelf,
    result: CorrelationResult,
    outcome,
):
    """Create the hedged inference event.

    The summary is written to be quoted safely: it says what was observed and
    what was not found, and names the ordinary explanations. It must survive
    being read aloud in a dispute.
    """
    from app.services.events import ingest_vision_event

    confidence_level = (
        ConfidenceLevel.LOW if outcome.candidates_considered else ConfidenceLevel.MEDIUM
    )
    summary = (
        f"Stock left '{shelf.name}' at "
        f"{inventory_event.occurred_at.strftime('%H:%M')} and no matching sale was "
        f"found within {outcome.window_seconds} seconds. This needs a human to "
        f"check: restocking, staff moving stock, an item put back elsewhere, a "
        f"mis-scanned item or a detection error all produce this result."
    )

    vision_event = VisionEvent(
        event_type=EventType.POSSIBLE_INVENTORY_DISCREPANCY,
        severity=Severity.LOW,
        camera_id=str(shelf.camera_id) if shelf.camera_id else str(shelf.id),
        store_id=str(shelf.store_id),
        shelf_id=str(shelf.id),
        product_id=str(inventory_event.product_id) if inventory_event.product_id else None,
        occurred_at=inventory_event.occurred_at,
        is_inference=True,
        confidence=inventory_event.confidence,
        confidence_level=confidence_level,
        rule_name="pos_correlation",
        source_event_ids=[str(inventory_event.id)],
        track_key=inventory_event.track_key,
        summary=summary,
        metadata={
            "correlation_result_id": str(result.id),
            "correlation_status": result.status.value,
            "candidates_considered": outcome.candidates_considered,
            "window_seconds": outcome.window_seconds,
            "shelf_name": shelf.name,
            "occupancy_before": inventory_event.occupancy_before,
            "occupancy_after": inventory_event.occupancy_after,
        },
    )
    event, _alert = ingest_vision_event(
        session, vision_event, organization_id=inventory_event.organization_id
    )
    return event


def correlate_pending(
    session: Session,
    scope: TenantScope,
    *,
    store_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[CorrelationResult]:
    """Correlate inventory events that have no result yet."""
    scope.require(Permission.POS_READ)
    correlated = select(CorrelationResult.inventory_event_id)
    statement = (
        scope.apply(select(InventoryEvent), InventoryEvent)
        .where(
            InventoryEvent.event_type.in_(CORRELATED_EVENT_TYPES),
            InventoryEvent.id.not_in(correlated),
        )
        .order_by(InventoryEvent.occurred_at)
        .limit(limit)
    )
    if store_id is not None:
        scope.require_store(store_id)
        statement = statement.where(InventoryEvent.store_id == store_id)
    if since is not None:
        statement = statement.where(InventoryEvent.occurred_at >= since)

    return [
        correlate_inventory_event(session, scope, event)
        for event in session.scalars(statement).all()
    ]


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def list_correlations(
    session: Session,
    scope: TenantScope,
    *,
    status: CorrelationStatus | None = None,
    store_id: uuid.UUID | None = None,
    reviewed: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[CorrelationResult], int]:
    scope.require(Permission.POS_READ)
    statement = scope.apply(select(CorrelationResult), CorrelationResult)
    if status is not None:
        statement = statement.where(CorrelationResult.status == status)
    if store_id is not None:
        scope.require_store(store_id)
        statement = statement.where(CorrelationResult.store_id == store_id)
    if reviewed is not None:
        statement = statement.where(
            CorrelationResult.reviewed_at.isnot(None)
            if reviewed
            else CorrelationResult.reviewed_at.is_(None)
        )

    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    rows = session.scalars(
        statement.order_by(CorrelationResult.correlated_at.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), int(total or 0)


def get_correlation(
    session: Session, scope: TenantScope, result_id: uuid.UUID
) -> CorrelationResult:
    scope.require(Permission.POS_READ)
    result = session.scalar(
        scope.apply(select(CorrelationResult), CorrelationResult).where(
            CorrelationResult.id == result_id
        )
    )
    if result is None:
        raise NotFoundError("Correlation result not found.", details={"result_id": str(result_id)})
    return result


def review_correlation(
    session: Session,
    scope: TenantScope,
    result_id: uuid.UUID,
    *,
    outcome: str,
    notes: str | None = None,
) -> CorrelationResult:
    """Record a human's verdict on a correlation.

    This is the feedback loop that lets accuracy be measured rather than
    assumed. The verdict is about whether *the system got the match right* --
    it is deliberately not a place to record a judgement about a person.
    """
    scope.require(Permission.POS_WRITE)
    result = get_correlation(session, scope, result_id)
    scope.require_store(result.store_id)

    verdict = outcome.strip().upper()
    if verdict not in REVIEW_OUTCOMES:
        raise ValidationError(
            f"Unknown review outcome '{outcome}'.",
            details={"allowed": sorted(REVIEW_OUTCOMES)},
        )
    if result.reviewed_at is not None:
        raise ConflictError(
            "This result has already been reviewed.",
            details={"result_id": str(result_id)},
        )

    result.review_outcome = verdict
    result.review_notes = notes
    result.reviewed_by_id = scope.user_id
    result.reviewed_at = datetime.now(UTC)
    session.flush()

    record_audit(
        session,
        action=AuditAction.UPDATE,
        organization_id=result.organization_id,
        user=None,
        resource_type="correlation_result",
        resource_id=str(result.id),
        description=f"Correlation reviewed as {verdict}",
    )
    return result


def accuracy_summary(session: Session, scope: TenantScope) -> dict:
    """How correlation is performing, according to the humans reviewing it."""
    scope.require(Permission.POS_READ)
    base = scope.apply(select(CorrelationResult), CorrelationResult)

    def count(statement) -> int:
        return int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)

    reviewed = base.where(CorrelationResult.reviewed_at.isnot(None))
    correct = count(reviewed.where(CorrelationResult.review_outcome == "CORRECT"))
    incorrect = count(reviewed.where(CorrelationResult.review_outcome == "INCORRECT"))
    explained = count(reviewed.where(CorrelationResult.review_outcome == "EXPLAINED"))
    judged = correct + incorrect

    return {
        "total": count(base),
        "matched": count(base.where(CorrelationResult.status == CorrelationStatus.MATCHED)),
        "unmatched": count(base.where(CorrelationResult.status == CorrelationStatus.UNMATCHED)),
        "uncertain": count(base.where(CorrelationResult.status == CorrelationStatus.UNCERTAIN)),
        "reviewed": count(reviewed),
        "review_correct": correct,
        "review_incorrect": incorrect,
        "review_explained": explained,
        # Null rather than 0 when nothing has been judged: an unmeasured system
        # must not display "0% accurate", nor "100%".
        "accuracy": round(correct / judged, 4) if judged else None,
    }
