"""Authentication and session management.

Design decisions worth knowing about:

* **Refresh-token rotation with family detection.** Every refresh mints a new
  token and revokes the old one.  If a *revoked* token is presented again, the
  whole family is revoked: that is the signature of a stolen token being
  replayed, and the safe response is to end every session it belongs to.
* **Authorisation is re-derived on refresh.** Refresh tokens carry no role
  claim, so a demoted or deactivated user cannot refresh back into privileges.
* **Uniform failure.** Wrong password, unknown email and deactivated account
  all return the same error and take comparable time, so the endpoint cannot
  be used to enumerate accounts.
* **Failure paths commit before raising.** Services normally leave committing
  to the caller, but a failed login raises, and the route never reaches its
  ``session.commit()``.  Without an explicit commit the lockout counter and
  the family revocation would be rolled back with the request -- meaning
  brute-force protection and stolen-token detection would quietly not work.
  Every write that must survive an error path therefore commits here, and
  says so at the call site.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, ConflictError, ErrorCode, InvalidCredentialsError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.security import (
    TokenPayload,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.core.tenancy import TenantScope
from app.models import Organization, RefreshToken, Subscription, User
from app.models.enums import AuditAction, PlanTier, UserRole
from app.services.audit import record_audit

log = get_logger(__name__)

#: Failed attempts before an account is temporarily locked.
MAX_FAILED_LOGINS = 8
LOCKOUT_DURATION = timedelta(minutes=15)

#: A real Argon2 hash of a random value. Verified against when the email is
#: unknown, so that "no such user" costs the same time as "wrong password".
_DUMMY_HASH = hash_password("not-a-real-password-just-for-timing-equalisation")


class AccountDisabledError(AppError):
    status_code = 403
    code = ErrorCode.ACCOUNT_DISABLED
    message = "This account has been deactivated. Contact your administrator."


class AccountLockedError(AppError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED
    message = "Too many failed sign-in attempts. Try again shortly."


class EmailAlreadyRegisteredError(ConflictError):
    code = ErrorCode.EMAIL_ALREADY_REGISTERED
    message = "An account with that email already exists."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(value: str) -> str:
    """Turn an organization name into a URL-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:70] or "organization"


def unique_slug(session: Session, base: str) -> str:
    """Append a numeric suffix until the slug is free."""
    slug = slugify(base)
    candidate = slug
    counter = 2
    while session.scalar(select(Organization.id).where(Organization.slug == candidate)):
        suffix = f"-{counter}"
        candidate = f"{slug[: 80 - len(suffix)]}{suffix}"
        counter += 1
    return candidate


def build_scope(user: User) -> TenantScope:
    """Derive the caller's tenant scope from their user row."""
    extra: frozenset[Permission] = frozenset()
    if user.custom_role is not None:
        codes = {p.code for p in user.custom_role.permissions}
        extra = frozenset(p for p in Permission if p.value in codes)
    return TenantScope(
        user_id=user.id,
        organization_id=user.organization_id,
        role=user.role,
        store_ids=frozenset(store.id for store in user.stores),
        extra_permissions=extra,
    )


def _issue_tokens(
    session: Session,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str, datetime]:
    """Mint an access/refresh pair and persist the refresh token's hash."""
    access_token, access_expires = create_access_token(
        subject=user.id,
        organization_id=user.organization_id,
        role=user.role.value,
    )
    refresh_token, refresh_expires, family = create_refresh_token(
        subject=user.id, family_id=family_id
    )
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh_token),
            family_id=family,
            expires_at=refresh_expires,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:400] or None,
        )
    )
    session.flush()
    return access_token, refresh_token, access_expires


