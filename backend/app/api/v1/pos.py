"""POS import and correlation endpoints.

Correlation is exposed as a resource with its own history and review trail,
not as a field on an event. That is deliberate: the reasoning behind a
discrepancy has to be inspectable after the fact, and a human's verdict on it
has to be recorded somewhere it can be counted.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, DbSession, Scope
from app.models.enums import CorrelationStatus
from app.schemas.common import MessageResponse, Page
from app.schemas.pos import (
    CorrelationAccuracy,
    CorrelationRead,
    CorrelationReview,
    CorrelationRunRequest,
    POSConfigIn,
    POSConfigRead,
    POSImportRequest,
    POSImportResult,
    POSTransactionRead,
)
from app.services import pos as pos_service
from app.services.stores import get_store
from vision.pos import PROVIDERS, POSLineItem, POSTransactionRecord, TransactionStatus

router = APIRouter(tags=["pos"])


# -------------------------------------------------------------------- import
@router.post(
    "/pos/transactions",
    response_model=POSImportResult,
    status_code=status.HTTP_201_CREATED,
    summary="Import POS transactions",
)
def import_transactions(
    payload: POSImportRequest, session: DbSession, scope: Scope, _user: CurrentUser
):
    """Import sales.

    Idempotent on `(provider, external_id)`, so re-running a nightly export or
    replaying a webhook is safe. Already-imported sales are reported as
    `skipped` rather than rejected -- and skipping them is what stops a
    duplicate sale from explaining one shelf change twice.
    """
    records = [
        POSTransactionRecord(
            external_id=tx.external_id,
            provider=payload.provider,
            transaction_at=tx.transaction_at,
            status=TransactionStatus(tx.status.value),
            receipt_number=tx.receipt_number,
            total_amount=tx.total_amount,
            tax_amount=tx.tax_amount,
            currency=tx.currency,
            payment_method=tx.payment_method,
            terminal_ref=tx.terminal_ref,
            cashier_ref=tx.cashier_ref,
            items=tuple(
                POSLineItem(
                    name=item.name,
                    external_product_ref=item.external_product_ref,
                    quantity=item.quantity or Decimal(1),
                    unit_price=item.unit_price,
                    total_price=item.total_price,
                    discount_amount=item.discount_amount,
                )
                for item in tx.items
            ),
        )
        for tx in payload.transactions
    ]
    created, skipped = pos_service.import_transactions(
        session, scope, payload.store_id, records, provider=payload.provider
    )
    session.commit()
    return POSImportResult(
        created=len(created),
        skipped=skipped,
        store_id=payload.store_id,
        provider=payload.provider,
    )


@router.get(
    "/pos/transactions",
    response_model=Page[POSTransactionRead],
    summary="List POS transactions",
)
def list_transactions(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    store_id: uuid.UUID | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows, total = pos_service.list_transactions(
        session,
        scope,
        store_id=store_id,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    return Page[POSTransactionRead](
        items=[POSTransactionRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# --------------------------------------------------------------- correlation
@router.post(
    "/pos/correlate",
    response_model=list[CorrelationRead],
    summary="Correlate outstanding shelf observations",
)
def run_correlation(
    payload: CorrelationRunRequest, session: DbSession, scope: Scope, _user: CurrentUser
):
    """Match shelf removals that have no correlation result yet.

    A result is produced for every observation considered. Only some of them
    raise a `POSSIBLE_INVENTORY_DISCREPANCY`, and never one taken over a
    window the POS provider could not see in full.
    """
    results = pos_service.correlate_pending(
        session,
        scope,
        store_id=payload.store_id,
        since=payload.since,
        limit=payload.limit,
    )
    session.commit()
    return [CorrelationRead.model_validate(result) for result in results]


@router.get(
    "/pos/correlations",
    response_model=Page[CorrelationRead],
    summary="Correlation history",
)
def list_correlations(
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
    correlation_status: CorrelationStatus | None = Query(None, alias="status"),
    store_id: uuid.UUID | None = Query(None),
    reviewed: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Every correlation, with the reasoning behind it.

    `status` alone is not the answer: `UNMATCHED` means no sale was found in
    the window, which is a statement about the data and not about a person.
    Render `explanation` with it.
    """
    rows, total = pos_service.list_correlations(
        session,
        scope,
        status=correlation_status,
        store_id=store_id,
        reviewed=reviewed,
        limit=limit,
        offset=offset,
    )
    return Page[CorrelationRead](
        items=[CorrelationRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/pos/correlations/{result_id}",
    response_model=CorrelationRead,
    summary="Get one correlation result",
)
def get_correlation(result_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    return CorrelationRead.model_validate(pos_service.get_correlation(session, scope, result_id))


@router.post(
    "/pos/correlations/{result_id}/review",
    response_model=CorrelationRead,
    summary="Record a review of a correlation",
)
def review_correlation(
    result_id: uuid.UUID,
    payload: CorrelationReview,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    """Record whether the system got this match right.

    The verdict is about the correlation -- CORRECT, INCORRECT, EXPLAINED,
    UNCLEAR -- and never about a person. It is the only way the accuracy of
    this subsystem can be measured rather than assumed.
    """
    result = pos_service.review_correlation(
        session, scope, result_id, outcome=payload.outcome, notes=payload.notes
    )
    session.commit()
    return CorrelationRead.model_validate(result)


@router.get(
    "/pos/accuracy",
    response_model=CorrelationAccuracy,
    summary="How correlation is performing",
)
def correlation_accuracy(session: DbSession, scope: Scope, _user: CurrentUser):
    return CorrelationAccuracy.model_validate(pos_service.accuracy_summary(session, scope))


# ------------------------------------------------------------- configuration
@router.get("/pos/config/{store_id}", response_model=POSConfigRead, summary="Get POS configuration")
def get_config(store_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    store = get_store(session, scope, store_id)
    config = pos_service.get_pos_config(store)
    return POSConfigRead(
        store_id=store.id,
        provider=config.get("provider"),
        options=config.get("options") or {},
        available_providers=sorted(PROVIDERS),
    )


@router.put("/pos/config/{store_id}", response_model=POSConfigRead, summary="Set POS configuration")
def set_config(
    store_id: uuid.UUID,
    payload: POSConfigIn,
    session: DbSession,
    scope: Scope,
    _user: CurrentUser,
):
    store = pos_service.set_pos_config(session, scope, store_id, payload.model_dump())
    session.commit()
    config = pos_service.get_pos_config(store)
    return POSConfigRead(
        store_id=store.id,
        provider=config.get("provider"),
        options=config.get("options") or {},
        available_providers=sorted(PROVIDERS),
    )


@router.delete(
    "/pos/config/{store_id}", response_model=MessageResponse, summary="Disable POS integration"
)
def clear_config(store_id: uuid.UUID, session: DbSession, scope: Scope, _user: CurrentUser):
    pos_service.set_pos_config(session, scope, store_id, {})
    session.commit()
    return MessageResponse(message="POS integration disabled.")
