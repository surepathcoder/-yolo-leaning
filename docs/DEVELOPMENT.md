# Development guide

## Environment

| Requirement | Version used here |
|-------------|-------------------|
| Python | 3.13.6 (3.11+ supported) |
| Node.js | 22.18 |
| Docker | 29.4 with Compose v5 |
| PyTorch | 2.13.0 **+cpu** |

This machine has no CUDA device, so CPU inference is the path that is actually
exercised. GPU support is implemented and configuration-driven but is verified
separately on GPU hardware — see [`DEPLOYMENT.md`](DEPLOYMENT.md).

## First-time setup

```bash
# 1. Infrastructure
docker compose -f deployment/docker/compose.infra.yml up -d

# 2. Configuration
cp .env.example .env

# 3. Python dependencies
venv/Scripts/python -m pip install -r backend/requirements-dev.txt
venv/Scripts/python -m pip install -r vision/requirements.txt

# 4. Database schema
venv/Scripts/python -m alembic upgrade head

# 5. Run
cd backend && ../venv/Scripts/python -m uvicorn app.main:app --reload
```

### Port allocation

| Service | Host port | Note |
|---------|-----------|------|
| API | 8000 | |
| Dashboard (Vite) | 5173 | |
| PostgreSQL | 5434 | Deliberately not 5432/5433 to avoid collisions |
| Redis | 6380 | Deliberately not 6379 |

If a port is taken, change it in `.env` — nothing is hardcoded.

## Code conventions

### Layering

```
api/       →  services/  →  models/
   ↑             ↑
schemas/      core/
```

- **Routes** validate input and delegate. No SQL, no CV code, no business rules.
- **Services** hold business logic and always take an explicit tenant scope.
- **Models** are SQLAlchemy only — no HTTP, no vision imports.
- **`vision/`** never imports `app.*`. If you feel the urge, you are putting
  business logic in the wrong layer.

### Configuration

Never call `os.environ` outside `app/core/config.py`. Add a typed field to
`Settings`, document it in `.env.example`, and read it via `get_settings()`.

### Errors

Raise an `AppError` subclass. Put user-safe text in `message` and technical
detail in `internal_message` — the latter is logged, never returned.

```python
raise CameraConnectionError(
    details={"camera_id": str(camera.id)},
    internal_message=f"cv2.VideoCapture failed: {exc}",
)
```

### Logging

Use `get_logger(__name__)` and log **events with fields**, not sentences:

```python
log.info("camera_connected", camera_id=str(cam.id), store_id=str(cam.store_id), fps=12.4)
```

Never log a credential. The redaction processor is a safety net, not a licence.

### Naming events

Event type names are `SCREAMING_SNAKE_CASE` verbs in the past tense describing
what was *observed*: `PERSON_ENTERED_ZONE`, not `INTRUDER_ALERT`. Inference
names are hedged: `POSSIBLE_INVENTORY_DISCREPANCY`.

## Testing

```bash
cd backend
../venv/Scripts/python -m pytest                 # everything
../venv/Scripts/python -m pytest -m "not integration and not vision"   # fast
../venv/Scripts/python -m pytest -m integration  # needs PostgreSQL + Redis
../venv/Scripts/python -m pytest -m vision       # needs YOLO weights
```

Unit tests use in-memory SQLite via the `db_session` fixture. That is a *test*
convenience — PostgreSQL is the production architecture, and anything using
PostgreSQL-specific behaviour must carry the `integration` marker and run
against the real database.

See [`TESTING.md`](TESTING.md).

## Linting

```bash
venv/Scripts/python -m ruff check backend vision scripts training
venv/Scripts/python -m ruff format backend vision scripts training
```

Ruff config lives in `pyproject.toml`. Line length is 100.

## Database migrations

```bash
# after changing a model
venv/Scripts/python -m alembic revision --autogenerate -m "add loitering threshold"
venv/Scripts/python -m alembic upgrade head
```

Always read the generated migration before committing it — autogenerate misses
server defaults, index renames and type changes.

**Destructive migrations** (dropping a column or table that holds data) must be
split: deploy the code that stops using the column, then drop it in a later
migration.

## Working on the vision engine without cameras

The `LocalVideoSource` reads a video file and the `SyntheticVideoSource`
generates deterministic frames, so the whole pipeline can be exercised in tests
and in development with no hardware. Sample fixtures live in `data/samples/`.

## Git

The repository root of this checkout is currently the user's home directory,
which is not intended. Before collaborating, initialise a repository at the
project root:

```bash
cd <project root>
git init
git add .
git commit -m "VisionGuard: initial commit"
```

`.gitignore` already excludes `venv/`, `.env`, model weights, media and
evidence.
