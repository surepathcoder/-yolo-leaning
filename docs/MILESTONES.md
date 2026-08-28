# Milestone status

Each milestone is implemented, tested and verified before the next begins.
"Verified" means the tests pass **and** the behaviour was exercised against the
running system.

**Current state:** the backend test suite passes, `ruff check` and
`ruff format --check` are clean across `backend`, `vision`, `scripts` and
`training`, and the frontend typechecks, builds and passes its Vitest suite.
Milestones 3-17 are complete.

| # | Milestone | Status | Verified by |
|---|-----------|--------|-------------|
| 0 | Project foundation | ✅ Done | 26 tests; live `/health` against real PostgreSQL + Redis |
| 1 | Database schema | ✅ Done | 35 tables; migration round-trips on real PostgreSQL; 15 integration tests |
| 2 | Authentication & RBAC | ✅ Done | 44 API tests + 26 crypto tests; live smoke test against real server |
| 3 | Camera management | ✅ Done | `test_camera_api.py`; credentials encrypted at rest |
| 4 | Video ingestion | ✅ Done | `vision/cameras`; file, webcam, RTSP and synthetic sources |
| 5 | YOLO detection | ✅ Done | `vision/detection`; CPU inference against `yolo26n.pt` |
| 6 | Object tracking | ✅ Done | `test_vision_tracking.py`; ByteTrack + Kalman |
| 7 | Zone engine | ✅ Done | `test_vision_zones.py`, `test_zones_pipeline.py` |
| 8 | Line crossing & counting | ✅ Done | `test_vision_counting.py` |
| 9 | Loitering | ✅ Done | `vision/events/rules.py`; covered in `test_zones_pipeline.py` |
| 10 | After-hours detection | ✅ Done | `vision/events/schedule.py` + store operating hours |
| 11 | Event engine | ✅ Done | `test_events_alerts.py` |
| 12 | Alert engine | ✅ Done | `test_events_alerts.py`; notification channels |
| 13 | Event evidence | ✅ Done | 45 tests (`test_vision_evidence.py`, `test_evidence_service.py`, `test_evidence_api.py`); snapshot + thumbnail + clip captured and served, audited, swept |
| 14 | Retail inventory | ✅ Done | 67 tests (`test_vision_inventory.py`, `test_inventory_api.py`); wired through `build_pipeline_config` |
| 15 | Custom product detection | ✅ Done | `training/` tooling + a real short train; `test_models_api.py`, `test_models_service.py`, `test_vision_models.py`, `test_training.py`; second-pass detector feeds occupancy |
| 16 | POS integration | ✅ Done | 71 tests (`test_vision_pos.py`, `test_pos_api.py`); `POSSIBLE_INVENTORY_DISCREPANCY` can now fire |
| 17 | Dashboard | ✅ Done | 12 pages, realtime via WebSocket; typechecks and builds clean; 51 Vitest/RTL tests covering the API client, auth store, UI primitives, routing gate and the sign-in, models and shell pages |

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

---

## Milestone 13 — Event evidence ✅

**Delivered**

*Vision engine* (`vision/evidence/`, no backend dependency)

- `EvidenceCapturer` — a rolling buffer of the last `pre_seconds` of decoded
  frames; on a triggering event it emits an annotated snapshot and a thumbnail
  at once, then fills a clip from `pre_seconds` before to `post_seconds` after
  and encodes it to MP4
- `should_capture()` — a deliberate mirror of `alerts.py::should_alert`, so
  evidence is kept for exactly the events that warrant a human's attention and
  nothing else. A parametrised test asserts the two never drift
- Encoding failures are counted and swallowed, never raised into the frame
  thread — same contract as `CallbackSink`

*Backend*

- `services/evidence.py` — writes bytes under `EVIDENCE_STORAGE_PATH` by a
  relative path, records size, SHA-256 and `expires_at`, and resolves an
  artefact tenant-scoped through its parent event
- `pipeline_manager` hands each artefact to a per-pipeline single-thread
  executor **off the frame thread**, matches it to the persisted event by
  dedupe key, and re-broadcasts so the dashboard flips `has_evidence`
- `GET /evidence/{id}` and `/download` — authorised (`evidence:view` /
  `evidence:export`), audited (`EVIDENCE_ACCESSED` / `EXPORT`), served from disk
