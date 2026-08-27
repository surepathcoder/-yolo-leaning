"""Milestone 2 -- the authentication API, end to end."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models import AuditLog, RefreshToken, User
from app.models.enums import AuditAction, UserRole
from tests import factories as f

GOOD_PASSWORD = "tembo-anga-baridi-91"


@pytest.fixture()
def registered(client):
    """Register an organization and return its tokens and payload."""
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "owner@duka.co.tz",
            "password": GOOD_PASSWORD,
            "full_name": "Amina Owner",
            "organization_name": "Duka la Amina",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth_header(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ----------------------------------------------------------------- register
def test_registration_creates_an_organization_and_admin(client, db_session, registered):
    assert registered["organization_slug"] == "duka-la-amina"
    assert registered["user"]["role"] == UserRole.ORGANIZATION_ADMIN.value
    assert registered["user"]["organization_name"] == "Duka la Amina"
    assert registered["tokens"]["token_type"] == "bearer"
    assert registered["tokens"]["expires_in"] > 0

    user = db_session.scalar(select(User).where(User.email == "owner@duka.co.tz"))
    assert user is not None
    assert user.hashed_password.startswith("$argon2id$")


def test_registration_response_never_contains_the_password(client, registered):
    body = str(registered)
    assert GOOD_PASSWORD not in body
    assert "hashed_password" not in body
    assert "$argon2id$" not in body


def test_registration_starts_a_trial_subscription(client, db_session, registered):
    from app.models import Subscription
    from app.models.enums import PlanTier

    subscription = db_session.scalar(
        select(Subscription).where(Subscription.organization_id == registered["organization_id"])
    )
    assert subscription is not None
    assert subscription.tier == PlanTier.TRIAL
    assert subscription.trial_ends_on is not None


def test_registration_rejects_a_weak_password(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "weak@duka.co.tz",
            "password": "short",
            "full_name": "Weak",
            "organization_name": "Weak Co",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_registration_rejects_a_password_containing_the_email(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "jumamwangi@duka.co.tz",
            "password": "jumamwangi-secure",
            "full_name": "Juma",
            "organization_name": "Juma Co",
        },
    )
    assert response.status_code == 422


def test_registration_rejects_a_malformed_email(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "not-an-email",
            "password": GOOD_PASSWORD,
            "full_name": "Nobody",
            "organization_name": "Nowhere",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["fields"][0]["field"] == "email"


def test_registration_slug_is_made_unique(client):
    first = client.post(
        "/api/v1/auth/register",
        json={
            "email": "a@x.co.tz",
            "password": GOOD_PASSWORD,
            "full_name": "A",
            "organization_name": "Same Name Ltd",
        },
    ).json()
    second = client.post(
        "/api/v1/auth/register",
        json={
            "email": "b@y.co.tz",
            "password": GOOD_PASSWORD,
            "full_name": "B",
            "organization_name": "Same Name Ltd",
        },
    ).json()
    assert first["organization_slug"] != second["organization_slug"]
    assert second["organization_slug"].startswith("same-name-ltd")


# -------------------------------------------------------------------- login
def test_login_succeeds(client, registered):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": GOOD_PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tokens"]["access_token"]
    assert body["user"]["email"] == "owner@duka.co.tz"
    assert "org:write" in body["user"]["permissions"]


def test_login_is_case_insensitive_on_email(client, registered):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "OWNER@DUKA.CO.TZ", "password": GOOD_PASSWORD},
    )
    assert response.status_code == 200


def test_login_with_a_wrong_password_fails(client, registered):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": "definitely-not-it"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_unknown_email_and_wrong_password_are_indistinguishable(client, registered):
    """The endpoint must not be usable to enumerate accounts."""
    unknown = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@nowhere.co.tz", "password": GOOD_PASSWORD},
    )
    wrong = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": "wrong-password-here"},
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_login_records_an_audit_entry(client, db_session, registered):
    client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": GOOD_PASSWORD},
    )
    entries = db_session.scalars(select(AuditLog).where(AuditLog.action == AuditAction.LOGIN)).all()
    assert len(entries) >= 1
    assert entries[-1].actor_email == "owner@duka.co.tz"


def test_failed_login_records_an_audit_entry(client, db_session, registered):
    client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": "nope-nope-nope"},
    )
    entry = db_session.scalars(
        select(AuditLog).where(AuditLog.action == AuditAction.LOGIN_FAILED)
    ).all()[-1]
    assert entry.succeeded is False
    assert GOOD_PASSWORD not in str(entry.changes)


def test_audit_log_never_stores_a_password(client, db_session, registered):
    client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": GOOD_PASSWORD},
    )
    for entry in db_session.scalars(select(AuditLog)).all():
        assert GOOD_PASSWORD not in str(entry.changes)
        assert GOOD_PASSWORD not in (entry.description or "")


def test_account_locks_after_repeated_failures(client, db_session, registered):
    for _ in range(8):
        client.post(
            "/api/v1/auth/login",
            json={"email": "owner@duka.co.tz", "password": "wrong-every-time"},
        )
    # Even the correct password is refused while locked.
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": GOOD_PASSWORD},
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.json()["error"]["details"]["retry_after_seconds"] > 0


def test_successful_login_resets_the_failure_counter(client, db_session, registered):
    for _ in range(3):
        client.post(
            "/api/v1/auth/login",
            json={"email": "owner@duka.co.tz", "password": "wrong"},
        )
    client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": GOOD_PASSWORD},
    )
    user = db_session.scalar(select(User).where(User.email == "owner@duka.co.tz"))
    db_session.refresh(user)
    assert user.failed_login_count == 0
    assert user.last_login_at is not None


def test_deactivated_account_cannot_sign_in(client, db_session, registered):
    user = db_session.scalar(select(User).where(User.email == "owner@duka.co.tz"))
    user.is_active = False
    db_session.flush()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": GOOD_PASSWORD},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"


def test_login_across_two_organizations_requires_disambiguation(client, db_session):
    """One person may run two businesses; the API must ask which."""
    for name, slug_email in (("Biz One", "a"), ("Biz Two", "b")):
        client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{slug_email}@seed.co.tz",
                "password": GOOD_PASSWORD,
                "full_name": "Seed",
                "organization_name": name,
            },
        )
    orgs = db_session.scalars(select(User)).all()
    shared_email = "dual@owner.co.tz"
    for user in orgs:
        db_session.add(
            User(
                organization_id=user.organization_id,
                email=shared_email,
                hashed_password=hash_password(GOOD_PASSWORD),
                full_name="Dual Owner",
                role=UserRole.ORGANIZATION_ADMIN,
            )
        )
    db_session.flush()

    ambiguous = client.post(
        "/api/v1/auth/login", json={"email": shared_email, "password": GOOD_PASSWORD}
    )
    assert ambiguous.status_code == 409
    assert "organizations" in ambiguous.json()["error"]["details"]

    slug = ambiguous.json()["error"]["details"]["organizations"][0]["slug"]
    resolved = client.post(
        "/api/v1/auth/login",
        json={
            "email": shared_email,
            "password": GOOD_PASSWORD,
            "organization_slug": slug,
        },
    )
    assert resolved.status_code == 200


# ---------------------------------------------------------------- protected
def test_me_requires_a_token(client):
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_me_returns_the_caller(client, registered):
    response = client.get("/api/v1/auth/me", headers=auth_header(registered["tokens"]))
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "owner@duka.co.tz"
    assert body["role"] == "ORGANIZATION_ADMIN"
    assert "camera:write" in body["permissions"]
    assert "hashed_password" not in body


def test_me_rejects_a_garbage_token(client):
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"TOKEN_INVALID", "NOT_AUTHENTICATED"}


def test_me_rejects_a_refresh_token(client, registered):
    """A refresh token must not be accepted where an access token is required."""
    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {registered['tokens']['refresh_token']}"},
    )
    assert response.status_code == 401


def test_deactivating_a_user_invalidates_their_live_token(client, db_session, registered):
    """The user row is authoritative, not the token's claims."""
    headers = auth_header(registered["tokens"])
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    user = db_session.scalar(select(User).where(User.email == "owner@duka.co.tz"))
    user.is_active = False
    db_session.flush()

    assert client.get("/api/v1/auth/me", headers=headers).status_code == 403