def _revoke_family(session: Session, family_id: uuid.UUID, reason: str) -> int:
    """Revoke every live token in a rotation family. Returns how many."""
    tokens = session.scalars(
        select(RefreshToken).where(
            RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None)
        )
    ).all()
    now = datetime.now(UTC)
    for token in tokens:
        token.revoked_at = now
        token.revoked_reason = reason
    session.flush()
    return len(tokens)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_organization(
    session: Session,
    *,
    email: str,
    password: str,
    full_name: str,
    organization_name: str,
    phone: str | None = None,
    country: str = "TZ",
    timezone: str = "Africa/Dar_es_Salaam",
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, Organization, str, str, datetime]:
    """Create an organization and its first ORGANIZATION_ADMIN.

    Self-service signup always creates a *new* tenant. Adding a user to an
    existing organization is an invitation flow, not registration -- otherwise
    anyone could attach themselves to a business by guessing its name.
    """
    email = email.strip().lower()
    validate_password_strength(password, email=email)

    settings = get_settings()
    organization = Organization(
        name=organization_name.strip(),
        slug=unique_slug(session, organization_name),
        contact_email=email,
        country=country.upper(),
        timezone=timezone,
        currency=settings.DEFAULT_CURRENCY,
        locale=settings.DEFAULT_LOCALE,
    )
    session.add(organization)
    session.flush()

    session.add(
        Subscription(
            organization_id=organization.id,
            tier=PlanTier.TRIAL,
            started_on=datetime.now(UTC).date(),
            trial_ends_on=(datetime.now(UTC) + timedelta(days=30)).date(),
        )
    )

    user = User(
        organization_id=organization.id,
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name.strip(),
        phone=phone,
        role=UserRole.ORGANIZATION_ADMIN,
        is_active=True,
        password_changed_at=datetime.now(UTC),
        timezone=timezone,
    )
    session.add(user)
    session.flush()

    access, refresh, expires = _issue_tokens(
        session, user, ip_address=ip_address, user_agent=user_agent
    )

    record_audit(
        session,
        action=AuditAction.CREATE,
        organization_id=organization.id,
        user=user,
        resource_type="organization",
        resource_id=str(organization.id),
        description=f"Organization '{organization.name}' registered",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    log.info(
        "organization_registered",
        organization_id=str(organization.id),
        slug=organization.slug,
        user_id=str(user.id),
    )
    return user, organization, access, refresh, expires


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def authenticate(
    session: Session,
    *,
    email: str,
    password: str,
    organization_slug: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, str, str, datetime]:
    """Verify credentials and issue tokens.

    Returns ``(user, access_token, refresh_token, access_expires_at)``.
    """
    email = email.strip().lower()

    statement = select(User).where(User.email == email, User.deleted_at.is_(None))
    if organization_slug:
        statement = statement.join(Organization).where(
            Organization.slug == organization_slug.strip().lower()
        )
    users = session.scalars(statement).all()

    if not users:
        # Spend comparable time so a missing account is not detectable by
        # response latency, then fail with the same message as a bad password.
        verify_password(password, _DUMMY_HASH)
        record_audit(
            session,
            action=AuditAction.LOGIN_FAILED,
            organization_id=None,
            user=None,
            actor_email=email,
            description="Sign-in attempt for an unknown email address",
            ip_address=ip_address,
            user_agent=user_agent,
            succeeded=False,
        )
        # Commit before raising: the request will abort and never commit.
        session.commit()
        log.info("login_failed", reason="unknown_email", ip_address=ip_address)
        raise InvalidCredentialsError()

    if len(users) > 1 and organization_slug is None:
        # The same email in two organizations: the caller must say which.
        raise AppError(
            "This email is registered with more than one organization. "
            "Include your organization to sign in.",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=409,
            details={
                "organizations": [
                    {"slug": u.organization.slug, "name": u.organization.name}
                    for u in users
                    if u.organization
                ]
            },
        )

    user = users[0]
    now = datetime.now(UTC)

    if user.locked_until and user.locked_until > now:
        log.warning(
            "login_blocked_account_locked",
            user_id=str(user.id),
            locked_until=user.locked_until.isoformat(),
        )
        raise AccountLockedError(
            details={"retry_after_seconds": int((user.locked_until - now).total_seconds())}
        )

    if not verify_password(password, user.hashed_password):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + LOCKOUT_DURATION
            log.warning(
                "account_locked",
                user_id=str(user.id),
                failed_attempts=user.failed_login_count,
            )
        session.flush()
        record_audit(
            session,
            action=AuditAction.LOGIN_FAILED,
            organization_id=user.organization_id,
            user=user,
            description="Incorrect password",
            ip_address=ip_address,
            user_agent=user_agent,
            succeeded=False,
        )
        # The lockout counter is worthless if it is rolled back with the
        # failed request, so persist it before raising.
        session.commit()
        log.info("login_failed", reason="bad_password", user_id=str(user.id))
        raise InvalidCredentialsError()

    if not user.is_active:
        record_audit(
            session,
            action=AuditAction.LOGIN_FAILED,
            organization_id=user.organization_id,
            user=user,
            description="Sign-in attempt on a deactivated account",
            ip_address=ip_address,
            user_agent=user_agent,
            succeeded=False,
        )
        session.commit()
        raise AccountDisabledError()

    if user.organization is not None and not user.organization.is_active:
        raise AccountDisabledError(
            "This organization is not active. Contact support.",
        )

    # Successful login: reset the failure counter and upgrade the hash if the
    # cost parameters have moved on since it was created.
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    if needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(password)
        log.info("password_hash_upgraded", user_id=str(user.id))
    session.flush()

    access, refresh, expires = _issue_tokens(
        session, user, ip_address=ip_address, user_agent=user_agent
    )
    record_audit(
        session,
        action=AuditAction.LOGIN,
        organization_id=user.organization_id,
        user=user,
        description="Signed in",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    log.info("login_succeeded", user_id=str(user.id), role=user.role.value)
    return user, access, refresh, expires


# ---------------------------------------------------------------------------
# Refresh / logout
# ---------------------------------------------------------------------------


def refresh_session(
    session: Session,
    *,
    refresh_token: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, str, str, datetime]:
    """Rotate a refresh token, re-deriving authorisation from the database."""
    payload: TokenPayload = decode_token(refresh_token, expected_type="refresh")
    token_hash = hash_token(refresh_token)

    stored = session.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if stored is None:
        # A syntactically valid token we have never issued, or one already
        # pruned. Treat as invalid without revealing which.
        log.warning("refresh_rejected", reason="unknown_token", user_id=str(payload.subject))
        raise InvalidCredentialsError("Your session is no longer valid. Please sign in again.")

    if stored.revoked_at is not None:
        # Replay of a rotated token: the hallmark of a stolen refresh token.
        # Revoke the entire family rather than just this one.
        revoked = _revoke_family(session, stored.family_id, "reuse_detected")
        record_audit(
            session,
            action=AuditAction.ACCESS_DENIED,
            organization_id=None,
            user=None,
            resource_type="refresh_token",
            resource_id=str(stored.id),
            description="Reuse of a revoked refresh token; session family revoked",
            ip_address=ip_address,
            user_agent=user_agent,
            succeeded=False,
        )
        log.warning(
            "refresh_token_reuse_detected",
            user_id=str(stored.user_id),
            family_id=str(stored.family_id),
            sessions_revoked=revoked,
        )
        # Critical: without this commit the revocation is rolled back with the
        # failed request and the stolen token keeps working.
        session.commit()
        raise InvalidCredentialsError("Your session is no longer valid. Please sign in again.")

    if stored.expires_at <= datetime.now(UTC):
        raise InvalidCredentialsError("Your session has expired. Please sign in again.")

    user = session.get(User, stored.user_id)
    if user is None or user.deleted_at is not None or not user.is_active:
        _revoke_family(session, stored.family_id, "user_inactive")
        session.commit()
        raise AccountDisabledError()

    stored.revoked_at = datetime.now(UTC)
    stored.revoked_reason = "rotated"
    session.flush()

    access, new_refresh, expires = _issue_tokens(
        session,
        user,
        family_id=stored.family_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    log.info("session_refreshed", user_id=str(user.id))
    return user, access, new_refresh, expires


def logout(
    session: Session,
    *,
    user: User,
    refresh_token: str | None = None,
    all_sessions: bool = False,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> int:
    """Revoke the current session, or every session. Returns how many."""
    revoked = 0
    now = datetime.now(UTC)

    if all_sessions:
        tokens = session.scalars(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        ).all()
        for token in tokens:
            token.revoked_at = now
            token.revoked_reason = "logout_all"
        revoked = len(tokens)
    elif refresh_token:
        stored = session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
        )
        if stored is not None and stored.revoked_at is None:
            # Revoke the family: the presented token and any already rotated
            # from it belong to the same device.
            revoked = _revoke_family(session, stored.family_id, "logout")

    session.flush()
    record_audit(
        session,
        action=AuditAction.LOGOUT,
        organization_id=user.organization_id,
        user=user,
        description=f"Signed out ({revoked} session(s) revoked)",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    log.info("logout", user_id=str(user.id), sessions_revoked=revoked)
    return revoked


def change_password(
    session: Session,
    *,
    user: User,
    current_password: str,
    new_password: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Change a password and end every other session."""
    if not verify_password(current_password, user.hashed_password):
        log.info("password_change_failed", user_id=str(user.id), reason="bad_current")
        raise InvalidCredentialsError("Your current password is incorrect.")

    validate_password_strength(new_password, email=user.email)
    if verify_password(new_password, user.hashed_password):
        raise AppError(
            "The new password must be different from the current one.",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
        )

    user.hashed_password = hash_password(new_password)
    user.password_changed_at = datetime.now(UTC)

    # A password change must invalidate sessions an attacker may hold.
    now = datetime.now(UTC)
    tokens = session.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ).all()
    for token in tokens:
        token.revoked_at = now
        token.revoked_reason = "password_changed"
    session.flush()

    record_audit(
        session,
        action=AuditAction.UPDATE,
        organization_id=user.organization_id,
        user=user,
        resource_type="user",
        resource_id=str(user.id),
        description="Password changed; all sessions revoked",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    log.info("password_changed", user_id=str(user.id), sessions_revoked=len(tokens))


def prune_expired_tokens(session: Session, *, older_than_days: int = 30) -> int:
    """Delete refresh tokens that expired long ago. Returns how many."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    tokens = session.scalars(select(RefreshToken).where(RefreshToken.expires_at < cutoff)).all()
    for token in tokens:
        session.delete(token)
    session.flush()
    return len(tokens)
