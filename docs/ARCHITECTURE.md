# Architecture

## 1. Design goals

VisionGuard is built for a specific operating reality: existing CCTV hardware,
unreliable internet, modest compute budgets, and multiple independent business
customers on one installation. Those constraints drive every structural
decision below.

| Goal | Consequence |
|------|-------------|
| Run on the customer's premises | The vision engine has zero dependency on the web framework |
| Survive internet outages | Events queue locally and sync when connectivity returns |
| Serve many businesses safely | Every tenant-owned query is organization-scoped |
| Be believable in court, not just in a demo | Observations and inferences are stored separately |
| Work without a GPU | CPU inference is a first-class path, not a fallback |

## 2. Component boundaries

```
┌──────────────────────────────────────────────────────────────────┐
│                            vision/                               │
│  Pure Python + OpenCV + PyTorch. No FastAPI, no SQLAlchemy.      │
│                                                                  │
│  cameras/   VideoSource abstraction (file, webcam, RTSP)         │
│  detection/ DetectionEngine — frame in, Detection[] out          │
│  tracking/  Tracker — Detection[] in, TrackedObject[] out        │
│  zones/     Polygon and line geometry                            │
│  counting/  Line-crossing counters                               │
│  events/    Rule evaluation → VisionEvent                        │
│  inventory/ Shelf occupancy estimation                           │
│  pos/       POSProvider abstraction and correlation              │
│  pipelines/ Wires the above into a runnable per-camera pipeline  │
└────────────────────────────┬─────────────────────────────────────┘
                             │  VisionEvent (plain dataclass)
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                           backend/                               │
│  FastAPI + SQLAlchemy + Redis.                                   │
│                                                                  │
│  api/        HTTP routes — thin, no business logic               │
│  services/   Business logic, tenant scoping, orchestration       │
│  models/     SQLAlchemy ORM                                      │
│  schemas/    Pydantic request/response contracts                 │
│  workers/    Background processing (retention, sync, pipelines)  │
│  websocket/  Realtime fan-out to the dashboard                   │
│  core/       Config, logging, errors, security primitives        │
└────────────────────────────┬─────────────────────────────────────┘
                             │  JSON / WebSocket
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                          frontend/                               │
│  React + TypeScript + Vite. Talks only to the documented API.    │
└──────────────────────────────────────────────────────────────────┘
```

### Rules enforced by this layout

1. **`vision/` must never import from `backend/`.** It emits plain dataclasses.
   A test can `import vision` with no database, no Redis, no web server.
2. **API route files contain no computer-vision logic and no raw SQL.** They
   validate input, call a service, and shape a response.
3. **Service functions take an explicit tenant scope.** There is no ambient
   "current organization" global.
4. **The frontend knows the API contract, never the Python types.** Response
   schemas are the boundary.

## 3. Data flow: a security event

```
 1. RTSPVideoSource yields a frame                    (vision/cameras)
 2. FrameProcessor resizes / skips to target FPS      (vision/pipelines)
 3. DetectionEngine runs YOLO                         (vision/detection)
      → Detection(class_name="person", conf=0.94, bbox=…)
 4. Tracker assigns a persistent id                   (vision/tracking)
      → TrackedObject(track_id=42, trajectory=[…])
 5. ZoneEngine tests the reference point              (vision/zones)
      → observation: PERSON_ENTERED_ZONE
 6. Rules evaluate duration, schedule, thresholds     (vision/events)
      → VisionEvent(type=LOITERING, severity=MEDIUM, confidence=0.88)
 7. Backend persists the event + evidence             (backend/services)
 8. AlertEngine applies severity routing              (backend/services)
 9. WebSocket pushes to subscribed dashboards         (backend/websocket)
```

Steps 1–6 run entirely on the edge. Steps 7–9 need the backend, which may be
local or in the cloud.

## 4. Observation vs inference

This distinction is structural, not cosmetic.

| | Observation | Inference |
|---|---|---|
| Produced by | Vision engine | Event engine |
| Example | `PRODUCT_REMOVED` from shelf 12 | `POSSIBLE_INVENTORY_DISCREPANCY` |
| Answers | "What did the camera measure?" | "What might that mean, per configured rules?" |
| Stored in | `detection_events`, `zone_events` | `events` with `is_inference = true` |
| Carries | model confidence | rule name + supporting observation ids |

An inference always records which observations produced it, so a reviewer can
audit the chain. The UI renders inferences with hedged language and a visible
confidence level.

## 5. Multi-tenancy

Every tenant-owned table carries `organization_id`. The rules:

- Service functions accept a `TenantScope` describing the caller's
  organization and accessible stores.
- Queries filter on `organization_id` **before** any other predicate.
- `SUPER_ADMIN` is the only role that may operate cross-organization, and doing
  so is written to the audit log.
- Cross-tenant access attempts raise `ORGANIZATION_SCOPE_VIOLATION` and are
  logged at WARNING with the actor's user id.

## 6. Storage decisions

| Data | Store | Why |
|------|-------|-----|
| Entities, events, audit | PostgreSQL | Relational integrity, aggregation queries for analytics |
| Realtime fan-out, rate limits, short caches | Redis | Pub/sub between vision workers and API processes |
| Snapshots and clips | Filesystem (S3-compatible later) | Large binaries do not belong in a row |
| Edge event queue | Local SQLite | Survives process restart with no server |

Analytics aggregation happens in PostgreSQL, never in the browser.

## 7. Failure behaviour

| Failure | Behaviour |
|---------|-----------|
| Camera disconnects | Pipeline marks camera `ERROR`, retries with backoff, keeps other cameras running |
| Model fails to load | Camera pipeline refuses to start and reports `MODEL_LOAD_FAILED`; API stays up |
| Redis unavailable | `/health` reports `degraded`; events still persist; realtime push is skipped |
| Database unavailable | `/ready` returns 503; edge queue absorbs events until it recovers |
| Internet unavailable (edge) | Vision keeps running; events queue locally and sync later |

The system fails **safe**: it stops reporting rather than reporting wrongly.

## 8. Why these technologies

| Choice | Reason | Alternative rejected because |
|--------|--------|------------------------------|
| FastAPI | Async, native WebSockets, OpenAPI for free | Django REST is heavier than needed here |
| SQLAlchemy 2.x typed ORM | Static typing catches tenant-scoping mistakes | Raw SQL scatters tenant filters |
| PostgreSQL | Window functions and time aggregation for analytics | SQLite cannot serve concurrent stores |
| Ultralytics YOLO | Best accuracy/latency ratio on CPU-class hardware | Detectron2 is too heavy for an edge box |
| ByteTrack | Strong ID stability without a re-ID model | DeepSORT needs a second network per frame |
| React + Vite | Fast builds, mature realtime tooling | A generic admin template cannot express live video |
| Redis pub/sub | Decouples vision workers from API processes | In-process queues break with multiple workers |

## 9. Deployment topologies

**Single box (small shop):** everything on one machine, PostgreSQL local,
no cloud. Cheapest, works with no internet.

**Edge + cloud (chain):** vision engine on an in-store box, backend in the
cloud, only metadata and selected evidence crosses the network. Raw video never
leaves the premises.

**All cloud (IP cameras reachable):** rare in the target market — most
installations sit behind consumer NAT — but supported.

See [`DEPLOYMENT.md`](DEPLOYMENT.md) and [`EDGE_AI.md`](EDGE_AI.md).
