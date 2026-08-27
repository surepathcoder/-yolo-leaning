"""Security primitives: password hashing, JWTs, and credential encryption.

Everything cryptographic lives here so it can be reviewed in one place.
Nothing in this module touches the database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

log = get_logger(__name__)

TokenType = Literal["access", "refresh"]


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
#
# Argon2id, the current password-hashing recommendation. Parameters follow
# OWASP guidance (19 MiB memory, 2 iterations) -- deliberately expensive enough
# to make offline cracking impractical, cheap enough that a login on a modest
# edge box stays under ~50 ms.
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)

#: Minimum acceptable password length. Length beats composition rules.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256


class WeakPasswordError(AppError):
    status_code = 422
    code = ErrorCode.VALIDATION_ERROR
    message = "Password does not meet the minimum requirements."


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    if not password:
        raise WeakPasswordError("Password must not be empty.")
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Check a password against a stored hash.

    Returns ``False`` for every failure mode, including a malformed stored
    hash, so a corrupt row cannot turn into an authentication bypass.
    """
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception as exc:  # pragma: no cover - defensive
        log.error("password_verification_error", error_type=type(exc).__name__)
        return False


def needs_rehash(hashed: str) -> bool:
    """Whether a stored hash uses outdated parameters and should be upgraded."""
    try:
        return _hasher.check_needs_rehash(hashed)
    except Exception:  # pragma: no cover - defensive
        return False


def validate_password_strength(password: str, *, email: str | None = None) -> None:
    """Raise :class:`WeakPasswordError` if a password is unacceptable.

    Length is the dominant factor, so the bar is 12 characters rather than a
    thicket of composition rules that push people towards ``Passw0rd!``.  The
    remaining checks block the two patterns that actually show up in breaches:
    the password being the email, and a handful of universally common values.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    if len(password) > MAX_PASSWORD_LENGTH:
        # Argon2 is happy with long inputs, but an unbounded field is a cheap
        # denial-of-service vector.
        raise WeakPasswordError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters long.")

    lowered = password.lower()
    if email:
        local_part = email.split("@", 1)[0].lower()
        # Only meaningful for a local part long enough to be a real name.
        # "a@..." would otherwise be a substring of almost every password.
        if len(local_part) >= 4 and local_part in lowered:
            raise WeakPasswordError("Password must not contain your email address.")

    common = {
        "password",
        "12345678",
        "qwerty",
        "letmein",
        "admin123",
        "welcome1",
        "visionguard",
        "changeme",
    }
    if lowered in common or any(lowered.startswith(c) and len(lowered) < 16 for c in common):
        raise WeakPasswordError("Password is too common. Choose something less predictable.")


def generate_password(length: int = 20) -> str:
    """Generate a strong random password (used by the seeder and invitations)."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%^&*-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# JSON Web Tokens
# ---------------------------------------------------------------------------


class TokenError(AppError):
    status_code = 401
    code = ErrorCode.TOKEN_INVALID
    message = "The provided token is not valid."


class TokenExpiredError(TokenError):
    code = ErrorCode.TOKEN_EXPIRED
    message = "Your session has expired. Please sign in again."


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """A decoded, verified token."""

    subject: uuid.UUID
    token_type: TokenType
    jti: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    organization_id: uuid.UUID | None = None
    role: str | None = None
    #: Rotation family, present on refresh tokens only.
    family_id: uuid.UUID | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(
    *,
    subject: uuid.UUID,
    organization_id: uuid.UUID | None,
    role: str,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, datetime]:
    """Mint a short-lived access token. Returns ``(token, expires_at)``.

    The organization and role are embedded so ordinary requests need no
    database round trip.  They are *cached authorisation*: a role change takes
    effect for existing tokens only when they expire, which is why access
    tokens are short-lived and why anything destructive re-reads the user.
    """
    settings = get_settings()
    issued = _now()
    expires = issued + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    claims: dict[str, Any] = {
        "sub": str(subject),
        "typ": "access",
        "jti": str(uuid.uuid4()),
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "org": str(organization_id) if organization_id else None,
        "role": role,
    }
    if extra_claims:
        claims.update(extra_claims)
    token = jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires


