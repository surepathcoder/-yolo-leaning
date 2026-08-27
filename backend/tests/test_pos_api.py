"""Milestone 16 -- POS import, correlation and review.

This is the subsystem that can do real harm if it is wrong, so most of what is
asserted below is what it *refuses* to do:

* it does not raise a discrepancy when the till data was incomplete
* it does not raise one from a low-confidence observation
* it does not raise one for stock arriving
* it never phrases an unmatched result as an accusation
* it stores every conclusion with the reasoning behind it

The end-to-end test at the bottom is the first time in this codebase that
POSSIBLE_INVENTORY_DISCREPANCY can fire at all, so it checks the whole chain:
shelf observation, no matching sale, hedged inference, source event attached.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.enums import InventoryEventType, PlanTier

PASSWORD = "tembo-anga-baridi-91"
SHELF_POLY = [[0.2, 0.4], [0.8, 0.4], [0.8, 0.6], [0.2, 0.6]]
NOW = datetime(2026, 1, 1, 14, 31, tzinfo=UTC)


def register(client, db_session, *, email: str, org: str) -> dict:
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": "Shop Owner",
            "organization_name": org,
        },
    ).json()
    headers = {"Authorization": f"Bearer {registered['tokens']['access_token']}"}

    from app.models import Subscription

    subscription = db_session.scalar(
        select(Subscription).where(
            Subscription.organization_id == uuid.UUID(registered["organization_id"])
        )
    )
    subscription.tier = PlanTier.BUSINESS
    db_session.commit()

    store = client.post(
        "/api/v1/stores",
        json={"name": "Kariakoo", "code": "DSM-01", "timezone": "Africa/Dar_es_Salaam"},
        headers=headers,
    ).json()
    camera = client.post(
        "/api/v1/cameras",
        json={"store_id": store["id"], "name": "Aisle 3", "source_kind": "SYNTHETIC"},
        headers=headers,
    ).json()
    product = client.post(
        "/api/v1/products",
        json={"sku": "AZAM-500", "name": "Azam Cola 500ml"},
        headers=headers,
    ).json()
    shelf = client.post(
        "/api/v1/shelves",
        json={
            "store_id": store["id"],
            "camera_id": camera["id"],
            "name": "Aisle 3 top",
            "code": "A3-T",
            "polygon": SHELF_POLY,
            "product_ids": [product["id"]],
        },
        headers=headers,
    ).json()
    return {
        "headers": headers,
        "store": store,
        "camera": camera,
        "product": product,
        "shelf": shelf,
        "organization_id": registered["organization_id"],
    }


@pytest.fixture()
def ctx(client, db_session):
    return register(client, db_session, email="owner@duka.co.tz", org="Duka la Amina")


@pytest.fixture()
def other(client, db_session):
    return register(client, db_session, email="rival@shop.co.tz", org="Rival Shop")


def scope_for(client, db_session, ctx):
    from app.models import User
    from app.services.auth import build_scope

    me = client.get("/api/v1/auth/me", headers=ctx["headers"]).json()
    return build_scope(db_session.get(User, uuid.UUID(me["id"])))


def make_removal(client, db_session, ctx, *, confidence: float = 0.9, at: datetime = NOW):
    """Record a shelf removal directly, as the pipeline would."""
    from app.services import inventory as inventory_service

    scope = scope_for(client, db_session, ctx)
    event = inventory_service.record_event(
        db_session,
        scope,
        uuid.UUID(ctx["shelf"]["id"]),
        event_type=InventoryEventType.PRODUCT_REMOVED,
        occupancy_before=0.9,
        occupancy_after=0.6,
        confidence=confidence,
        occurred_at=at,
    )
    db_session.commit()
    return event


def import_sale(client, ctx, *, external_id="TX-1", offset_seconds=20, sku="AZAM-500"):
    payload = {
        "store_id": ctx["store"]["id"],
        "provider": "mock",
        "transactions": [
            {
                "external_id": external_id,
                "transaction_at": (NOW + timedelta(seconds=offset_seconds)).isoformat(),
                "total_amount": "1200.00",
                "items": [{"name": "Azam Cola", "external_product_ref": sku, "quantity": "1"}],
            }
        ],
    }
    response = client.post("/api/v1/pos/transactions", json=payload, headers=ctx["headers"])
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------- import


def test_import_stores_a_transaction_with_its_items(client, ctx):
    result = import_sale(client, ctx)
    assert result["created"] == 1
    assert result["skipped"] == 0

    page = client.get("/api/v1/pos/transactions", headers=ctx["headers"]).json()
    assert page["total"] == 1
    assert page["items"][0]["external_id"] == "TX-1"
    assert page["items"][0]["items"][0]["external_product_ref"] == "AZAM-500"


def test_reimporting_the_same_sale_is_skipped_not_duplicated(client, ctx):
    """A duplicated sale would explain one shelf change twice."""
    import_sale(client, ctx)
    again = import_sale(client, ctx)
    assert again["created"] == 0
    assert again["skipped"] == 1
    assert client.get("/api/v1/pos/transactions", headers=ctx["headers"]).json()["total"] == 1


def test_import_resolves_known_skus_to_catalogue_products(client, ctx):
    import_sale(client, ctx, sku="AZAM-500")
    item = client.get("/api/v1/pos/transactions", headers=ctx["headers"]).json()["items"][0][
        "items"
    ][0]
    assert item["product_id"] == ctx["product"]["id"]


def test_an_unknown_sku_does_not_break_the_import(client, ctx):
    """Shops sell things that are not in the VisionGuard catalogue."""
    import_sale(client, ctx, sku="NOT-IN-CATALOGUE")
    item = client.get("/api/v1/pos/transactions", headers=ctx["headers"]).json()["items"][0][
        "items"
    ][0]
    assert item["product_id"] is None
    assert item["external_product_ref"] == "NOT-IN-CATALOGUE"


def test_a_naive_timestamp_is_rejected(client, ctx):
    """Silent hours of drift would make every correlation wrong."""
    response = client.post(
        "/api/v1/pos/transactions",
        json={
            "store_id": ctx["store"]["id"],
            "transactions": [{"external_id": "TX-N", "transaction_at": "2026-01-01T14:31:00"}],
        },
        headers=ctx["headers"],
    )
    assert response.status_code == 422


def test_transactions_are_isolated_between_tenants(client, ctx, other):
    import_sale(client, ctx)
    assert client.get("/api/v1/pos/transactions", headers=other["headers"]).json()["total"] == 0


def test_cannot_import_into_another_tenants_store(client, ctx, other):
    response = client.post(
        "/api/v1/pos/transactions",
        json={
            "store_id": ctx["store"]["id"],
            "transactions": [{"external_id": "TX-X", "transaction_at": NOW.isoformat()}],
        },
        headers=other["headers"],
    )
    assert response.status_code == 404


# ---------------------------------------------------------------- correlation


def test_a_matching_sale_explains_a_removal(client, db_session, ctx):
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=20)

    results = client.post(
        "/api/v1/pos/correlate", json={"store_id": ctx["store"]["id"]}, headers=ctx["headers"]
    ).json()
    assert len(results) == 1
    assert results[0]["status"] == "MATCHED"
    assert results[0]["pos_transaction_id"] is not None
    assert results[0]["event_id"] is None  # nothing to infer


def test_no_sale_produces_an_unmatched_result_and_an_inference(client, db_session, ctx):
    make_removal(client, db_session, ctx)
    # A sale exists for the store, but far outside the window: coverage is
    # complete, so "no sale nearby" is a real finding rather than missing data.
    import_sale(client, ctx, offset_seconds=6000)

    results = client.post(
        "/api/v1/pos/correlate", json={"store_id": ctx["store"]["id"]}, headers=ctx["headers"]
    ).json()
    assert results[0]["status"] == "UNMATCHED"
    assert results[0]["event_id"] is not None


def test_correlation_stores_its_reasoning(client, db_session, ctx):
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=20)
    result = client.post(
        "/api/v1/pos/correlate", json={"store_id": ctx["store"]["id"]}, headers=ctx["headers"]
    ).json()[0]

    assert result["explanation"]
    assert result["window_seconds"] > 0
    assert result["candidates_considered"] >= 1
    assert set(result["score_breakdown"]) >= {"time"}


def test_correlating_twice_does_not_duplicate_results(client, db_session, ctx):
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=20)
    client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"])
    second = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()
    assert second == []
    assert client.get("/api/v1/pos/correlations", headers=ctx["headers"]).json()["total"] == 1


def test_correlations_are_isolated_between_tenants(client, db_session, ctx, other):
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=6000)
    client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"])
    assert client.get("/api/v1/pos/correlations", headers=other["headers"]).json()["total"] == 0


# ------------------------------------------- what it refuses to conclude


def test_no_pos_data_at_all_never_produces_a_discrepancy(client, db_session, ctx):
    """A store that has not connected its till is not a store with no sales.

    This is the case that would otherwise flood a new customer with
    suspicions the moment inventory monitoring was switched on.
    """
    make_removal(client, db_session, ctx)
    results = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()

    assert results[0]["status"] == "UNCERTAIN"
    assert results[0]["event_id"] is None
    assert "no POS data" in results[0]["explanation"]


def test_a_low_confidence_observation_never_produces_a_discrepancy(client, db_session, ctx):
    """A shaky detection is not a foundation for a conclusion about a shop."""
    make_removal(client, db_session, ctx, confidence=0.2)
    import_sale(client, ctx, offset_seconds=6000)

    results = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()
    assert results[0]["status"] == "UNMATCHED"
    assert results[0]["event_id"] is None  # recorded, but not concluded from


def test_a_restock_is_never_correlated(client, db_session, ctx):
    from app.services import inventory as inventory_service

    scope = scope_for(client, db_session, ctx)
    inventory_service.record_event(
        db_session,
        scope,
        uuid.UUID(ctx["shelf"]["id"]),
        event_type=InventoryEventType.SHELF_RESTOCKED,
        occupancy_before=0.2,
        occupancy_after=0.9,
        confidence=0.9,
        occurred_at=NOW,
    )
    db_session.commit()

    assert client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json() == []


def test_an_unmatched_explanation_names_the_innocent_causes(client, db_session, ctx):
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=6000)
    result = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()[0]

    lowered = result["explanation"].lower()
    assert "restocking" in lowered
    assert "detection error" in lowered
    for word in ("theft", "steal", "stolen", "suspect", "guilty"):
        assert word not in lowered


# ------------------------------------------------------------- the inference


def test_the_discrepancy_event_is_an_inference_not_an_accusation(client, db_session, ctx):
    """The first time POSSIBLE_INVENTORY_DISCREPANCY can fire in this codebase.

    It must arrive hedged, carrying what it rests on, and phrased so it
    survives being read aloud in a dispute.
    """
    inventory_event = make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=6000)

    result = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()[0]
    assert result["event_id"] is not None

    event = client.get(f"/api/v1/events/{result['event_id']}", headers=ctx["headers"]).json()

    assert event["event_type"] == "POSSIBLE_INVENTORY_DISCREPANCY"
    assert event["is_inference"] is True
    assert str(inventory_event.id) in event["source_event_ids"]
    assert event["rule_name"] == "pos_correlation"

    summary = event["summary"].lower()
    assert "no matching sale was found" in summary
    assert "needs a human to check" in summary
    for word in ("theft", "steal", "stolen", "thief", "suspect", "guilty", "culprit"):
        assert word not in summary


def test_the_inference_records_what_it_rests_on(client, db_session, ctx):
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=6000)
    result = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()[0]

    event = client.get(f"/api/v1/events/{result['event_id']}", headers=ctx["headers"]).json()
    metadata = event["event_metadata"]
    assert metadata["correlation_result_id"] == result["id"]
    assert metadata["correlation_status"] == "UNMATCHED"
    assert metadata["window_seconds"] > 0


# ------------------------------------------------------------------- review


def test_a_correlation_can_be_reviewed(client, db_session, ctx):
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=20)
    result = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()[0]

    reviewed = client.post(
        f"/api/v1/pos/correlations/{result['id']}/review",
        json={"outcome": "CORRECT", "notes": "Receipt checked."},
        headers=ctx["headers"],
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["review_outcome"] == "CORRECT"
    assert reviewed.json()["reviewed_at"] is not None


def test_an_unknown_review_outcome_is_rejected(client, db_session, ctx):
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=20)
    result = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()[0]

    response = client.post(
        f"/api/v1/pos/correlations/{result['id']}/review",
        json={"outcome": "THEFT_CONFIRMED"},
        headers=ctx["headers"],
    )
    assert response.status_code == 422


def test_a_result_cannot_be_reviewed_twice(client, db_session, ctx):
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=20)
    result = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()[0]

    body = {"outcome": "CORRECT"}
    url = f"/api/v1/pos/correlations/{result['id']}/review"
    assert client.post(url, json=body, headers=ctx["headers"]).status_code == 200
    assert client.post(url, json=body, headers=ctx["headers"]).status_code == 409


def test_accuracy_is_null_until_something_is_judged(client, ctx):
    summary = client.get("/api/v1/pos/accuracy", headers=ctx["headers"]).json()
    assert summary["total"] == 0
    assert summary["accuracy"] is None


def test_accuracy_counts_human_verdicts(client, db_session, ctx):
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=20)
    result = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()[0]
    client.post(
        f"/api/v1/pos/correlations/{result['id']}/review",
        json={"outcome": "CORRECT"},
        headers=ctx["headers"],
    )

    summary = client.get("/api/v1/pos/accuracy", headers=ctx["headers"]).json()
    assert summary["reviewed"] == 1
    assert summary["review_correct"] == 1
    assert summary["accuracy"] == 1.0


def test_explained_results_are_counted_separately(client, db_session, ctx):
    """'Unmatched, and I know why' is not the system being wrong."""
    make_removal(client, db_session, ctx)
    import_sale(client, ctx, offset_seconds=6000)
    result = client.post("/api/v1/pos/correlate", json={}, headers=ctx["headers"]).json()[0]
    client.post(
        f"/api/v1/pos/correlations/{result['id']}/review",
        json={"outcome": "EXPLAINED", "notes": "Staff moved it to the end display."},
        headers=ctx["headers"],
    )

    summary = client.get("/api/v1/pos/accuracy", headers=ctx["headers"]).json()
    assert summary["review_explained"] == 1
    assert summary["accuracy"] is None  # neither correct nor incorrect


# ------------------------------------------------------------ configuration


def test_pos_provider_can_be_configured(client, ctx):
    response = client.put(
        f"/api/v1/pos/config/{ctx['store']['id']}",
        json={"provider": "mock", "options": {}},
        headers=ctx["headers"],
    )
    assert response.status_code == 200
    assert response.json()["provider"] == "mock"
    assert "csv" in response.json()["available_providers"]


def test_an_unknown_provider_is_rejected(client, ctx):
    response = client.put(
        f"/api/v1/pos/config/{ctx['store']['id']}",
        json={"provider": "not-a-real-till"},
        headers=ctx["headers"],
    )
    assert response.status_code == 422


def test_pos_config_can_be_cleared(client, ctx):
    client.put(
        f"/api/v1/pos/config/{ctx['store']['id']}",
        json={"provider": "mock"},
        headers=ctx["headers"],
    )
    assert (
        client.delete(
            f"/api/v1/pos/config/{ctx['store']['id']}", headers=ctx["headers"]
        ).status_code
        == 200
    )
    config = client.get(f"/api/v1/pos/config/{ctx['store']['id']}", headers=ctx["headers"]).json()
    assert config["provider"] is None


# ------------------------------------------------------------- permissions


def login_as(client, db_session, ctx, *, email: str, role):
    from app.core.security import hash_password
    from app.models import Organization
    from tests import factories as f

    org = db_session.get(Organization, uuid.UUID(ctx["organization_id"]))
    f.make_user(
        db_session,
        org,
        email=email,
        hashed_password=hash_password(PASSWORD),
        role=role,
    )
    db_session.commit()
    tokens = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()[
        "tokens"
    ]
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_a_viewer_cannot_see_sales_at_all(client, ctx, db_session):
    """Sales are commercially sensitive, so pos:read starts above VIEWER.

    A guard watching the camera feed has no business reading the takings.
    """
    from app.models.enums import UserRole

    headers = login_as(client, db_session, ctx, email="viewer@duka.co.tz", role=UserRole.VIEWER)
    assert client.get("/api/v1/pos/transactions", headers=headers).status_code == 403
    assert client.get("/api/v1/pos/correlations", headers=headers).status_code == 403


def test_an_analyst_can_read_but_not_import(client, ctx, db_session):
    from app.models.enums import UserRole

    headers = login_as(client, db_session, ctx, email="analyst@duka.co.tz", role=UserRole.ANALYST)
    assert client.get("/api/v1/pos/correlations", headers=headers).status_code == 200
    assert client.get("/api/v1/pos/accuracy", headers=headers).status_code == 200

    denied = client.post(
        "/api/v1/pos/transactions",
        json={
            "store_id": ctx["store"]["id"],
            "transactions": [{"external_id": "TX-A", "transaction_at": NOW.isoformat()}],
        },
        headers=headers,
    )
    assert denied.status_code == 403


def test_only_an_admin_may_configure_a_provider(client, ctx, db_session):
    from app.models.enums import UserRole

    headers = login_as(
        client, db_session, ctx, email="manager@duka.co.tz", role=UserRole.STORE_MANAGER
    )
    denied = client.put(
        f"/api/v1/pos/config/{ctx['store']['id']}",
        json={"provider": "mock"},
        headers=headers,
    )
    assert denied.status_code == 403


def test_unauthenticated_access_is_refused(client):
    assert client.get("/api/v1/pos/transactions").status_code == 401
    assert client.get("/api/v1/pos/accuracy").status_code == 401
