# Security

## Threat model

VisionGuard holds camera credentials, live video access and behavioural records
for multiple independent businesses. The threats that matter, ranked by damage:

| # | Threat | Why it is the worst case | Primary control |
|---|--------|--------------------------|-----------------|
| 1 | Cross-tenant data access | One customer watching another's shop ends the product | `TenantScope`, enforced on every query |
| 2 | Camera credential theft | Grants live video access independent of VisionGuard | Fernet encryption at rest; credentials never serialised |
| 3 | Account takeover | Full access to evidence and configuration | Argon2id, lockout, refresh rotation with reuse detection |
| 4 | Evidence tampering or leakage | Destroys the record's value and the customer's trust | Authorised access, checksums, audit log |
| 5 | Insider misuse | An operator watching a person rather than a premises | Least-privilege roles, audit log, no facial recognition |

## Authentication

**Passwords** — Argon2id with OWASP parameters (19 MiB, t=2, p=1). A minimum of
12 characters; length is the requirement rather than composition rules, which
push people towards `Passw0rd!`. Passwords may not contain the email local part
(when it is 4+ characters) or appear in a small list of universally common
values. Hashes are upgraded transparently on login when parameters change.

**Login failure is uniform.** Unknown email, wrong password and deactivated
account all return `401 INVALID_CREDENTIALS` with an identical body. An unknown
email is still verified against a real dummy hash so the response time matches.
The endpoint cannot be used to enumerate accounts.

**Lockout** — 8 consecutive failures locks the account for 15 minutes. The
counter resets on success.

**Tokens**

| | Access | Refresh |
|---|---|---|
| Lifetime | 30 minutes (configurable) | 14 days |
| Claims | `sub`, `org`, `role`, `jti`, `typ` | `sub`, `fam`, `jti`, `typ` — **no role** |
| Stored | Not stored | SHA-256 hash only |
| On use | User row is re-read every request | Rotated; old token revoked |

The `typ` claim is verified, so a refresh token can never be presented where an
access token is expected.

Refresh tokens carry **no role claim** so authorisation is re-derived from the
database on every refresh: a demoted or deactivated user cannot refresh back
into their old privileges.

**Refresh rotation with reuse detection.** Each refresh mints a new token and
revokes the old one. Presenting an *already revoked* token is the signature of
a stolen token being replayed, so the entire rotation family is revoked — the
attacker and the legitimate user are both signed out, and the user notices.

**Failure paths commit before raising.** A failed login raises, so the request
aborts and the route never reaches its commit. The lockout counter and family
revocation are committed explicitly inside the service. Without that,
brute-force protection and stolen-token detection would silently do nothing —
a bug that a test suite sharing one session can easily hide, so
`test_auth_api.py` includes durability tests that roll back before asserting.

## Authorisation

Two independent checks, both required:

1. **Permission** — does this role hold `camera:write`? See
   `app/core/permissions.py`. The catalogue lives in code, because adding a
   permission means adding a check, and the two must ship together.
2. **Tenant scope** — does this caller's organization own the row? See
   `app/core/tenancy.py`.

`TenantScope` is a frozen dataclass derived once from the authenticated user.
Code further down the stack cannot widen its own access. Every tenant query
either goes through `scope.apply()` or calls `scope.require_organization()`.

`SUPER_ADMIN` is the only role that may cross organizations. Every such access
is written to the audit log **against the target organization**, so the customer
sees it in their own audit view.

The system fails **closed**: a non-super-admin with no organization can see
nothing, and `scope.apply()` raises rather than returning an unfiltered query.

## Evidence

Snapshots and clips are files on disk, referenced by a **relative** path and a
SHA-256. They are never public URLs:

- `GET /api/v1/evidence/{id}` requires `evidence:view`, is tenant-scoped
  through the parent event (so a store-restricted operator is held to their
  stores), and writes an `EVIDENCE_ACCESSED` audit row on every hit.
- `GET /api/v1/evidence/{id}/download` additionally requires `evidence:export`
  and audits as `EXPORT`.
- The dashboard fetches the bytes through the authenticated API client, not an
  `<img src>` — a plain image request carries no bearer token.
- A file removed by retention is a 404, not an error.

See [`EVIDENCE.md`](EVIDENCE.md) for what is captured and when.

## Credentials

Camera passwords are encrypted with Fernet (AES-128-CBC + HMAC) using
`CREDENTIALS_ENCRYPTION_KEY`.

- The RTSP URL is **never stored as a column**. Host, port and path are stored
  in the clear (not secret, needed for diagnostics); the username and password
  are separate, and the playable URL is assembled in memory inside the vision
  layer only.
- Camera passwords are never returned by any API response.
- In production, `get_fernet()` **refuses to start** without an explicit key
  rather than deriving one from `JWT_SECRET`.
- Encryption is non-deterministic, so identical passwords do not produce
  identical ciphertext.

## Secrets in logs

Two independent layers:

1. `app/core/logging.py` redacts a known set of sensitive keys from every log
   event before rendering.
2. `app/services/audit.py` sanitises the `changes` diff recursively before it
   is *persisted* — a stronger requirement than log redaction, because the
   data is stored rather than merely printed.

Internal exception text never reaches a user. Unhandled exceptions are logged
with a generated `incident_id` and the client receives only that id.

## API surface

- Consistent error envelope; no stack traces, no SQL, no internal paths.
- Request validation via Pydantic with `extra="forbid"` on auth inputs.
- Field-level validation errors report the field and reason but **never the
  submitted value**, which may be a password.
- Every response carries `X-Request-ID` for correlation.
- CORS is an explicit allowlist from configuration, never `*` with credentials.

## Audit log

Append-only: no `updated_at`, no soft delete. A row that can be edited is not
an audit row.

Recorded: authentication (success and failure), permission and configuration
changes, evidence access, exports, and every cross-tenant operation. The
actor's email and role are copied onto the row so the trail stays readable
after a user is deleted.

## Checklist before deploying

- [ ] `JWT_SECRET` replaced with 64+ random bytes
- [ ] `CREDENTIALS_ENCRYPTION_KEY` set to a generated Fernet key
- [ ] `ENVIRONMENT=production` and `DEBUG=false`
- [ ] `CORS_ORIGINS` restricted to the real dashboard origin
- [ ] PostgreSQL and Redis not exposed to the public internet
- [ ] TLS terminated in front of the API; HSTS enabled
- [ ] `LOG_FORMAT=json` and logs shipped off the box
- [ ] Database backups configured and a restore tested
- [ ] Default seeded accounts removed or their passwords rotated

## Reporting

Security issues should be reported privately to the maintainers, not filed as
public issues.