- `services/maintenance.py` + a lifespan loop + `POST /system/maintenance/retention`
  — deletes evidence past `expires_at` (row *and* file) and prunes expired tokens

*Dashboard* — the event timeline's expanded row shows each snapshot and an
inline clip player, fetched as blobs through the authenticated client (an
`<img src>` carries no token), gated on `evidence:view`, with a download for
holders of `evidence:export`.

**What it refuses to do**

- No continuous recording. Only alert-worthy events, a few seconds each.
- No public or guessable URLs; every byte served is authorised and audited.
- `ANALYST` cannot see any of it — analysing traffic patterns does not require
  looking at pictures of people.

Full detail: [`docs/EVIDENCE.md`](EVIDENCE.md).

**Verification**

```
$ cd backend && python -m pytest
636 passed

$ python -m ruff check backend vision scripts training
All checks passed!

$ cd frontend && npm run typecheck && npm run build
✓ built
```

Also exercised end to end by `test_a_synthetic_pipeline_produces_events_and_alerts`:
a real `CameraPipeline` runs through the real `pipeline_manager` wiring and the
real event/alert services, a HIGH-severity `RESTRICTED_AREA_ENTRY` fires, and
the test asserts a `SNAPSHOT`, a `THUMBNAIL` and a `CLIP` land on disk with a
checksum and non-zero size.

Live against a running uvicorn (SQLite; Redis unavailable, so the server ran
degraded — the Docker engine was down this session, but every code path M13
touches was exercised):

```
pipeline start                    -> 200 STARTING
event detail                      -> is_inference=False  evidence kinds=[CLIP, SNAPSHOT, THUMBNAIL]
GET snapshot (admin)              -> 200 image/jpeg 6800B
GET clip (admin)                  -> 200 video/mp4 36268B  7.5s
EVIDENCE_ACCESSED audit rows      -> 2
GET snapshot (ANALYST)            -> 403   <- no evidence:view
GET snapshot (other org)          -> 404
GET download (admin)              -> 200 Content-Disposition: attachment
retention sweep                   -> {evidence_deleted: 1, tokens_deleted: 0}  rows 6 -> 5
accusatory language in summary    -> NONE
```

The two that matter: a HIGH-severity event captured all three artefacts and
served them only to a caller holding `evidence:view` in the owning
organization, and the retention sweep removed the row *and* its file the moment
`expires_at` passed.

---

## Milestone 14 — Retail inventory ✅

**Delivered**

*Vision engine* (`vision/inventory/`, no backend dependency)

- `estimate_occupancy()` — the fraction of a shelf polygon covered by product
  detections, measured on a fixed 64x64 grid clipped to the polygon, so an
  L-shaped or angled shelf is measured over its true area
- Coverage is a **union** over detections, not a sum: two overlapping boxes
  describe one covered patch, and adding them would report a shelf as more
  than full
- Optional `baseline_occupancy` calibrates out the gaps, shadows and shelf
  edge that stop a genuinely full shelf from ever reading 1.0
- `InventoryEngine` — interval sampling, a stability requirement before a new
  value is believed, per-kind cooldowns, and level events that fire on entry
  only
- `calibrate_baseline()` — median, not max, so one inflated frame cannot
  become a permanent baseline

*Backend* — product and shelf CRUD (both soft-deleting), reading and event
history, per-shelf status, an organization summary, and 9 endpoints under
`/products`, `/shelves` and `/inventory`.

*Pipeline* — `PipelineConfig.shelves`, an `InventoryEngine` per camera, shelf
observations on `FrameResult`, and `apply_config` adopting shelf changes
without restarting the camera.

*Dashboard* — an Inventory page with shelf levels, activity and the catalogue.

**Occlusion, which is the point**

A person standing at a shelf hides it. Reported naively, that shelf reads
empty, and the system announces a stockout every time somebody shops. The
whole feature's credibility rests on not doing that, so occlusion is handled
at all four layers:

| Layer | Behaviour |
|-------|-----------|
| `estimate_occupancy` | Occluders are measured separately; coverage is computed over the **visible** part of the shelf, so a half-hidden shelf whose visible half is stocked reads as stocked |
| `InventoryEngine` | Occluded readings never move state and never emit; they also clear any partial confirmation, so readings either side of a passer-by are not stitched together |
| `services/inventory` | Occluded snapshots are stored (they are a true record of what the camera saw) but never establish status and never count towards "empty" |
| Dashboard | An unknown shelf gets no progress bar at all — a zero-width bar and a genuinely empty shelf would look identical |

