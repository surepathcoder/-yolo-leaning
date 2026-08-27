"""Vision engine -- POS providers and camera/POS correlation.

The failure that matters here is not a missed match. It is an **unearned
accusation**: reporting "no sale explains this" when the real answer is "we
could not see the sales". A till that was switched off for an hour must not
turn that hour of ordinary trading into a list of suspicions.

So the tests that carry the most weight below are the ones asserting what
correlation refuses to conclude.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vision.pos import (
    CorrelationConfig,
    CorrelationStatus,
    CSVPOSProvider,
    FetchResult,
    InMemoryPOSProvider,
    POSError,
    POSLineItem,
    POSTransactionRecord,
    ShelfChange,
    TransactionStatus,
    correlate,
    score_transaction,
)

BASE = datetime(2026, 1, 1, 14, 31, 0, tzinfo=UTC)


def sale(
    external_id: str = "TX-1",
    *,
    offset_seconds: float = 30,
    refs: tuple[str, ...] = ("AZAM-500",),
    quantity: str = "1",
    status: TransactionStatus = TransactionStatus.COMPLETED,
    terminal: str | None = None,
) -> POSTransactionRecord:
    return POSTransactionRecord(
        external_id=external_id,
        provider="mock",
        transaction_at=BASE + timedelta(seconds=offset_seconds),
        status=status,
        terminal_ref=terminal,
        total_amount=Decimal("1200.00"),
        items=tuple(
            POSLineItem(name=ref, external_product_ref=ref, quantity=Decimal(quantity))
            for ref in refs
        ),
    )


def removal(**overrides) -> ShelfChange:
    kwargs = {
        "shelf_id": "shelf-1",
        "shelf_name": "Aisle 3 top",
        "occurred_at": BASE,
        "delta": -0.15,
        "expected_product_refs": frozenset({"AZAM-500"}),
    }
    kwargs.update(overrides)
    return ShelfChange(**kwargs)


def fetched(*transactions, complete: bool = True, reason: str | None = None) -> FetchResult:
    return FetchResult(transactions=tuple(transactions), complete=complete, reason=reason)


# ------------------------------------------------------------------ providers


def test_in_memory_provider_filters_by_window():
    provider = InMemoryPOSProvider([sale("A", offset_seconds=0), sale("B", offset_seconds=600)])
    result = provider.fetch(BASE - timedelta(seconds=60), BASE + timedelta(seconds=60))
    assert [tx.external_id for tx in result.transactions] == ["A"]
    assert result.complete


def test_voided_transactions_are_not_sales():
    """A void did not move stock, so it cannot explain a shelf change."""
    provider = InMemoryPOSProvider([sale("V", status=TransactionStatus.VOIDED)])
    result = provider.fetch(BASE - timedelta(minutes=5), BASE + timedelta(minutes=5))
    assert len(result.transactions) == 1
    assert result.sales == ()


@pytest.mark.parametrize(
    ("status", "is_sale"),
    [
        (TransactionStatus.COMPLETED, True),
        (TransactionStatus.VOIDED, False),
        (TransactionStatus.REFUNDED, False),
        (TransactionStatus.PENDING, False),
    ],
)
def test_only_completed_transactions_count_as_sales(status, is_sale):
    assert sale(status=status).is_sale is is_sale


def test_provider_reports_incomplete_coverage():
    provider = InMemoryPOSProvider([], complete=False, reason="till offline")
    result = provider.fetch(BASE, BASE + timedelta(minutes=5))
    assert not result.complete
    assert result.reason == "till offline"


# ---------------------------------------------------------------- CSV import


def write_csv(tmp_path, rows: str):
    path = tmp_path / "sales.csv"
    path.write_text(rows, encoding="utf-8")
    return path


def test_csv_groups_rows_into_one_transaction(tmp_path):
    path = write_csv(
        tmp_path,
        "transaction_id,datetime,sku,item,qty,price\n"
        "TX-1,2026-01-01T14:31:30+00:00,AZAM-500,Azam Cola,2,1200\n"
        "TX-1,2026-01-01T14:31:30+00:00,KIL-500,Kilimanjaro,1,900\n",
    )
    result = CSVPOSProvider(path=path).fetch(BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    assert len(result.transactions) == 1
    assert result.transactions[0].product_refs == {"AZAM-500", "KIL-500"}
    assert result.transactions[0].total_quantity == Decimal(3)


def test_csv_accepts_alternative_column_names(tmp_path):
    """Small shops export from inconsistent tools."""
    path = write_csv(
        tmp_path,
        "receipt_id,timestamp,product_code,description,units\n"
        "R-9,2026-01-01T14:31:10+00:00,AZAM-500,Azam Cola,1\n",
    )
    result = CSVPOSProvider(path=path).fetch(BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    assert result.transactions[0].external_id == "R-9"


def test_a_bad_row_is_skipped_but_marks_the_window_incomplete(tmp_path):
    """One malformed line must not cost the night's sales -- nor be silent."""
    path = write_csv(
        tmp_path,
        "transaction_id,datetime,sku,item,qty\n"
        "TX-1,2026-01-01T14:31:30+00:00,AZAM-500,Azam Cola,1\n"
        ",,BROKEN,,\n",
    )
    provider = CSVPOSProvider(path=path)
    result = provider.fetch(BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    assert len(result.transactions) == 1
    assert not result.complete
    assert len(provider.skipped) == 1


def test_naive_timestamps_are_rejected_rather_than_guessed(tmp_path):
    """A silent hour of drift would corrupt every correlation."""
    path = write_csv(
        tmp_path,
        "transaction_id,datetime,sku,item,qty\nTX-1,2026-01-01T14:31:30,AZAM-500,Cola,1\n",
    )
    provider = CSVPOSProvider(path=path)
    result = provider.fetch(BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    assert result.transactions == ()
    assert not result.complete


def test_naive_timestamps_are_accepted_when_a_zone_is_configured(tmp_path):
    path = write_csv(
        tmp_path,
        "transaction_id,datetime,sku,item,qty\nTX-1,2026-01-01T14:31:30,AZAM-500,Cola,1\n",
    )
    provider = CSVPOSProvider(path=path, assume_tzinfo=UTC)
    result = provider.fetch(BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    assert len(result.transactions) == 1


def test_an_unknown_status_is_not_assumed_to_be_a_sale(tmp_path):
    path = write_csv(
        tmp_path,
        "transaction_id,datetime,status,sku,item,qty\n"
        "TX-1,2026-01-01T14:31:30+00:00,WEIRD,AZAM-500,Cola,1\n",
    )
    result = CSVPOSProvider(path=path).fetch(BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    assert result.transactions[0].status is TransactionStatus.PENDING
    assert result.sales == ()


def test_a_missing_export_is_an_error_not_an_empty_day(tmp_path):
    """Silently returning no sales would invent a shop-wide discrepancy."""
    with pytest.raises(POSError):
        CSVPOSProvider(path=tmp_path / "nope.csv").fetch(BASE, BASE + timedelta(hours=1))


# ----------------------------------------------------------------- the scores


def test_a_sale_at_the_same_moment_scores_highest_on_time():
    score, breakdown = score_transaction(removal(), sale(offset_seconds=0), CorrelationConfig())
    assert breakdown["time"] == pytest.approx(1.0)
    assert score > 0.9


def test_time_score_decays_towards_the_window_edge():
    config = CorrelationConfig(window_seconds=120)
    near = score_transaction(removal(), sale(offset_seconds=10), config)[1]["time"]
    far = score_transaction(removal(), sale(offset_seconds=110), config)[1]["time"]
    assert near > far > 0


def test_the_window_is_asymmetric_because_people_queue():
    """A sale 60s after the shelf changed is far more explanatory than one 60s
    before, when the item had not been picked up yet."""
    config = CorrelationConfig(window_seconds=120, lookback_ratio=0.25)
    after = score_transaction(removal(), sale(offset_seconds=60), config)[1]["time"]
    before = score_transaction(removal(), sale(offset_seconds=-60), config)[1]["time"]
    assert after > before


def test_a_missing_product_signal_is_unavailable_not_a_failure():
    """An uncatalogued shop must not be pushed towards 'unexplained'."""
    uncatalogued = removal(expected_product_refs=frozenset())
    score, breakdown = score_transaction(uncatalogued, sale(offset_seconds=0), CorrelationConfig())
    assert "product" not in breakdown
    assert score > 0.9  # judged on the signals it does have


def test_a_wrong_product_lowers_the_score():
    right = score_transaction(removal(), sale(offset_seconds=0), CorrelationConfig())[0]
    wrong = score_transaction(
        removal(), sale(offset_seconds=0, refs=("OTHER-1",)), CorrelationConfig()
    )[0]
    assert wrong < right


def test_quantity_alone_cannot_carry_a_match():
    """Occupancy is a ratio; the implied unit count is an estimate on an
    estimate, so it is weighted low deliberately."""
    config = CorrelationConfig()
    # Perfect quantity agreement, wrong product, at the very edge of the window.
    score, breakdown = score_transaction(
        removal(delta=-0.15),
        sale(offset_seconds=119, refs=("OTHER-1",), quantity="1"),
        config,
    )
    assert breakdown["quantity"] > 0.9
    assert score < config.match_threshold


def test_terminal_match_is_only_a_tie_breaker():
    config = CorrelationConfig()
    plain = score_transaction(removal(), sale(offset_seconds=30), config)[0]
    same_till = score_transaction(
        removal(expected_terminal_ref="T1"), sale(offset_seconds=30, terminal="T1"), config
    )[0]
    assert same_till > plain
    assert same_till - plain == pytest.approx(0.05)


# ------------------------------------------------------------- the conclusion


def test_a_matching_sale_explains_the_change():
    outcome = correlate(removal(), fetched(sale(offset_seconds=20)))
    assert outcome.status is CorrelationStatus.MATCHED
    assert outcome.matched_transaction is not None
    assert outcome.matched_transaction.external_id == "TX-1"
    assert "20 seconds later" in outcome.explanation
    assert not outcome.supports_inference


def test_no_sale_in_the_window_is_unmatched():
    outcome = correlate(removal(), fetched())
    assert outcome.status is CorrelationStatus.UNMATCHED
    assert outcome.supports_inference


def test_a_sale_outside_the_window_does_not_count():
    outcome = correlate(removal(), fetched(sale(offset_seconds=600)))
    assert outcome.status is CorrelationStatus.UNMATCHED
    assert outcome.candidates_considered == 0


def test_a_nearby_but_implausible_sale_is_uncertain_or_unmatched():
    outcome = correlate(removal(), fetched(sale(offset_seconds=100, refs=("OTHER-1",))))
    assert outcome.status in {CorrelationStatus.UNCERTAIN, CorrelationStatus.UNMATCHED}
    assert outcome.candidates_considered == 1


def test_the_closest_sale_wins_a_tie():
    outcome = correlate(
        removal(), fetched(sale("FAR", offset_seconds=90), sale("NEAR", offset_seconds=5))
    )
    assert outcome.matched_transaction.external_id == "NEAR"


def test_the_score_is_auditable():
    outcome = correlate(removal(), fetched(sale(offset_seconds=10)))
    assert set(outcome.score_breakdown) >= {"time", "product"}
    assert all(0.0 <= v <= 1.0 for v in outcome.score_breakdown.values())


def test_a_restock_is_not_correlated_against_sales():
    """Looking for a sale to explain stock arriving would be nonsense."""
    outcome = correlate(removal(delta=+0.4), fetched(sale()))
    assert outcome.status is CorrelationStatus.UNCERTAIN
    assert not outcome.supports_inference
    assert "restocking" in outcome.explanation.lower()


def test_a_voided_sale_cannot_explain_a_change():
    outcome = correlate(
        removal(), fetched(sale(offset_seconds=10, status=TransactionStatus.VOIDED))
    )
    assert outcome.status is CorrelationStatus.UNMATCHED


# ------------------------------------------------- what it refuses to conclude


def test_an_incomplete_window_never_produces_a_discrepancy():
    """The single most important behaviour in this module.

    A till that was switched off must not turn ordinary trading into a list of
    suspicions.
    """
    outcome = correlate(removal(), fetched(complete=False, reason="till offline"))
    assert outcome.status is CorrelationStatus.UNCERTAIN
    assert not outcome.supports_inference
    assert "till offline" in outcome.explanation


def test_an_incomplete_window_can_still_confirm_a_match():
    """Absence of evidence is unusable; presence of it is not."""
    outcome = correlate(
        removal(), fetched(sale(offset_seconds=10), complete=False, reason="export truncated")
    )
    assert outcome.status is CorrelationStatus.MATCHED
    assert not outcome.supports_inference
    assert "export truncated" in outcome.explanation


def test_an_incomplete_window_with_a_poor_candidate_stays_uncertain():
    outcome = correlate(
        removal(),
        fetched(sale(offset_seconds=115, refs=("OTHER-1",)), complete=False, reason="gap"),
    )
    assert outcome.status is CorrelationStatus.UNCERTAIN
    assert not outcome.supports_inference


def test_an_unmatched_result_names_the_innocent_explanations():
    """The text a manager reads must not read as an accusation."""
    outcome = correlate(removal(), fetched())
    lowered = outcome.explanation.lower()
    assert "restocking" in lowered
    assert "detection error" in lowered


@pytest.mark.parametrize(
    "builder",
    [
        lambda: correlate(removal(), fetched()),
        lambda: correlate(removal(), fetched(sale(offset_seconds=10))),
        lambda: correlate(removal(), fetched(complete=False, reason="offline")),
        lambda: correlate(removal(delta=+0.4), fetched(sale())),
        lambda: correlate(removal(), fetched(sale(offset_seconds=110, refs=("X",)))),
    ],
)
def test_no_outcome_ever_accuses_anyone(builder):
    """docs/PRIVACY.md: the system reports observations, never motives."""
    forbidden = (
        "theft",
        "thief",
        "steal",
        "stolen",
        "shoplift",
        "criminal",
        "guilty",
        "suspect",
        "culprit",
        "dishonest",
    )
    explanation = builder().explanation.lower()
    for word in forbidden:
        assert word not in explanation, f"{word!r} appeared in: {explanation}"


def test_track_keys_are_not_an_input_to_the_score():
    """Presence at a shelf is not evidence that this person moved the stock."""
    without = correlate(removal(), fetched(sale(offset_seconds=20)))
    with_tracks = correlate(
        removal(nearby_track_keys=("cam:sess:7",)), fetched(sale(offset_seconds=20))
    )
    assert without.score == with_tracks.score
    assert without.status is with_tracks.status


def test_vision_and_backend_correlation_vocabularies_agree():
    from app.models.enums import CorrelationStatus as BackendStatus
    from app.models.enums import POSTransactionStatus as BackendTxStatus

    assert {s.value for s in CorrelationStatus} == {s.value for s in BackendStatus}
    assert {s.value for s in TransactionStatus} == {s.value for s in BackendTxStatus}