# ------------------------------------------------------------------ refresh
def test_refresh_rotates_both_tokens(client, registered):
    original = registered["tokens"]
    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": original["refresh_token"]}
    )
    assert response.status_code == 200
    rotated = response.json()
    assert rotated["refresh_token"] != original["refresh_token"]
    assert rotated["access_token"] != original["access_token"]

    # The new access token works.
    assert client.get("/api/v1/auth/me", headers=auth_header(rotated)).status_code == 200


def test_a_rotated_refresh_token_cannot_be_reused(client, registered):
    old = registered["tokens"]["refresh_token"]
    client.post("/api/v1/auth/refresh", json={"refresh_token": old})

    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": old})
    assert replay.status_code == 401


def test_reusing_a_revoked_token_revokes_the_whole_family(client, db_session, registered):
    """Replay is the signature of a stolen token: end every session it belongs to."""
    old = registered["tokens"]["refresh_token"]
    rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": old}).json()

    # An attacker replays the stolen (already rotated) token.
    client.post("/api/v1/auth/refresh", json={"refresh_token": old})

    # The legitimate user's newer token is now dead too.
    victim = client.post("/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert victim.status_code == 401

    live = db_session.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))).all()
    assert live == []


def test_refresh_rejects_an_unknown_token(client, registered):
    from app.core.security import create_refresh_token

    forged, _, _ = create_refresh_token(subject=registered["user"]["id"])
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": forged})
    assert response.status_code == 401