A shelf that has never been read reports `UNKNOWN`, which is deliberately
distinct from reading as empty, and the summary surfaces that count rather
than hiding it: "we cannot see 6 of your shelves" is information the operator
needs.

**What it deliberately does not do**

No endpoint returns a unit count. CCTV at shelf-monitoring angles cannot
resolve individual items on a packed shelf, and a field called `quantity`
would invite every consumer to believe it could. Occupancy is a ratio and a
coarse level, and that is the honest limit of what the camera knows.

Nothing here concludes *why* stock moved. Restocking, a customer, a staff
member tidying a display and a detector error all produce the same row.
`nearby_track_keys` is recorded as context for a human reviewer, computed per
shelf, and is explicitly not an attribution — see [`PRIVACY.md`](PRIVACY.md).

`POSSIBLE_INVENTORY_DISCREPANCY` could not fire until milestone 16.

**Verification**

```
$ cd backend && python -m pytest
530 passed

$ python -m ruff check backend vision scripts training
All checks passed!

$ cd frontend && npm run typecheck && npm run build
✓ built in 7.60s
```

67 of those tests are new. The ones that matter most assert that an occluded
reading never becomes a shelf's status, that a never-read shelf reports
UNKNOWN rather than empty, that a single anomalous reading cannot manufacture
a stockout, and that one tenant cannot see another's stock levels.

Live against the running server + real PostgreSQL and Redis:

```
create product        -> 201 sku=AZAM-500 detectable=True
create shelf          -> 201 code=A3-T products=1
shelf too small       -> 422 INVALID_POLYGON
status (never read)   -> 200 level=UNKNOWN ratio=None      <- not "empty"
status (full reading)  -> 200 level=FULL    ratio=0.92
status (occluded)     -> 200 level=FULL    occluded=True  <- not "empty"
summary               -> 200 total=1 empty=0 unknown=0 products=1
snapshots stored      -> 200 total=2                       <- occluded one kept
tenant isolation      -> list=0 byid=404 NOT_FOUND
```

The two lines marked above are the ones the feature's credibility rests on: a
shelf nobody has looked at, and a shelf somebody is standing in front of, both
refuse to claim they are empty.

---

## Milestone 15 — Custom product detection ✅

**Delivered**

*`training/`* (no backend or web-framework dependency)

- `datasets.py` — strict validation of a YOLO-format dataset: every image has a
  label, every class id is in range, every box is normalised, both splits are
  non-empty. A dataset that trains "fine" but has an unlabelled image produces a
  model that quietly ignores real stock; that is caught here, before the run.
- `train.py` — wraps Ultralytics, tuned for CPU (`imgsz=320`, augmentation off
  for a tiny set). Returns a plain `TrainingResult` with the weights path, the
  class names in id order, and the validation metrics.
- `register.py` — turns that into the payload `models.register_model` accepts.
- A bundled synthetic dataset (`samples/products/`, "cartons" on a shelf) so
  the test suite does a real short training run in CI.

*Backend*

- `services/models.py` — register (weights on disk by a relative path + SHA-256,
  same discipline as evidence), list (own org **plus** shared), **activate**,
  archive, delete, assign-to-camera. Activation is the gate that matters: a
  model with no metrics, or `map50` below `MODEL_MIN_MAP50`, is refused, and
  activating one version archives the previous active one — one active per name.
- `pipeline_manager` builds a `ProductModelConfig` for a camera whose assigned
  model is `ACTIVE`, same-org and checksum-verified. A missing / stale / wrong-org
  model logs loudly and the camera keeps running on COCO — a bad model must not
  blind a camera.
- 7 endpoints under `/models`, plus `GET /inventory/product-detections`.

*Vision* — `CameraPipeline` runs the custom model as a **second pass** on its
own interval (`MODEL_DETECTION_INTERVAL_SECONDS`). Its boxes are merged into the
detections the occupancy engine sees — so a shelf is measured by real product
class — but never reach the security rules. A carton in a restricted zone
raises nothing.

**What it refuses to do**

