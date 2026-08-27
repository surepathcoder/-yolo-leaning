"""Milestone 2 -- cryptographic primitives."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.security import (
    MIN_PASSWORD_LENGTH,
    TokenError,
    TokenExpiredError,
    WeakPasswordError,
    constant_time_compare,
    create_access_token,
    create_refresh_token,
    decode_token,
    decrypt_credential,
    encrypt_credential,
    generate_password,
    hash_password,
    hash_token,
    validate_password_strength,
    verify_password,
)


# ------------------------------------------------------------------ hashing
def test_password_hash_is_argon2id():
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")


def test_password_verifies():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong password entirely", hashed) is False


def test_hashes_are_salted():
    """Two users with the same password must not share a hash."""
    a = hash_password("identical-password-123")
    b = hash_password("identical-password-123")
    assert a != b
    assert verify_password("identical-password-123", a)
    assert verify_password("identical-password-123", b)


def test_verify_returns_false_for_a_corrupt_hash():
    """A malformed stored hash must not become an authentication bypass."""
    assert verify_password("anything", "not-a-hash") is False
    assert verify_password("anything", "") is False
    assert verify_password("anything", "$argon2id$garbage") is False


def test_empty_password_is_rejected():
    with pytest.raises(WeakPasswordError):
        hash_password("")


# ------------------------------------------------------------ password rules
def test_short_passwords_are_rejected():
    with pytest.raises(WeakPasswordError) as exc:
        validate_password_strength("short")
    assert str(MIN_PASSWORD_LENGTH) in exc.value.message


def test_absurdly_long_passwords_are_rejected():
    with pytest.raises(WeakPasswordError):
        validate_password_strength("a" * 500)


def test_password_may_not_contain_the_email_local_part():
    with pytest.raises(WeakPasswordError):
        validate_password_strength("juma-mwangi-2024", email="juma@example.co.tz")


def test_common_passwords_are_rejected():
    for candidate in ("password", "visionguard", "changeme"):
        with pytest.raises(WeakPasswordError):
            validate_password_strength(candidate)


def test_a_good_password_passes():
    validate_password_strength("tembo-anga-baridi-91", email="ops@example.co.tz")


def test_generated_passwords_are_strong_and_unique():
    generated = {generate_password() for _ in range(20)}
    assert len(generated) == 20
    for candidate in generated:
        validate_password_strength(candidate)


# ------------------------------------------------------------------- tokens
def test_access_token_round_trips():
    user_id, org_id = uuid.uuid4(), uuid.uuid4()
    token, expires = create_access_token(
        subject=user_id, organization_id=org_id, role="STORE_MANAGER"
    )
    payload = decode_token(token, expected_type="access")
    assert payload.subject == user_id
    assert payload.organization_id == org_id
    assert payload.role == "STORE_MANAGER"
    assert payload.token_type == "access"
    assert payload.expires_at.replace(microsecond=0) == expires.replace(microsecond=0)


def test_refresh_token_carries_no_role_claim():
    """Authorisation must be re-derived on refresh, not replayed from a claim."""
    token, _expires, family = create_refresh_token(subject=uuid.uuid4())
    payload = decode_token(token, expected_type="refresh")
    assert payload.role is None
    assert payload.organization_id is None
    assert payload.family_id == family


def test_a_refresh_token_cannot_be_used_as_an_access_token():
    token, _, _ = create_refresh_token(subject=uuid.uuid4())
    with pytest.raises(TokenError):
        decode_token(token, expected_type="access")


def test_an_access_token_cannot_be_used_as_a_refresh_token():
    token, _ = create_access_token(subject=uuid.uuid4(), organization_id=None, role="VIEWER")
    with pytest.raises(TokenError):
        decode_token(token, expected_type="refresh")


def test_expired_tokens_are_rejected():
    token, _ = create_access_token(
        subject=uuid.uuid4(),
        organization_id=None,
        role="VIEWER",
        expires_delta=timedelta(seconds=-10),
    )
    with pytest.raises(TokenExpiredError):
        decode_token(token)


def test_tampered_tokens_are_rejected():
    token, _ = create_access_token(subject=uuid.uuid4(), organization_id=None, role="VIEWER")
    header, body, signature = token.split(".")
    forged = f"{header}.{body}.{'A' * len(signature)}"
    with pytest.raises(TokenError):
        decode_token(forged)


def test_garbage_tokens_are_rejected():
    for candidate in ("", "not.a.token", "a.b", "..."):
        with pytest.raises(TokenError):
            decode_token(candidate)


def test_a_token_signed_with_another_secret_is_rejected():
    import jwt as pyjwt

    from app.core.config import get_settings

    forged = pyjwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "typ": "access",
            "jti": str(uuid.uuid4()),
            "iat": 1_700_000_000,
            "exp": 4_000_000_000,
            "role": "SUPER_ADMIN",
        },
        "an-attackers-own-secret",
        algorithm=get_settings().JWT_ALGORITHM,
    )
    with pytest.raises(TokenError):
        decode_token(forged)


def test_each_token_has_a_unique_id():
    subject = uuid.uuid4()
    first, _ = create_access_token(subject=subject, organization_id=None, role="VIEWER")
    second, _ = create_access_token(subject=subject, organization_id=None, role="VIEWER")
    assert decode_token(first).jti != decode_token(second).jti


def test_token_hash_is_stable_and_opaque():
    token = "some-refresh-token-value"
    assert hash_token(token) == hash_token(token)
    assert len(hash_token(token)) == 64
    assert token not in hash_token(token)


def test_constant_time_compare():
    assert constant_time_compare("abc", "abc") is True
    assert constant_time_compare("abc", "abd") is False
    assert constant_time_compare("abc", "abcd") is False


# ------------------------------------------------- credential encryption
def test_camera_credentials_round_trip():
    secret = "Camera!Passw0rd"
    ciphertext = encrypt_credential(secret)
    assert secret not in ciphertext
    assert decrypt_credential(ciphertext) == secret


def test_encryption_is_non_deterministic():
    """Identical passwords must not produce identical ciphertext."""
    a = encrypt_credential("same-password")
    b = encrypt_credential("same-password")
    assert a != b
    assert decrypt_credential(a) == decrypt_credential(b) == "same-password"


def test_tampered_ciphertext_is_rejected():
    from app.core.security import CredentialEncryptionError

    ciphertext = encrypt_credential("secret")
    tampered = ciphertext[:-4] + "AAAA"
    with pytest.raises(CredentialEncryptionError):
        decrypt_credential(tampered)


def test_production_refuses_a_derived_encryption_key(monkeypatch):
    """A derived dev key must never be used to protect real credentials."""
    from app.core.config import Settings
    from app.core.security import CredentialEncryptionError, get_fernet, reset_fernet

    reset_fernet()
    production = Settings(_env_file=None, ENVIRONMENT="production", CREDENTIALS_ENCRYPTION_KEY="")
    monkeypatch.setattr("app.core.security.get_settings", lambda: production)
    try:
        with pytest.raises(CredentialEncryptionError):
            get_fernet()
    finally:
        reset_fernet()