def test_refresh_rejects_an_expired_stored_token(client, db_session, registered):
    stored = db_session.scalars(select(RefreshToken)).all()[-1]
    stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    response = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": registered["tokens"]["refresh_token"]},
    )
    assert response.status_code == 401


def test_refresh_reflects_a_role_change(client, db_session, registered):
    """Authorisation is re-derived on refresh, not replayed from the old token."""
    user = db_session.scalar(select(User).where(User.email == "owner@duka.co.tz"))
    user.role = UserRole.VIEWER
    db_session.flush()

    rotated = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": registered["tokens"]["refresh_token"]},
    ).json()
    me = client.get("/api/v1/auth/me", headers=auth_header(rotated)).json()
    assert me["role"] == "VIEWER"
    assert "camera:write" not in me["permissions"]


# ------------------------------------------------------------------- logout
def test_logout_revokes_the_session(client, registered):
    tokens = registered["tokens"]
    response = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers=auth_header(tokens),
    )
    assert response.status_code == 200

    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401


def test_logout_all_sessions(client, db_session, registered):
    client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": GOOD_PASSWORD},
    )
    client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": GOOD_PASSWORD},
    )

    client.post(
        "/api/v1/auth/logout",
        json={"all_sessions": True},
        headers=auth_header(registered["tokens"]),
    )
    live = db_session.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))).all()
    assert live == []


def test_logout_requires_authentication(client, registered):
    response = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": registered["tokens"]["refresh_token"]},
    )
    assert response.status_code == 401


# ---------------------------------------------------------- change password
def test_change_password_works_and_ends_sessions(client, db_session, registered):
    new_password = "kilimanjaro-mvua-77"
    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": GOOD_PASSWORD, "new_password": new_password},
        headers=auth_header(registered["tokens"]),
    )
    assert response.status_code == 200

    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "owner@duka.co.tz", "password": GOOD_PASSWORD},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "owner@duka.co.tz", "password": new_password},
        ).status_code
        == 200
    )

    stale = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": registered["tokens"]["refresh_token"]},
    )
    assert stale.status_code == 401


def test_change_password_requires_the_current_one(client, registered):
    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "wrong", "new_password": "kilimanjaro-mvua-77"},
        headers=auth_header(registered["tokens"]),
    )
    assert response.status_code == 401


def test_change_password_rejects_reuse(client, registered):
    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": GOOD_PASSWORD, "new_password": GOOD_PASSWORD},
        headers=auth_header(registered["tokens"]),
    )
    assert response.status_code == 422


def test_change_password_rejects_a_weak_new_password(client, registered):
    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": GOOD_PASSWORD, "new_password": "short"},
        headers=auth_header(registered["tokens"]),
    )
    assert response.status_code == 422


# ----------------------------------------------------------------- sessions
def test_sessions_endpoint_lists_live_sessions(client, registered):
    client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": GOOD_PASSWORD},
    )
    response = client.get("/api/v1/auth/sessions", headers=auth_header(registered["tokens"]))
    assert response.status_code == 200
    sessions = response.json()
    assert len(sessions) >= 2
    for entry in sessions:
        assert "token_hash" not in entry
        assert "id" in entry and "expires_at" in entry