| Situation | Result |
|-----------|--------|
| Model has no validation metrics | Stays `DRAFT`; cannot be activated |
| `map50` below `MODEL_MIN_MAP50` (0.30) | Activation refused |
| Model belongs to another organization | Will not load; not listed; 404 by id |
| Weights file no longer matches its checksum | Will not load |
| Assigned model is not `ACTIVE` | Assignment refused |
| Product box in a restricted zone | Not an event — feeds occupancy only |
| Assigned model missing at pipeline start | Camera runs on COCO, logs why |

Full detail: [`docs/CUSTOM_MODELS.md`](CUSTOM_MODELS.md).

**Verification**

```
$ cd backend && python -m pytest
<suite passes, incl. test_models_api.py, test_models_service.py,
 test_vision_models.py, test_training.py>

$ python -m ruff check backend vision scripts training
All checks passed!

$ cd frontend && npm run typecheck && npm run build
✓ built
```

`test_training.py` fine-tunes a nano model on the bundled dataset for a handful
of epochs and asserts the result is registrable — weights produced, loadable,
metrics extracted. `test_vision_models.py` drives a real `CameraPipeline` with a
scripted product detector and asserts the two rules that matter: the product
boxes feed occupancy, and the security rules never see them.

Live against a running uvicorn (SQLite, as in M13):

```
train sample model (40 epochs, CPU)   -> best.pt  map50=0.883  map50-95=0.800
                                         detects [blue_carton, red_carton, red_carton] on a held-out image
POST /models  (register v1)           -> 201 status=VALIDATED activatable=true
POST /models/{weak}/activate (map50 0.20) -> 409   <- below MODEL_MIN_MAP50
POST /models/{v1}/activate            -> 200 status=ACTIVE
POST /models/{v2}/activate            -> 200, v1 is now ARCHIVED   <- one active per name
PUT  /models/{other-org}/cameras/{cam} -> 404      <- not this org's model
PUT  /models/{v2}/cameras/{cam}       -> 200, camera.model_version_id set
GET  /models/{v1}  as another org     -> 404
GET  /inventory/product-detections    -> 200
```

---

## Milestone 16 — POS integration ✅

**Delivered**

*Vision engine* (`vision/pos/`, no backend dependency)

- Provider-agnostic sale records, so correlation never learns whether a sale
  arrived from a CSV, a webhook or a vendor API
- `POSProvider` ABC plus in-memory and CSV implementations, registered by key
- `correlate()` — time, product and quantity signals combined into an
  auditable score with a stored per-signal breakdown

*Backend* — idempotent import with SKU and barcode resolution, correlation of
pending shelf removals, the discrepancy inference, a human review loop, an
accuracy summary, and 7 endpoints under `/pos`. Provider configuration lives
in `Store.settings`, so no migration was needed.

*Dashboard* — a Reconciliation page with the three outcomes, the score
breakdown behind a disclosure, and the review form.

**Coverage, which outranks everything else here**

If the till was offline, "no sale was found" carries no information. Reported
as `UNMATCHED` it would turn an afternoon of ordinary trading into a list of
suspicions — worst of all for the customer whose POS integration is least
reliable, which is exactly backwards.

So `FetchResult` carries `complete` and `reason`, and an incomplete window
yields `UNCERTAIN` with no inference. It can still confirm a match: absence of
evidence is unusable, presence of it is not. A store that has never imported
any POS data is treated the same way — it is not a store with no sales.

**When a discrepancy is inferred**

All four must hold: the change was a removal, coverage was complete, no
plausible sale was found, and the observation's own confidence is at least
0.5. `_should_infer` states these in one readable place and defers the middle
two to correlation's `supports_inference` rather than restating them, so the
two cannot drift apart.

Even then the event is hedged: `is_inference` set, the inventory event in
`source_event_ids`, severity `LOW`, and a summary written to survive being
read aloud in a dispute.

**What it refuses to conclude**

| Situation | Result |
|-----------|--------|
| Till offline / export truncated | `UNCERTAIN`, no inference |
| Store has never imported POS data | `UNCERTAIN`, no inference |
| Observation confidence below 0.5 | Recorded, but no inference |
| Stock arriving rather than leaving | Not correlated at all |
| Voided, refunded or pending sale | Not a sale; cannot explain a change |
| Unrecognised CSV status | `PENDING`, not assumed to be a sale |
| Missing CSV export | Raises, rather than reporting zero sales |
| Naive timestamp with no zone configured | Rejected, rather than guessed |

