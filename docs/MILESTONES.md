# Milestone status

Each milestone is implemented, tested and verified before the next begins.
"Verified" means the tests pass **and** the behaviour was exercised against the
running system.

| # | Milestone | Status | Verified by |
|---|-----------|--------|-------------|
| 0 | Project foundation | ✅ Done | 26 tests; live `/health` against real PostgreSQL + Redis |
| 1 | Database schema | ✅ Done | 35 tables; migration round-trips on real PostgreSQL; 15 integration tests |
| 2 | Authentication & RBAC | ✅ Done | 44 API tests + 26 crypto tests; live smoke test against real server |
| 3 | Camera management | ⬜ Not started | |
| 4 | Video ingestion | ⬜ Not started | |
| 5 | YOLO detection | ⬜ Not started | |
| 6 | Object tracking | ⬜ Not started | |
| 7 | Zone engine | ⬜ Not started | |
| 8 | Line crossing & counting | ⬜ Not started | |
| 9 | Loitering | ⬜ Not started | |
| 10 | After-hours detection | ⬜ Not started | |
| 11 | Event engine | ⬜ Not started | |
| 12 | Alert engine | ⬜ Not started | |
| 13 | Event evidence | ⬜ Not started | |
| 14 | Retail inventory | ⬜ Not started | |
| 15 | Custom product detection | ⬜ Not started | |
| 16 | POS integration | ⬜ Not started | |
| 17 | Dashboard | ⬜ Not started | |

---

## Milestone 0 — Project foundation ✅

**Delivered**

- Monorepo layout with enforced boundaries (`backend/`, `vision/`, `frontend/`,
  `training/`, `deployment/`, `docs/`, `scripts/`)
- Typed configuration (`app/core/config.py`) — every setting comes from the
  environment; nothing reads `os.environ` directly
- Structured logging (`app/core/logging.py`) with automatic credential
  redaction on every log line
- Consistent API error envelope (`app/core/errors.py`, `app/api/errors.py`) —
  stack traces are logged with an incident id, never returned
- Request-context middleware: request ids, access logging, timing
- Database session management with a portable `GUID` column type
- Redis client with graceful-degradation health checks
- `GET /health`, `GET /ready`, `GET /live`
- Docker Compose for the full stack plus an infra-only compose file
- `.env.example` covering every setting
- Test harness: pytest, fixtures, in-memory SQLite for unit tests
- Ruff lint + format configuration, clean

**Environment notes**

- PostgreSQL runs on host port **5434** (5433 was already taken on this
  machine) and Redis on **6380**.
- Torch is CPU-only in this development environment, so CPU inference is the
  verified path.

**Verification**

```
$ cd backend && python -m pytest
26 passed

$ curl http://localhost:8000/health
{"status":"ok","database":"connected","redis":"connected","version":"0.1.0",
 "environment":"development","uptime_seconds":4.437,...}

$ python -m ruff check backend vision
All checks passed!
```


---

## Milestone 1 — Database schema ✅

**Delivered**

- 35 tables covering every entity in the specification, plus `CountingLine`,
  `LineCrossingEvent`, `CorrelationResult`, `ModelVersion` and `DailyMetric`
  which the later milestones need.
- UUID primary keys via a portable `GUID` type (native `uuid` on PostgreSQL,
  `CHAR(36)` on SQLite for tests).
- `UTCDateTime` column type — SQLite has no timezone support and silently
  returns naive datetimes, which raises `TypeError` at the first comparison
  against `datetime.now(UTC)`. The decorator normalises both directions so
  application code can rely on every timestamp being aware.
- Enums stored as `VARCHAR` **with check constraints**. SQLAlchemy defaults
  `create_constraint=False`, which would have left them unconstrained; a
  PostgreSQL integration test caught this.
- `organization_id` denormalised onto every leaf table and indexed, so the
  tenant filter is the first predicate on every query.
- Alembic configured; the initial migration applies, downgrades and
  re-upgrades cleanly, and `alembic check` reports no drift.
- `TenantScope` — a frozen dataclass that is the single enforcement point for
  multi-tenant isolation.
- Permission catalogue (44 permissions, 6 roles) defined in code and
  idempotently synced to the database.

**Design decisions**

- Coordinates (zone polygons, bounding boxes, counting lines) are stored
  **normalised 0..1**, not in pixels, so they survive a resolution change or a
  switch from main to sub stream.
- `Event.is_inference` separates measurements from conclusions, with
  `source_event_ids` and `rule_name` making every inference auditable.
- Money uses `Numeric`, never `Float`.
- `AuditLog` has no `updated_at` and no soft delete.

**Verification**

```
99 passed  (15 against real PostgreSQL)
alembic upgrade head / downgrade base / upgrade head  -> clean
alembic check -> No new upgrade operations detected
36 tables in PostgreSQL (35 + alembic_version), 24 check constraints
```

---

## Milestone 2 — Authentication & RBAC ✅

**Delivered**

- Argon2id password hashing with OWASP parameters and transparent rehashing
- Password policy: 12-character minimum, no email local part, no common values
- JWT access tokens (30 min) and refresh tokens (14 days), type-checked
- Refresh-token rotation with **family reuse detection**
- Account lockout after 8 failed attempts
- Registration creating an organization, trial subscription and first admin
- `/auth/login`, `/register`, `/refresh`, `/logout`, `/me`, `/change-password`,
  `/sessions`
- Fernet encryption for camera credentials, refusing a derived key in production
- Audit logging with recursive secret sanitisation
- FastAPI dependencies: `CurrentUser`, `Scope`, `require_permission(...)`

**A bug the live test caught that the unit tests hid**

Failure paths flushed security state and then raised. The request aborted
before the route's `session.commit()`, so the lockout counter and the
stolen-token family revocation were rolled back — brute-force protection and
reuse detection would have silently done nothing in production. The unit tests
passed because they shared one session that was never rolled back between
requests.

Fixed by committing explicitly in those paths, and covered by four durability
tests that roll back before asserting. The test fixture now truncates tables
between tests instead of relying on transaction rollback, so services that
commit are exercised honestly.

**Verification**

```
176 passed

Live against the running server + real PostgreSQL:
  register            -> 201 role=ORGANIZATION_ADMIN perms=41
  me (no token)       -> 401 NOT_AUTHENTICATED
  me (refresh token)  -> 401 TOKEN_INVALID
  login (bad pw)      -> 401 INVALID_CREDENTIALS
  login (no such user)-> 401 INVALID_CREDENTIALS   (byte-identical)
  refresh             -> 200 rotated=True
  refresh (replay)    -> 401  <- stolen-token detection
  refresh (victim)    -> 401  <- whole family revoked
  weak password       -> 422 VALIDATION_ERROR
```