# ---------------------------------------------------------- tenant isolation
def test_a_user_cannot_see_another_organizations_data(client, db_session):
    """Two tenants, two tokens: neither may read the other's user record."""
    org_a = client.post(
        "/api/v1/auth/register",
        json={
            "email": "a@one.co.tz",
            "password": GOOD_PASSWORD,
            "full_name": "A",
            "organization_name": "Org One",
        },
    ).json()
    org_b = client.post(
        "/api/v1/auth/register",
        json={
            "email": "b@two.co.tz",
            "password": GOOD_PASSWORD,
            "full_name": "B",
            "organization_name": "Org Two",
        },
    ).json()

    assert org_a["organization_id"] != org_b["organization_id"]

    me_a = client.get("/api/v1/auth/me", headers=auth_header(org_a["tokens"])).json()
    me_b = client.get("/api/v1/auth/me", headers=auth_header(org_b["tokens"])).json()
    assert me_a["organization_id"] == org_a["organization_id"]
    assert me_b["organization_id"] == org_b["organization_id"]
    assert me_a["organization_id"] != me_b["organization_id"]


# ---------------------------------------------------------------- role scope
def test_viewer_permissions_are_limited(client, db_session):
    org = f.make_organization(db_session)
    f.make_user(
        db_session,
        org,
        email="viewer@duka.co.tz",
        hashed_password=hash_password(GOOD_PASSWORD),
        role=UserRole.VIEWER,
    )
    tokens = client.post(
        "/api/v1/auth/login",
        json={"email": "viewer@duka.co.tz", "password": GOOD_PASSWORD},
    ).json()["tokens"]

    me = client.get("/api/v1/auth/me", headers=auth_header(tokens)).json()
    assert "event:read" in me["permissions"]
    assert "camera:write" not in me["permissions"]
    assert "audit:read" not in me["permissions"]


def test_store_restricted_user_reports_its_stores(client, db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    user = f.make_user(
        db_session,
        org,
        email="manager@duka.co.tz",
        hashed_password=hash_password(GOOD_PASSWORD),
        role=UserRole.STORE_MANAGER,
    )
    user.stores.append(store)
    db_session.flush()

    tokens = client.post(
        "/api/v1/auth/login",
        json={"email": "manager@duka.co.tz", "password": GOOD_PASSWORD},
    ).json()["tokens"]
    me = client.get("/api/v1/auth/me", headers=auth_header(tokens)).json()
    assert me["is_store_restricted"] is True
    assert me["accessible_store_ids"] == [str(store.id)]


# ---------------------------------------------------- failure-path durability
#
# These cover a class of bug that unit tests sharing one session can hide: a
# service that flushes security state and then raises. The route never reaches
# its commit, so without an explicit commit in the service the write is rolled
# back with the request -- brute-force protection and stolen-token detection
# would silently do nothing in production while every test still passed.


def test_failed_login_counter_survives_the_aborted_request(client, db_session, registered):
    client.post(
        "/api/v1/auth/login",
        json={"email": "owner@duka.co.tz", "password": "wrong-password-here"},
    )
    # Discard anything merely pending: only committed state survives this.
    db_session.rollback()
    user = db_session.scalar(select(User).where(User.email == "owner@duka.co.tz"))
    assert user.failed_login_count == 1


def test_lockout_survives_the_aborted_request(client, db_session, registered):
    for _ in range(8):
        client.post(
            "/api/v1/auth/login",
            json={"email": "owner@duka.co.tz", "password": "wrong-password-here"},
        )
    db_session.rollback()
    user = db_session.scalar(select(User).where(User.email == "owner@duka.co.tz"))
    assert user.locked_until is not None
    assert user.locked_until > datetime.now(UTC)


def test_family_revocation_survives_the_aborted_request(client, db_session, registered):
    old = registered["tokens"]["refresh_token"]
    client.post("/api/v1/auth/refresh", json={"refresh_token": old})
    client.post("/api/v1/auth/refresh", json={"refresh_token": old})  # replay

    db_session.rollback()
    live = db_session.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))).all()
    assert live == [], "revocation must be committed, not rolled back with the request"


def test_failed_login_audit_entry_survives(client, db_session, registered):
    client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@nowhere.co.tz", "password": GOOD_PASSWORD},
    )
    db_session.rollback()
    entries = db_session.scalars(
        select(AuditLog).where(AuditLog.action == AuditAction.LOGIN_FAILED)
    ).all()
    assert len(entries) == 1
    assert entries[0].actor_email == "ghost@nowhere.co.tz"