**Measuring rather than assuming**

Every result can be reviewed: `CORRECT`, `INCORRECT`, `EXPLAINED`, `UNCLEAR` —
about whether the match was right, never about a person. `EXPLAINED` is
counted separately, because "unmatched, and I know why" is not the system
being wrong. Accuracy is null until a human has judged something; an
unmeasured system must not report 0% or 100%.

**Verification**

```
$ cd backend && python -m pytest
606 passed

$ python -m ruff check backend vision scripts training
All checks passed!

$ cd frontend && npm run typecheck && npm run build
✓ built in 6.03s
```

Live against the running server + real PostgreSQL and Redis:

```
A. no till connected      -> UNCERTAIN inference=False   <- refuses to conclude
   "Cannot say whether a sale explains this: no POS data has been imported..."
B. sale explains it       -> MATCHED   score=0.84 inference=False
   breakdown={'time': 0.83, 'product': 1.0, 'quantity': 0.5}
C. till on, no sale near  -> UNMATCHED inference=True
   event=POSSIBLE_INVENTORY_DISCREPANCY  is_inference=True  sources=1
   rule=pos_correlation
   "Stock left 'Aisle 3 top' at 18:38 and no matching sale was found within
    120 seconds. This needs a human to check: restocking, staff moving
    stock, an item put back elsewhere, a mis-scanned item or a detection
    error all produce this result."
   accusatory language: NONE
D. low-confidence removal -> UNMATCHED inference=False   <- recorded, not concluded from
E. review EXPLAINED       -> reviewed=1 explained=1 accuracy=None
E. review twice           -> CONFLICT
```

Case C is the first time `POSSIBLE_INVENTORY_DISCREPANCY` has fired in this
codebase. Cases A and D are the ones that matter more: the same shelf change,
with the same missing sale, correctly refusing to conclude anything.

71 of those tests are new, and most assert what the system refuses to do. A
parametrised test drives every outcome shape and asserts no explanation ever
contains *theft*, *steal*, *stolen*, *shoplift*, *criminal*, *guilty*,
*suspect*, *culprit* or *dishonest*. Another asserts the correlation score is
byte-identical with and without nearby track keys, because presence at a shelf
is not evidence that a person moved the stock.

---

## Milestone 17 — Dashboard test suite ✅

The 12-page dashboard (routing, the auth gate, the API client, realtime
connection and every page) shipped typechecked and building clean, but with no
automated coverage — a regression in the token-refresh logic or the
observation/inference badge would only be caught by hand. `frontend/tests/`
closes that gap with Vitest + React Testing Library.

**Delivered**

- `tests/client.test.ts` — the two things `services/client.ts` exists for:
  single-flight token refresh (two concurrent 401s trigger exactly one
  `/auth/refresh` call, both original requests share and retry with the new
  token), and that every failure becomes a typed `ApiError` with the
  backend's `code` and field errors, never raw JSON.
- `tests/auth-store.test.ts` — `login`/`logout`/`restore` state transitions,
  including that a dead stored token is cleared rather than looping the app
  between the dashboard and the login page.
- `tests/primitives.test.tsx` — the formatting helpers every page leans on,
  and `EventKindBadge`'s two branches: "an observation is not a conclusion"
  is the platform's central rule, and it is now asserted on screen, not just
  believed true of the database.
- `tests/App.test.tsx` — `RequireAuth` actually redirects a signed-out
  visitor away from the dashboard.
- `tests/pages/Login.test.tsx` — sign-in, the 409 multi-organization
  disambiguation flow described in the page's own header comment, and
  registration.
- `tests/pages/Models.test.tsx` — the milestone 15 UI: the mAP activation
  gate disables (and explains) the Activate button, activating refreshes the
  list, and the camera-assignment table is permission-gated.
- `tests/components/AppShell.test.tsx` — navigation is filtered to exactly
  the sections a user's permissions allow, and signing out clears the
  session and returns to `/login`.

**What it does not cover yet**

Deep interaction tests for Analytics' charts, the LiveView player, and the
Zones drawing canvas — the highest-effort, lowest-regression-risk surfaces.
Added as the dashboard's untested area shrinks further, not required to call
this milestone done.

**Verification**

```
$ cd frontend && npm run typecheck && npm run build && npx vitest run
...
✓ built
Test Files  7 passed (7)
     Tests  51 passed (51)
```