def create_refresh_token(
    *,
    subject: uuid.UUID,
    family_id: uuid.UUID | None = None,
    expires_delta: timedelta | None = None,
) -> tuple[str, datetime, uuid.UUID]:
    """Mint a refresh token. Returns ``(token, expires_at, family_id)``.

    Refresh tokens carry no role or organization claim: authorisation is
    re-derived from the database when they are exchanged, so a demoted or
    disabled user cannot refresh their way back into their old privileges.
    """
    settings = get_settings()
    issued = _now()
    expires = issued + (expires_delta or timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))
    family = family_id or uuid.uuid4()
    claims = {
        "sub": str(subject),
        "typ": "refresh",
        "jti": str(uuid.uuid4()),
        "fam": str(family),
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires, family


def decode_token(token: str, *, expected_type: TokenType | None = None) -> TokenPayload:
    """Verify and decode a token.

    Raises :class:`TokenExpiredError` or :class:`TokenError`.  The token type
    is checked here rather than at the call site, so a refresh token can never
    be presented as an access token.
    """
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(internal_message=f"jwt decode failed: {exc}") from exc

    token_type = claims.get("typ")
    if expected_type is not None and token_type != expected_type:
        raise TokenError(internal_message=f"expected {expected_type} token, got {token_type!r}")

    try:
        subject = uuid.UUID(claims["sub"])
        jti = uuid.UUID(claims["jti"])
    except (KeyError, ValueError) as exc:
        raise TokenError(internal_message="malformed subject or jti claim") from exc

    org_claim = claims.get("org")
    family_claim = claims.get("fam")
    return TokenPayload(
        subject=subject,
        token_type=token_type,  # type: ignore[arg-type]
        jti=jti,
        issued_at=datetime.fromtimestamp(claims["iat"], tz=UTC),
        expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        organization_id=uuid.UUID(org_claim) if org_claim else None,
        role=claims.get("role"),
        family_id=uuid.UUID(family_claim) if family_claim else None,
    )


def hash_token(token: str) -> str:
    """SHA-256 of a token, for storage.

    Refresh tokens are stored hashed so a database leak does not hand an
    attacker usable sessions.  A plain hash (not Argon2) is right here: the
    input is 200+ bits of entropy, so there is nothing to brute-force, and
    lookups must stay fast.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    """Timing-safe string comparison."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Credential encryption (camera passwords)
# ---------------------------------------------------------------------------


class CredentialEncryptionError(AppError):
    status_code = 500
    code = ErrorCode.INTERNAL_ERROR
    message = "Unable to process stored credentials."


def _derive_dev_key(secret: str) -> bytes:
    """Derive a Fernet key from JWT_SECRET for development only.

    Production must set ``CREDENTIALS_ENCRYPTION_KEY`` explicitly:
    :func:`get_fernet` refuses to start otherwise.
    """
    digest = hashlib.sha256(f"visionguard-credentials::{secret}".encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet: Fernet | None = None


def get_fernet() -> Fernet:
    """Return the credential cipher, refusing an unsafe production setup."""
    global _fernet
    if _fernet is not None:
        return _fernet

    settings = get_settings()
    configured = settings.CREDENTIALS_ENCRYPTION_KEY.strip()
    if configured:
        try:
            _fernet = Fernet(configured.encode())
        except (ValueError, TypeError) as exc:
            raise CredentialEncryptionError(
                internal_message="CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key"
            ) from exc
        return _fernet

    if settings.is_production:
        raise CredentialEncryptionError(
            internal_message=(
                "CREDENTIALS_ENCRYPTION_KEY must be set in production; "
                "refusing to derive a key from JWT_SECRET"
            )
        )

    log.warning(
        "credentials_key_derived_from_jwt_secret",
        detail="set CREDENTIALS_ENCRYPTION_KEY before deploying",
    )
    _fernet = Fernet(_derive_dev_key(settings.JWT_SECRET))
    return _fernet


def reset_fernet() -> None:
    """Clear the cached cipher (tests that change configuration)."""
    global _fernet
    _fernet = None


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a camera password for storage."""
    if plaintext is None:
        raise CredentialEncryptionError(internal_message="cannot encrypt None")
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_credential(ciphertext: str) -> str:
    """Decrypt a stored camera password.

    Only the vision layer should call this, and the result must never be
    logged or serialised into an API response.
    """
    try:
        return get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise CredentialEncryptionError(
            internal_message="stored credential could not be decrypted"
        ) from exc
