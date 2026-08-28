# VisionGuard

**AI-powered CCTV, retail security and inventory intelligence platform.**

VisionGuard turns ordinary CCTV cameras into intelligent cameras. It connects to
existing cameras and NVRs over RTSP, runs object detection and tracking on the
video, and applies configurable rules to produce **observations** — which the
event engine then interprets into business-meaningful events and alerts.

---

## The one principle that shapes everything

> **An observation is not a conclusion.**

The vision engine only ever reports what it can measure:

```
PERSON_ENTERED_ZONE     camera 4, track 42, zone "Warehouse"
PRODUCT_REMOVED         shelf 12, confidence 0.71
POS transaction         none within 120 s
```

The event engine combines those observations into an *inference*, clearly
labelled as one:

```
POSSIBLE_INVENTORY_DISCREPANCY   confidence: medium
```

VisionGuard never claims that someone committed theft. It reports what was
observed, what rule fired, and how confident it is. A human decides the rest.

---

## Architecture at a glance

```
CCTV / NVR
    ↓  RTSP
Video Ingestion  ──►  Frame Processing  ──►  YOLO Detection
                                                  ↓
                                            Object Tracking
                                                  ↓
                                       Zone / Line / Rule Engine
                                                  ↓
                                             Event Engine
                                              ↓        ↓
                                      Event Storage   Alert Engine
                                              ↓        ↓
                                        FastAPI  ──► WebSocket ──► Dashboard
```

Inventory path:

```
Camera ──► Product / Shelf Detection ──► Inventory Event
                                              ↓
                                      POS Correlation
                                              ↓
                             Inventory Intelligence ──► Dashboard / Alerts
```

Full detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Repository layout

| Path | Contents |
|------|----------|
| `backend/` | FastAPI application — API, auth, database, WebSockets, workers |
| `vision/` | Standalone vision engine — detection, tracking, zones, events. **No web framework imports.** |
| `frontend/` | React + TypeScript + Vite dashboard |
| `training/` | Custom-model dataset tooling and training scripts |
| `deployment/` | Docker, GPU and edge deployment configuration |
| `scripts/` | Developer and operational scripts |
| `docs/` | Architecture and subsystem documentation |
| `data/` | Runtime media, evidence and sample fixtures (git-ignored) |

The vision engine is deliberately importable **without** the backend, so it can
run on an edge device with no cloud connection. See
[`docs/EDGE_AI.md`](docs/EDGE_AI.md).

---

## Quick start (local development)

### Prerequisites

- Python 3.11+ (this repo is developed against 3.13)
- Node.js 20+
- Docker Desktop (for PostgreSQL and Redis)

### 1. Start infrastructure

```bash
docker compose -f deployment/docker/compose.infra.yml up -d
```

This starts PostgreSQL on **host port 5434** and Redis on **6380**. Non-default
ports are used deliberately so VisionGuard does not collide with other local
databases.

### 2. Configure

```bash
cp .env.example .env
```

Then edit `.env`. At minimum, before any non-local deployment, replace
`JWT_SECRET` and set `CREDENTIALS_ENCRYPTION_KEY`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"                       # JWT_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # CREDENTIALS_ENCRYPTION_KEY
```

### 3. Install Python dependencies

```bash
python -m venv venv
venv/Scripts/python -m pip install -r backend/requirements-dev.txt   # Windows
venv/Scripts/python -m pip install -r vision/requirements.txt
```

On Linux/macOS use `venv/bin/python`. `torch` is installed as a dependency of
`ultralytics`; to force a CPU-only or CUDA build see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

### 4. Run the API

```bash
cd backend
../venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

Verify:

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "database": "connected",
  "redis": "connected",
  "version": "0.1.0",
  "environment": "development",
  "uptime_seconds": 4.437,
  "timestamp": "2026-08-24T15:27:00.536148Z"
}
```

Interactive API docs: <http://localhost:8000/docs>

### 5. Run the tests

```bash
cd backend
../venv/Scripts/python -m pytest
```

---

## Developer commands

`make help` lists everything. The common ones:

| Command | Does |
|---------|------|
| `make infra-up` | Start PostgreSQL + Redis |
| `make dev` | Run the API with autoreload |
| `make test` | Run the backend + vision test suite |
| `make lint` | Ruff lint + format check |
| `make migrate` | Apply database migrations |
| `make frontend-dev` | Run the dashboard dev server |
| `make up` | Run the whole stack in Docker |

---

## Health endpoints

| Endpoint | Purpose | Behaviour |
|----------|---------|-----------|
| `GET /live` | Liveness | Always 200 while the process is alive. Touches no dependency. |
| `GET /ready` | Readiness | 503 when PostgreSQL or Redis is unreachable. |
| `GET /health` | Human/monitoring | Always 200; `status` is `ok` or `degraded` with per-component detail. |

`/health` deliberately returns 200 when degraded so monitoring can distinguish
"the process is up but Redis is gone" from "the process is dead".

---

## Documentation

| Document | Covers |
|----------|--------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and boundaries |
| [DEVELOPMENT.md](docs/DEVELOPMENT.md) | Local setup, conventions, workflow |
| [VISION_PIPELINE.md](docs/VISION_PIPELINE.md) | Frame → detection → tracking → event |
| [CAMERA_SETUP.md](docs/CAMERA_SETUP.md) | Connecting real CCTV / NVR hardware |
| [YOLO.md](docs/YOLO.md) | Detection engine, models, thresholds |
| [TRACKING.md](docs/TRACKING.md) | Tracker choice and tuning |
| [ZONES.md](docs/ZONES.md) | Zone and line geometry |
| [EVENT_ENGINE.md](docs/EVENT_ENGINE.md) | Rules, event types, severity |
| [EVIDENCE.md](docs/EVIDENCE.md) | Snapshot / clip capture, delivery and retention |
| [INVENTORY.md](docs/INVENTORY.md) | Shelf occupancy and stock intelligence |
| [CUSTOM_MODELS.md](docs/CUSTOM_MODELS.md) | Training and running per-retailer product models |
| [POS_INTEGRATION.md](docs/POS_INTEGRATION.md) | POS providers and correlation |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | CPU, GPU and edge deployment |
| [EDGE_AI.md](docs/EDGE_AI.md) | Offline-first edge architecture |
| [SECURITY.md](docs/SECURITY.md) | Threat model and controls |
| [PRIVACY.md](docs/PRIVACY.md) | Privacy stance and retention |
| [TESTING.md](docs/TESTING.md) | Test strategy and fixtures |

---

## Project status

Built in milestones. Each milestone is implemented, tested and verified before
the next begins — see [`docs/MILESTONES.md`](docs/MILESTONES.md) for the
current state.

---

## Licence

Proprietary. © VisionGuard.
